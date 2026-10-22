"""Single confirmation using synthetic data on the disposable PostgreSQL cluster."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch
from django.test import TransactionTestCase, override_settings
from django.db import close_old_connections
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from estoque.models import MovimentacaoEstoqueManual
from estoque.services.estoque_manual import conferir_ou_ajustar_estoque
from . import ajustes
from .models import PropostaAjuste, AplicacaoAjuste, EventoAjuste
from .tests_ajustes import AjustesFixture


@override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
class ConfirmacaoUnicaTests(AjustesFixture, TransactionTestCase):
    def previa(self, rodadas=None):
        return ajustes.previa_atualizacao(self.inv.pk, [self.avaliar(r) for r in (rodadas or [self.rodada])])

    def aplicar(self, previa=None, **kwargs):
        return ajustes.atualizar(self.usuario, self.inv.pk, previa or self.previa(), confirmacao=True, **kwargs)

    def test_individual_sem_justificativa_idempotencia_auditoria(self):
        previa = self.previa()
        self.aplicar(previa)
        self.aplicar(previa)
        self.assertEqual(self.fresca().item.produto.quantidade, 8)
        self.assertEqual(MovimentacaoEstoqueManual.objects.count(), 1)
        self.assertEqual(PropostaAjuste.objects.count(), 1)
        self.assertEqual(AplicacaoAjuste.objects.count(), 1)
        self.assertEqual(EventoAjuste.objects.count(), 3)
        evento = EventoAjuste.objects.get(acao='confirmado_gestor')
        self.assertEqual(evento.usuario_id, self.usuario.pk)
        self.assertEqual(evento.dados['confirmacao_fisica'], self.rodada.confirmacoes.get().pk)
        self.assertIsNotNone(evento.criado_em)

    def test_lote_observacao_e_repeticao(self):
        r2 = self.segunda()
        previa = self.previa([self.rodada, r2])
        self.aplicar(previa, observacao='Contagem conferida')
        self.aplicar(previa)
        self.assertEqual(AplicacaoAjuste.objects.count(), 2)
        self.assertEqual(len(set(AplicacaoAjuste.objects.values_list('lote', flat=True))), 1)
        self.assertTrue(all(p.justificativa == 'Contagem conferida' for p in PropostaAjuste.objects.all()))

    def test_rollback_inclui_aprovacao_e_confirmacao(self):
        r2 = self.segunda()
        previa = self.previa([self.rodada, r2])
        # Multi-product updates use the existing batch writer. Inject the
        # failure in that real path, after stock writes and before commit.
        from estoque.services import estoque_manual_lote
        real = estoque_manual_lote.conferir_movimento
        n = 0
        def falhar(*args, **kwargs):
            nonlocal n
            n += 1
            if n == 2:
                raise ValueError('Falha simulada')
            return real(*args, **kwargs)
        with patch.object(estoque_manual_lote, 'conferir_movimento', side_effect=falhar):
            with self.assertRaisesMessage(ValueError, 'Falha simulada'):
                self.aplicar(previa)
        self.assertFalse(PropostaAjuste.objects.exists())
        self.assertFalse(EventoAjuste.objects.exists())
        self.assertFalse(AplicacaoAjuste.objects.exists())
        self.assertFalse(MovimentacaoEstoqueManual.objects.exists())
        self.assertEqual(self.fresca().item.produto.quantidade, 10)

    def test_movimento_posterior_invalida_resumo_sem_aprovacao_persistida(self):
        previa = self.previa()
        conferir_ou_ajustar_estoque(self.produto.pk, MovimentacaoEstoqueManual.TIPO_AJUSTE,
            novo_estoque=11, motivo='Movimento fictício', usuario=self.usuario)
        with self.assertRaises(ValueError):
            self.aplicar(previa)
        self.assertFalse(PropostaAjuste.objects.exists())
        self.assertEqual(self.fresca().item.produto.quantidade, 11)

    def test_flag_permissao_confirmacao_e_token(self):
        previa = self.previa()
        with override_settings(INVENTARIO_AJUSTES_HABILITADOS=False):
            with self.assertRaisesMessage(ValueError, 'desabilitada'):
                self.aplicar(previa)
        with self.assertRaises(ValueError):
            ajustes.atualizar(self.usuario, self.inv.pk, previa)
        with self.assertRaises(ValueError):
            self.aplicar(previa+'x')
        with self.assertRaises(ValueError):
            self.aplicar(previa, observacao='x'*4001)
        with self.assertRaises(PermissionDenied):
            ajustes.atualizar(get_user_model().objects.create_user('intruso'), self.inv.pk, previa, True)
        self.assertFalse(PropostaAjuste.objects.exists())

    def test_concorrencia_duas_confirmacoes_uma_atualizacao(self):
        previa = self.previa()
        barreira = Barrier(2)
        def executar():
            close_old_connections()
            try:
                barreira.wait(timeout=10)
                return self.aplicar(previa)[0].pk
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            resultados = list(pool.map(lambda _: executar(), range(2)))
        self.assertEqual(resultados[0], resultados[1])
        self.assertEqual(AplicacaoAjuste.objects.count(), 1)
        self.assertEqual(EventoAjuste.objects.count(), 3)

    def test_http_sem_etapa_intermediaria_e_lote_somente_aptos(self):
        self.segunda()
        pagina = self.client.get(self.url, {'confirmar': 'todos'})
        self.assertEqual(len(pagina.context['confirmacoes']), 2)
        self.assertContains(pagina, 'Adicionar observação')
        self.assertNotContains(pagina, 'type="checkbox"')
        self.assertNotContains(pagina, 'required maxlength="4000"')
        resposta = self.client.post(self.url, dict(acao='atualizar',
            previa=pagina.context['token_aplicacao'], confirmacao='sim'))
        self.assertEqual(resposta.status_code, 302)
        self.assertEqual(AplicacaoAjuste.objects.count(), 2)

    def test_sem_diferenca_e_bloqueados_fora_do_lote(self):
        r2 = self.segunda()
        from .models import RodadaContagem, ConfirmacaoContagem
        valores = dict(contado=20, contado_base=20, embalagens=20)
        RodadaContagem.objects.filter(pk=r2.pk).update(**valores)
        ConfirmacaoContagem.objects.filter(rodada=r2).update(**valores)
        pagina = self.client.get(self.url, {'confirmar': 'todos'})
        self.assertEqual(len(pagina.context['confirmacoes']), 1)
        self.aplicar(pagina.context['token_aplicacao'])
        self.assertEqual(AplicacaoAjuste.objects.count(), 1)
        self.assertEqual(self.fresca(r2.pk).item.produto.quantidade, 20)

    def test_rejeitada_bloqueada_e_selecao_parcial_nao_reaplica(self):
        r2 = self.segunda()
        previa = self.previa([self.rodada, r2])
        self.aplicar(self.previa())
        with self.assertRaisesMessage(ValueError, 'parcialmente'):
            self.aplicar(previa)
        self.assertEqual(AplicacaoAjuste.objects.count(), 1)
        a = self.avaliar(r2)
        ajustes.decidir(self.usuario, self.inv.pk, r2.pk, a['token'], 'Rejeição fictícia',
            aprovar=False, confirmacao=True)
        pagina = self.client.get(self.url, {'confirmar': 'todos'})
        self.assertFalse(pagina.context['confirmacoes'])

    def test_conferencia_um_conciliacao_venda_saldo_atual_mais_diferenca(self):
        from .tests_conciliacao_documental import ConciliacaoDocumentalTests
        from estoque.models import ItemVenda
        ConciliacaoDocumentalTests.preparar_conferencia_um(self)
        venda, item, movimento = ConciliacaoDocumentalTests.vender(self)
        documentos_antes = list(ItemVenda.objects.filter(pk=item.pk).values())
        previa = self.previa()
        resultado = self.aplicar(previa)[0]
        self.assertEqual((resultado.antes, resultado.diferenca, resultado.depois), (4, -4, 0))
        self.assertEqual(resultado.proposta.evidencia['documentos'][0]['venda'], venda.pk)
        self.assertEqual(documentos_antes, list(ItemVenda.objects.filter(pk=item.pk).values()))
        self.assertEqual(self.fresca().contado_base, 6)

    def test_evidencia_insuficiente_bloqueia_individualmente(self):
        r2 = self.segunda()
        from .models import ConfirmacaoContagem
        ConfirmacaoContagem.objects.filter(rodada=r2).delete()
        pagina = self.client.get(self.url, {'confirmar': 'todos'})
        self.assertEqual(pagina.context['resumo']['bloqueados'], 1)
        self.assertEqual(len(pagina.context['confirmacoes']), 1)
        self.aplicar(pagina.context['token_aplicacao'])
        self.assertEqual(AplicacaoAjuste.objects.count(), 1)
        self.assertEqual(self.fresca(r2.pk).item.produto.quantidade, 20)
