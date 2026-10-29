"""Presentation checks with synthetic stock; no adjustment is applied."""
from unittest.mock import patch

from django.test import TestCase

from estoque.models import Produto
from . import services
from .models import AplicacaoAjuste, ConfirmacaoContagem
from .tests_ajustes_modal import Fixture


class RevisaoApresentacaoTests(Fixture, TestCase):
    def conferir_sem_escrita(self):
        saldos = list(Produto.objects.order_by('pk').values())
        oks = list(ConfirmacaoContagem.objects.order_by('pk').values())
        payload, dados = self.prever()
        self.assertEqual(saldos, list(Produto.objects.order_by('pk').values()))
        self.assertEqual(oks, list(ConfirmacaoContagem.objects.order_by('pk').values()))
        self.assertFalse(AplicacaoAjuste.objects.exists())
        return payload, dados

    def test_sem_divergencias_finalizada_somente_fechar(self):
        self.inv = services.criar_inventario(self.usuario, 'Sem diferenças fictícia', 'manual', [self.produto.pk])
        self.confirmar(self.inv, 10)
        from django.urls import reverse
        self.modal = reverse('inventario:ajustes_modal', args=[self.inv.pk])
        self.detalhe = reverse('inventario:detalhe', args=[self.inv.pk])
        self.finalizar()
        p, dados = self.conferir_sem_escrita()
        self.assertTrue(p['resumo']['sem_divergencias'])
        self.assertIn('Conferência revisada — nenhum ajuste necessário. Todos os produtos conferidos estão corretos.', p['html'])
        self.assertIn('>Fechar</button>', p['html'])
        self.assertNotIn('previa', dados)
        self.assertNotIn('<form', p['html'])
        pagina = self.client.get(self.detalhe)
        self.assertContains(pagina, 'id="revisar-atualizacao"')
        self.assertContains(pagina, 'Revisar ajustes de estoque')
        html = pagina.content.decode()
        self.assertLess(html.index('</tbody></table>'), html.index('data-ajustes-diretos'))
        self.assertLess(html.index('data-ajustes-diretos'), html.index('Investigar produtos'))
        self.assertNotIn('A conferência mudou.', html)
        self.inv.refresh_from_db()
        self.assertIsNotNone(self.inv.finalizada_em)

    def test_divergencia_preserva_confirmacao(self):
        p, dados = self.conferir_sem_escrita()
        self.assertEqual(p['resumo']['aptos'], 1)
        self.assertFalse(p['resumo']['sem_divergencias'])
        self.assertFalse(p['resumo']['concluidos'])
        self.assertTrue(dados['previa'])
        self.assertIn('Confirmar atualização', p['html'])

    def test_bloqueio_preserva_motivo_sem_confirmacao(self):
        from uuid import uuid4
        services.sinalizar_fisico(self.rodada.pk, 'Ocorrência fictícia sem documento', str(uuid4()), usuario=self.usuario)
        p, dados = self.conferir_sem_escrita()
        self.assertEqual(len(p['resumo']['bloqueados']), 1)
        self.assertFalse(p['resumo']['sem_divergencias'])
        self.assertFalse(p['resumo']['concluidos'])
        self.assertIn('data-bloqueados-modal', p['html'])
        self.assertNotIn('previa', dados)

    def test_resultado_aplicado_somente_consulta(self):
        # Application belongs to the PostgreSQL suite; here only its read projection is supplied.
        resultado = dict(linhas=[dict(estado='Aplicado', diferenca=-2)], aptas=[],
                         resumo=dict(aplicados=1), erro='')
        with patch('inventario.ajustes_modal.ajustes.preparar_resultado', return_value=resultado):
            p, dados = self.conferir_sem_escrita()
        self.assertTrue(p['resumo']['concluidos'])
        self.assertFalse(p['resumo']['sem_divergencias'])
        self.assertIn('Todos os ajustes de estoque foram concluídos.', p['html'])
        self.assertNotIn('<form', p['html'])
        self.assertNotIn('previa', dados)

    def test_diagnostico_versao_antiga_apos_finalizacao(self):
        self.inv.refresh_from_db()
        anterior = self.inv.versao
        self.finalizar()
        with self.assertRaisesMessage(ValueError, 'A conferência mudou. Atualize a página antes de distribuir os produtos.'):
            services.distribuir_participantes(self.usuario, self.inv.pk, [], {}, 24, anterior)
        with self.assertRaisesMessage(ValueError, 'Conferência finalizada. A seleção histórica será preservada.'):
            services.distribuir_participantes(self.usuario, self.inv.pk, [], {}, 24, self.inv.versao)
