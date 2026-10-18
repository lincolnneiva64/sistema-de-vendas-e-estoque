from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from unittest import skipUnless
from unittest.mock import patch
from uuid import uuid4
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db import connection, transaction, close_old_connections
from django.test import TestCase, TransactionTestCase, Client
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse
from django.utils import timezone

from estoque.models import Funcionario, Produto, MovimentacaoEstoqueManual
from estoque.views import _baixar_estoque_produto
from . import services, views
from .models import Inventario, RodadaContagem, ConfirmacaoContagem, MovimentoObservado, EventoInventario
from .tests import produto
from . import tests_browser


class CicloFixture:
    def setUp(self):
        self.usuario = get_user_model().objects.create_superuser("gestor-ciclo", "", "teste")
        self.funcionario = Funcionario.objects.create(nome="Contador fictício")
        self.produto = produto(nome="Adoçante", unidade_compra="UN", vende_fracionado=False)
        self.inventario = services.criar_inventario(self.usuario, "Conferência adoçante", "manual", [self.produto.pk])
        self.rodada = self.inventario.itens.get().rodadas.get()
        self.acesso, self.token = services.gerar_acesso(self.usuario, self.inventario.pk, self.funcionario.pk, [self.rodada.pk])

    def ok(self, versao=0, quantidade="6"):
        return services.salvar_contagem(self.acesso.pk, self.rodada.pk, versao, quantidade, "", True, token_hash=self.acesso.token_hash)

    def versao(self):
        return Inventario.objects.get(pk=self.inventario.pk).versao

    def finalizar(self):
        return services.finalizar_conferencia(self.acesso.pk, self.versao(), token_hash=self.acesso.token_hash)


class CicloVidaTests(CicloFixture, TestCase):
    def test_adocante_6_ok_corrigido_8_ok_e_envio_geral(self):
        primeiro = self.ok()
        horario = primeiro.ok_em
        segundo = self.ok(primeiro.versao, "8")
        self.assertGreaterEqual(segundo.ok_em, horario)
        self.assertEqual(list(ConfirmacaoContagem.objects.order_by("pk").values_list("contado_base", flat=True)), [6, 8])
        self.assertEqual(set(ConfirmacaoContagem.objects.values_list("responsavel_nome", flat=True)), {self.funcionario.nome})
        self.assertEqual(set(ConfirmacaoContagem.objects.values_list("acesso_id", flat=True)), {self.acesso.pk})
        self.assertEqual(views.resumo_inicial()["conferencias"], 1)
        finalizada = self.finalizar()
        self.assertIsNotNone(finalizada.finalizada_em)
        self.assertEqual(views.resumo_inicial()["conferencias"], 0)
        self.assertEqual(views.resumo_inicial()["divergencias"], 1)
        for confirmar in (False, True):
            with self.assertRaises(ValueError):
                services.salvar_contagem(self.acesso.pk, self.rodada.pk, segundo.versao, "9", "", confirmar)
        with self.assertRaises(ValueError):
            services.sinalizar_fisico(self.rodada.pk, "Não deve gravar após envio", uuid4(), acesso_id=self.acesso.pk, token_hash=self.acesso.token_hash)
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, 10)
        self.assertFalse(MovimentoObservado.objects.exists())
        self.assertFalse(MovimentacaoEstoqueManual.objects.exists())

    def test_rascunho_apos_ok_preserva_historico_e_exige_novo_ok(self):
        self.ok()
        rascunho = services.salvar_contagem(self.acesso.pk, self.rodada.pk, 1, "8", "")
        self.assertIsNone(rascunho.ok_em)
        self.assertIsNone(rascunho.esperado)
        self.assertEqual(ConfirmacaoContagem.objects.get().contado_base, 6)
        with self.assertRaises(ValueError):
            self.finalizar()
        self.ok(rascunho.versao, "8")
        self.finalizar()

    def test_retry_antigo_nao_duplica_ok_nem_sobrescreve_correcoes(self):
        primeiro = self.ok()
        repetido = self.ok()
        self.assertEqual(primeiro.ok_em, repetido.ok_em)
        segundo = self.ok(1, "8")
        antigo = self.ok(0, "6")
        self.assertEqual((antigo.contado_base, antigo.ok_em), (8, segundo.ok_em))
        self.assertEqual(ConfirmacaoContagem.objects.count(), 2)
        with self.assertRaises(ValueError):
            self.ok(0, "9")
        with self.assertRaises(ValueError):
            services.salvar_contagem(self.acesso.pk, self.rodada.pk, 0, "7", "")

    def test_movimento_entre_oks_nao_desaparece_ou_vira_prova_fisica(self):
        self.ok()
        with transaction.atomic():
            _baixar_estoque_produto(self.produto.pk, 1, unidade="UN")
        movimento = MovimentoObservado.objects.get()
        services.conciliar(self.usuario, self.rodada.pk, movimento.pk, "Verificação anterior")
        segundo = self.ok(1, "8")
        self.assertEqual((segundo.esperado, segundo.sequencia_ok), (9, 1))
        self.assertFalse(services.posteriores(segundo).exists())
        self.assertEqual(services.movimentos_relevantes(segundo).count(), 1)
        with self.assertRaises(ValueError):
            services.revisar(self.usuario, segundo.pk, "Não pode presumir imobilidade")
        services.conciliar(self.usuario, segundo.pk, movimento.pk, "Saída física verificada entre as duas confirmações")
        services.revisar(self.usuario, segundo.pk, "Último corte físico verificado")
        self.assertTrue(EventoInventario.objects.filter(acao="conciliacao_anterior_preservada").exists())
        self.client.force_login(self.usuario)
        resposta = self.client.get(reverse("inventario:detalhe", args=[self.inventario.pk]))
        self.assertContains(resposta, "Entre confirmações individuais")
        self.assertContains(resposta, "Confirmação")

    def test_origem_desconhecida_entre_oks_continua_bloqueando_revisao(self):
        self.ok()
        Produto.objects.filter(pk=self.produto.pk).update(quantidade=11)
        segundo = self.ok(1, "8")
        with self.assertRaises(ValueError):
            services.revisar(self.usuario, segundo.pk, "Origem desconhecida")
        with self.assertRaises(ValueError):
            services.conciliar(self.usuario, segundo.pk, MovimentoObservado.objects.get().pk, "Não deve liberar")

    def test_finalizacao_exige_todos_oks_e_versao_atual_e_e_idempotente(self):
        with self.assertRaises(ValueError):
            self.finalizar()
        self.ok()
        with self.assertRaises(ValueError):
            services.finalizar_conferencia(self.acesso.pk, 0)
        versao = self.versao()
        primeiro = self.finalizar()
        repetido = services.finalizar_conferencia(self.acesso.pk, versao)
        self.assertEqual(primeiro.finalizada_em, repetido.finalizada_em)
        self.assertEqual(EventoInventario.objects.filter(acao="conferencia_finalizada").count(), 1)
        with self.assertRaises(ValueError):
            self.ok(0, "6")  # Even a formerly valid OK retry cannot write after submission.

    def test_envio_geral_nao_ignora_produtos_em_outro_acesso(self):
        outro = produto(nome="Outro produto", unidade_compra="UN", vende_fracionado=False)
        inventario = services.criar_inventario(self.usuario, "Dois acessos", "manual", [self.produto.pk, outro.pk])
        rodadas = list(RodadaContagem.objects.filter(item__inventario=inventario).order_by("pk"))
        a, _ = services.gerar_acesso(self.usuario, inventario.pk, self.funcionario.pk, [rodadas[0].pk])
        b, _ = services.gerar_acesso(self.usuario, inventario.pk, self.funcionario.pk, [rodadas[1].pk])
        services.salvar_contagem(a.pk, rodadas[0].pk, 0, "6", "", True)
        parcial = services.finalizar_conferencia(a.pk, Inventario.objects.get(pk=inventario.pk).versao)
        self.assertIsNone(parcial.finalizada_em)
        services.salvar_contagem(b.pk, rodadas[1].pk, 0, "8", "", True)
        parcial.refresh_from_db()
        self.assertIsNone(parcial.finalizada_em)
        services.finalizar_conferencia(b.pk, parcial.versao)
        with self.assertRaises(ValueError):
            services.salvar_contagem(b.pk, rodadas[1].pk, 1, "9", "", True)

    def test_revogacao_expiracao_e_inativacao_bloqueiam_novo_ok_e_envio(self):
        self.ok()
        from .models import AcessoInventario
        AcessoInventario.objects.filter(pk=self.acesso.pk).update(expira_em=timezone.now() - timedelta(seconds=1))
        with self.assertRaises(PermissionDenied):
            self.ok(1, "8")
        with self.assertRaises(PermissionDenied):
            self.finalizar()
        AcessoInventario.objects.filter(pk=self.acesso.pk).update(expira_em=timezone.now() + timedelta(hours=1))
        services.revogar_acesso(self.usuario, self.acesso.pk)
        with self.assertRaises(PermissionDenied):
            self.ok(1, "8")
        AcessoInventario.objects.filter(pk=self.acesso.pk).update(revogado_em=None)
        Funcionario.objects.filter(pk=self.funcionario.pk).update(ativo=False)
        with self.assertRaises(PermissionDenied):
            self.finalizar()

    def test_reabertura_administrativa_cria_novas_rodadas_e_nao_revive_links(self):
        self.ok()
        self.finalizar()
        sem_permissao = get_user_model().objects.create_user("sem-permissao-ciclo")
        with self.assertRaises(PermissionDenied):
            services.reabrir_conferencia(sem_permissao, self.inventario.pk, "Não autorizado")
        with self.assertRaises(ValueError):
            services.reabrir_conferencia(self.usuario, self.inventario.pk, "")
        reaberta = services.reabrir_conferencia(self.usuario, self.inventario.pk, "Reabertura autorizada")
        self.assertEqual(reaberta.ciclo, 2)
        with self.assertRaises(PermissionDenied):
            self.ok(1, "8")
        with self.assertRaises(ValueError):
            services.renovar_acesso(self.usuario, self.acesso.pk)
        nova = RodadaContagem.objects.get(item=self.rodada.item, numero=2)
        self.assertIsNone(nova.embalagens)
        novo, token = services.gerar_acesso(self.usuario, reaberta.pk, self.funcionario.pk, [nova.pk])
        services.salvar_contagem(novo.pk, nova.pk, 0, "8", "", True, token_hash=novo.token_hash)
        self.assertEqual(ConfirmacaoContagem.objects.count(), 2)
        self.assertTrue(EventoInventario.objects.filter(acao="conferencia_reaberta", usuario=self.usuario).exists())

    def test_nova_rodada_apos_finalizacao_tambem_reabre_com_auditoria(self):
        self.ok()
        self.finalizar()
        nova = services.recontar(self.usuario, self.rodada.pk, "Recontagem seletiva")
        self.inventario.refresh_from_db()
        self.assertIsNone(self.inventario.finalizada_em)
        self.assertEqual(self.inventario.ciclo, 2)
        self.assertEqual(nova.numero, 2)
        with self.assertRaises(PermissionDenied):
            self.ok(1, "8")

    def test_formulario_administrativo_reabre_com_auditoria_e_nega_link_limitado(self):
        self.ok()
        self.finalizar()
        detalhe = reverse("inventario:detalhe", args=[self.inventario.pk])
        self.client.post(reverse("inventario:convite", args=[self.token]))
        self.assertEqual(self.client.post(detalhe, {"acao": "reabrir_conferencia", "motivo": "Não autorizado"}).status_code, 403)
        gestor = Client(enforce_csrf_checks=True)
        gestor.force_login(self.usuario)
        pagina = gestor.get(detalhe)
        self.assertContains(pagina, "Reabrir conferência geral")
        self.assertNotContains(pagina, "Gerar acesso limitado")
        resposta = gestor.post(detalhe, {"acao": "reabrir_conferencia", "motivo": "Reabertura pelo responsável", "csrfmiddlewaretoken": gestor.cookies["csrftoken"].value})
        self.assertEqual(resposta.status_code, 302)
        self.inventario.refresh_from_db()
        self.assertEqual(self.inventario.ciclo, 2)
        self.assertIsNone(self.inventario.finalizada_em)
        self.assertTrue(EventoInventario.objects.filter(acao="conferencia_reaberta", usuario=self.usuario).exists())

    def test_http_csrf_cegueira_e_bloqueio_de_requisicoes_antigas(self):
        client = Client(enforce_csrf_checks=True)
        convite = reverse("inventario:convite", args=[self.token])
        client.get(convite)
        client.post(convite, {"csrfmiddlewaretoken": client.cookies["csrftoken"].value})
        client.get(reverse("inventario:mobile"))
        contar = reverse("inventario:contar", args=[self.rodada.pk])
        csrf = client.cookies["csrftoken"].value
        for versao, numero in [(0, "6"), (1, "8")]:
            resposta = client.post(contar, {"acao": "ok", "versao": versao, "embalagens": numero, "avulsos": "", "csrfmiddlewaretoken": csrf})
            self.assertEqual(resposta.status_code, 200)
            self.assertEqual(set(resposta.json()["produto"]), {"id", "nome", "codigo", "base", "secundaria", "fracionado", "passo", "passo_avulsos", "embalagens", "avulsos", "versao", "guardado", "conferido", "ok_em"})
        finalizar = reverse("inventario:finalizar")
        self.assertEqual(client.post(finalizar, {"versao": self.versao()}).status_code, 403)
        self.assertEqual(client.post(finalizar, {"versao": self.versao(), "csrfmiddlewaretoken": csrf}).status_code, 200)
        self.assertEqual(client.post(contar, {"acao": "ok", "versao": 2, "embalagens": "9", "csrfmiddlewaretoken": csrf}).status_code, 409)
        self.assertEqual(client.get(reverse("inventario:lista")).status_code, 403)
        resposta = client.get(reverse("inventario:mobile"))
        self.assertContains(resposta, "Conferência finalizada e enviada")
        self.assertNotContains(resposta, "Referência")


@skipUnless(connection.vendor == "postgresql", "Exige PostgreSQL descartável")
class CicloConcorrenciaTests(CicloFixture, TransactionTestCase):
    def executar(self, funcao):
        close_old_connections()
        try:
            return funcao()
        finally:
            close_old_connections()

    def disputar(self, primeira, segunda, acao):
        preso, liberar, iniciou, terminou = Event(), Event(), Event(), Event()
        original = services.evento
        def segurar(*args, **kwargs):
            if args[1] == acao:
                preso.set()
                if not liberar.wait(10):
                    raise AssertionError("Sem liberação")
            return original(*args, **kwargs)
        def disputar():
            iniciou.set()
            try:
                return segunda()
            except ValueError as exc:
                return str(exc)
            finally:
                terminou.set()
        with patch("inventario.services.evento", side_effect=segurar), ThreadPoolExecutor(2) as pool:
            a = pool.submit(self.executar, primeira)
            self.assertTrue(preso.wait(10))
            b = pool.submit(self.executar, disputar)
            try:
                self.assertTrue(iniciou.wait(10))
                self.assertFalse(terminou.wait(.2))
            finally:
                liberar.set()
            return a.result(15), b.result(15)

    def test_correcao_em_curso_invalida_finalizacao_de_outra_aba(self):
        self.ok()
        _, erro = self.disputar(lambda: self.ok(1, "8"), lambda: services.finalizar_conferencia(self.acesso.pk, 1), "produto_ok")
        self.assertIn("mudaram", erro)
        self.inventario.refresh_from_db()
        self.assertIsNone(self.inventario.finalizada_em)
        self.finalizar()

    def test_envio_em_curso_bloqueia_correcao_em_outra_aba(self):
        self.ok()
        _, erro = self.disputar(self.finalizar, lambda: self.ok(1, "8"), "conferencia_finalizada")
        self.assertIn("finalizada", erro)
        self.assertEqual(ConfirmacaoContagem.objects.count(), 1)
        self.rodada.refresh_from_db()
        self.assertEqual(self.rodada.contado_base, 6)

    def test_novo_ok_aguarda_venda_e_captura_nova_referencia(self):
        self.ok()
        preso, liberar, iniciou, terminou = Event(), Event(), Event(), Event()
        def venda():
            with transaction.atomic():
                _baixar_estoque_produto(self.produto.pk, 1, unidade="UN")
                preso.set()
                if not liberar.wait(10):
                    raise AssertionError("Sem liberação")
        def confirmar():
            iniciou.set()
            try:
                return self.ok(1, "8")
            finally:
                terminou.set()
        with ThreadPoolExecutor(2) as pool:
            a = pool.submit(self.executar, venda)
            self.assertTrue(preso.wait(10))
            b = pool.submit(self.executar, confirmar)
            try:
                self.assertTrue(iniciou.wait(10))
                self.assertFalse(terminou.wait(.2))
            finally:
                liberar.set()
            a.result(15)
            rodada = b.result(15)
        self.assertEqual((rodada.esperado, rodada.sequencia_ok), (9, 1))
        self.assertEqual(ConfirmacaoContagem.objects.count(), 2)


@skipUnless(Path(tests_browser.CHROME).is_file(), "Chrome indisponível")
class CicloBrowserTests(CicloFixture, StaticLiveServerTestCase):
    abrir = tests_browser.InventarioBrowserTests.abrir

    def test_adocante_corrigir_apos_ok_envio_perda_resposta_e_reabertura(self):
        with TemporaryDirectory(prefix="inventario-chrome-ciclo-") as profile:
            chrome = tests_browser.Chrome(tests_browser.CHROME, profile).start()
            try:
                tab = chrome.tab()
                self.abrir(tab)
                tab.evaluate("document.querySelector('[name=embalagens]').value='6';document.querySelector('[value=ok]').click();true")
                tab.wait("document.querySelector('form.contagem [name=versao]').value === '1'")
                self.assertTrue(tab.evaluate("!!document.querySelector('article.produto-conferido') && document.querySelector('.estado-produto').textContent === 'Conferido' && document.querySelector('.quantidade-confirmada').textContent.includes('6 UN')"))
                self.assertTrue(tab.evaluate("document.documentElement.scrollWidth <= innerWidth"))
                tests_browser.capturar(tab, 'ok-01-conferido-390.png')
                self.assertFalse(tab.evaluate("document.querySelector('form.contagem fieldset').disabled"))
                tab.evaluate("document.querySelector('[name=embalagens]').value='8';document.querySelector('[name=embalagens]').dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('#finalizar-conferencia button').click();true")
                tab.wait("document.querySelector('.mensagem-finalizacao').textContent.includes('Confirme o OK')")
                self.assertTrue(tab.evaluate("!!document.querySelector('article.produto-pendente') && !document.querySelector('article.produto-conferido') && document.querySelector('.estado-produto').textContent === 'Alteração pendente de confirmação' && document.querySelector('[value=ok]').textContent === 'Atualizar OK' && document.querySelector('.quantidade-confirmada').textContent.includes('6 UN')"))
                tests_browser.capturar(tab, 'ok-02-pendente-390.png')
                self.inventario.refresh_from_db()
                self.assertIsNone(self.inventario.finalizada_em)
                tab.evaluate("document.querySelector('[value=ok]').click();true")
                tab.wait("document.querySelector('form.contagem [name=versao]').value === '2'")
                self.assertTrue(tab.evaluate("!!document.querySelector('article.produto-conferido') && document.querySelector('.estado-produto').textContent === 'Conferido' && document.querySelector('.quantidade-confirmada').textContent.includes('8 UN')"))
                tests_browser.capturar(tab, 'ok-03-atualizado-390.png')
                tab.call('Emulation.setDeviceMetricsOverride', {'width': 1280, 'height': 900, 'deviceScaleFactor': 1, 'mobile': False})
                self.assertTrue(tab.evaluate("document.documentElement.scrollWidth <= innerWidth"))
                tests_browser.capturar(tab, 'ok-04-desktop.png')
                self.assertEqual(list(ConfirmacaoContagem.objects.order_by('pk').values_list('contado_base', flat=True)), [6, 8])
                self.assertFalse(tab.evaluate("/esperado|custo_base|sequencia_ok|preco_compra/.test(document.documentElement.outerHTML)"))
                tab.evaluate("window.fetchOriginal=fetch;window.fetch=async(...args)=>{const r=await fetchOriginal(...args);if(String(args[0]).endsWith('/finalizar/'))throw new TypeError('Resposta perdida');return r};document.querySelector('#finalizar-conferencia button').click();true")
                tab.wait("document.querySelector('.mensagem-finalizacao').textContent.includes('Sem confirmação')")
                self.inventario.refresh_from_db()
                self.assertIsNotNone(self.inventario.finalizada_em)
                tab.call('Page.reload')
                tab.wait("document.querySelector('form.contagem fieldset')?.disabled === true")
                self.assertTrue(tab.evaluate("!!document.querySelector('article.produto-conferido') && document.querySelector('.quantidade-confirmada').textContent.includes('8 UN')"))
                tests_browser.capturar(tab, 'ok-05-enviado.png')
                self.assertFalse(tab.evaluate("!!document.querySelector('#finalizar-conferencia button')"))
                url = json.dumps(reverse('inventario:contar', args=[self.rodada.pk]))
                status = tab.evaluate("(async()=>{const t=document.querySelector('[name=csrfmiddlewaretoken]').value;const r=await fetch(" + url + ",{method:'POST',body:new URLSearchParams({acao:'ok',versao:'2',embalagens:'9',csrfmiddlewaretoken:t})});return r.status})()")
                self.assertEqual(status, 409)
                self.assertEqual(EventoInventario.objects.filter(acao='conferencia_finalizada').count(), 1)
                services.reabrir_conferencia(self.usuario, self.inventario.pk, 'Nova verificação autorizada')
                tab.call('Page.reload')
                tab.wait("document.querySelector('#estado-conferencia').textContent.includes('ciclo anterior')")
                self.assertFalse(tab.evaluate("!!document.querySelector('form.contagem')"))
                nova = RodadaContagem.objects.get(item=self.rodada.item, numero=2)
                novo, token = services.gerar_acesso(self.usuario, self.inventario.pk, self.funcionario.pk, [nova.pk])
                self.token = token
                self.abrir(tab)
                self.assertEqual(tab.evaluate("document.querySelector('[name=embalagens]').value"), '')
                self.assertFalse(tab.evaluate("document.querySelector('fieldset').disabled"))
                self.produto.refresh_from_db()
                self.assertEqual(self.produto.quantidade, 10)
                self.assertFalse(MovimentoObservado.objects.exists())
                self.assertFalse(MovimentacaoEstoqueManual.objects.exists())
            finally:
                chrome.stop()
