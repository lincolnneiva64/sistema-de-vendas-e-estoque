"""Explicit recognition using synthetic records in disposable PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from django.core.exceptions import PermissionDenied
from django.db import connection, connections, close_old_connections
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from estoque.models import Produto, Venda, ItemVenda, EventoVenda, ItemVendaRemovido
from . import ajustes, remocoes_legadas, views
from .contexto_ajustes import carregar
from .models import EventoInventario, ConfirmacaoContagem, MovimentoObservado, RodadaContagem, Inventario
from .tests_ajustes import AjustesFixture
from .tests_conciliacao_remocoes import ConciliacaoRemocoesTests


class Fixture(AjustesFixture):
    vender = ConciliacaoRemocoesTests.vender
    preparar = ConciliacaoRemocoesTests.preparar

    def legado(self):
        venda, registro, evento = self.preparar()
        EventoVenda.objects.filter(pk=evento.pk).update(descricao=evento.descricao.split(' Registro')[0])
        evento.refresh_from_db()
        self.venda, self.registro, self.evento = venda, registro, evento
        r = self.fresca()
        with carregar([r]) as lote:
            sugestao = remocoes_legadas.sugestoes(r, lote)[0]
        self.previa = sugestao['previa']
        return sugestao

    def reconhecer(self, **campos):
        dados = dict(usuario=self.usuario, inventario_id=self.inv.pk, rodada_id=self.rodada.pk,
            previa=self.previa, registro_id=self.registro.pk,
            documento='Documento externo fictício A-123, item original 999001',
            evidencia='Registro independente fictício identifica a venda, o produto e o item original removido.',
            motivo='Reconhecimento explícito da atribuição legada.', confirmacao=True)
        dados.update(campos)
        return remocoes_legadas.reconhecer(**dados)


class ReconhecimentoTests(Fixture, TestCase):
    def test_legado_bloqueado_ate_decisao_explicita_e_auditoria_sem_mudar_fontes(self):
        self.legado()
        self.assertTrue(ajustes.avaliar(self.fresca())['bloqueio'])
        modelos = (Produto, Venda, ItemVenda, EventoVenda, ItemVendaRemovido,
                   ConfirmacaoContagem, MovimentoObservado, RodadaContagem, Inventario)
        antes = {m: list(m.objects.order_by('pk').values()) for m in modelos}
        auditoria = self.reconhecer()
        self.assertEqual(auditoria.usuario_id, self.usuario.pk)
        self.assertIsNotNone(auditoria.criado_em)
        self.assertEqual(auditoria.dados['reconhecimento']['registro']['id'], self.registro.pk)
        self.assertEqual(auditoria.dados['reconhecimento']['evento']['pk'], self.evento.pk)
        self.assertFalse(ajustes.avaliar(self.fresca())['bloqueio'])
        self.assertEqual(self.reconhecer().pk, auditoria.pk)
        self.assertEqual(EventoInventario.objects.filter(acao=remocoes_legadas.ACAO).count(), 1)
        for modelo in modelos:
            self.assertEqual(list(modelo.objects.order_by('pk').values()), antes[modelo])

    def test_documento_evidencia_motivo_confirmacao_e_permissao_obrigatorios(self):
        self.legado()
        for campo in ('documento', 'evidencia', 'motivo', 'confirmacao'):
            with self.subTest(campo=campo), self.assertRaises(ValueError):
                self.reconhecer(**{campo: ''})
        from django.contrib.auth import get_user_model
        sem_permissao = get_user_model().objects.create_user('consulta-legada')
        with self.assertRaises(PermissionDenied):
            self.reconhecer(usuario=sem_permissao)
        self.assertFalse(EventoInventario.objects.filter(acao=remocoes_legadas.ACAO).exists())

    def test_registro_ausente_ou_alterado_nao_e_aceito(self):
        self.legado()
        ItemVendaRemovido.objects.filter(pk=self.registro.pk).update(valor_total_snapshot=15)
        with self.assertRaisesMessage(ValueError, 'alterados desde a prévia'):
            self.reconhecer()

    def test_venda_cancelada_ou_evento_ambiguo_desfaz_reconhecimento(self):
        self.legado()
        from django.db import transaction
        for tipo in ('cancelada', 'evento'):
            with self.subTest(tipo=tipo), transaction.atomic():
                if tipo == 'cancelada': Venda.objects.filter(pk=self.venda.pk).update(cancelada=True)
                else: EventoVenda.objects.create(venda=self.venda, tipo_evento='desconhecido', descricao='Sem atribuição')
                with self.assertRaises(ValueError): self.reconhecer()
                self.assertFalse(EventoInventario.objects.filter(acao=remocoes_legadas.ACAO).exists())
                transaction.set_rollback(True)

    def test_fonte_alterada_ou_auditoria_adulterada_volta_a_bloquear(self):
        self.legado()
        auditoria = self.reconhecer()
        dados = auditoria.dados
        dados['reconhecimento']['documento'] = 'Documento substituído'
        EventoInventario.objects.filter(pk=auditoria.pk).update(dados=dados)
        self.assertTrue(ajustes.avaliar(self.fresca())['bloqueio'])

    def test_conferencia_finalizada_http_csrf_e_escopo(self):
        self.legado()
        Inventario.objects.filter(pk=self.inv.pk).update(finalizada_em=timezone.now())
        url = reverse('inventario:detalhe', args=[self.inv.pk])
        self.assertContains(self.client.get(url), 'Reconhecer remoção antiga')
        dados = dict(acao='reconhecer_remocao_legada', rodada=self.rodada.pk, previa=self.previa,
            registro=self.registro.pk, documento='Documento externo A123', evidencia='Ligação independente entre item original e remoção.',
            motivo='Decisão administrativa explícita', confirmacao='sim')
        from django.test import Client
        protegido = Client(enforce_csrf_checks=True)
        protegido.force_login(self.usuario)
        self.assertEqual(protegido.post(url, dados).status_code, 403)
        resposta = self.client.post(url, dados)
        self.assertEqual(resposta.status_code, 302)
        self.assertEqual(EventoInventario.objects.filter(acao=remocoes_legadas.ACAO).count(), 1)

    def test_consultas_de_validacao_continuam_em_lote(self):
        self.legado()
        self.reconhecer()
        r = self.fresca()
        with carregar([r]) as lote, CaptureQueriesContext(connection) as queries:
            self.assertFalse(ajustes.avaliar(r, _lote=lote)['bloqueio'])
            self.assertEqual(remocoes_legadas.sugestoes(r, lote), [])
        self.assertEqual(len(queries), 0)

    def test_futura_remocao_ja_tem_id_sem_reconhecimento(self):
        self.preparar()
        self.assertFalse(ajustes.avaliar(self.fresca())['bloqueio'])
        self.assertFalse(EventoInventario.objects.filter(acao=remocoes_legadas.ACAO).exists())


class ReconhecimentoConcorrenciaTests(Fixture, TransactionTestCase):
    def test_dois_envios_criam_um_unico_reconhecimento(self):
        self.legado()
        barreira = Barrier(2)
        def executar():
            close_old_connections()
            try:
                barreira.wait(10)
                return self.reconhecer().pk
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = [pool.submit(executar) for _ in range(2)]
            self.assertEqual(a.result(25), b.result(25))
        self.assertEqual(EventoInventario.objects.filter(acao=remocoes_legadas.ACAO).count(), 1)
