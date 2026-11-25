from datetime import timedelta
from django.test import TestCase
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from .tests import produto
from . import services
from .selecao import conferidos_recentes


class SelecaoRecenteTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser('selecao', '', 'teste')
        self.p = produto(quantidade=0)
        self.inv = services.criar_inventario(self.user, criterio='manual', selecionados=[self.p.pk])
        self.r = self.inv.itens.get().rodadas.get()
        self.agora = timezone.now()
        self.r.ok_em = self.agora - timedelta(hours=1)
        self.r.contado_base = self.r.esperado = 0
        self.r.save()
        self.inv.finalizada_em = self.agora
        self.inv.save()
        self.client.force_login(self.user)

    def test_concluida_zero_e_limite_24h(self):
        self.assertIn(self.p.pk, conferidos_recentes([self.p.pk], self.agora))
        self.r.ok_em = self.agora - timedelta(hours=24, seconds=1)
        self.r.save()
        self.assertEqual(conferidos_recentes([self.p.pk], self.agora), {})

    def test_rascunho_e_divergencia_nao_dispensam(self):
        self.inv.finalizada_em = None
        self.inv.save()
        self.assertEqual(conferidos_recentes([self.p.pk]), {})
        self.inv.finalizada_em = self.agora
        self.inv.save()
        self.r.contado_base = 1
        self.r.save()
        self.assertEqual(conferidos_recentes([self.p.pk]), {})

    def test_outra_divergencia_bloqueia(self):
        outra = services.criar_inventario(self.user, criterio='manual', selecionados=[self.p.pk])
        rodada = outra.itens.get().rodadas.get()
        rodada.ok_em = self.agora
        rodada.contado_base = 1
        rodada.esperado = 0
        rodada.save()
        self.assertEqual(conferidos_recentes([self.p.pk]), {})

    def test_selecao_separa_sem_reaproveitar_quantidades(self):
        nova = services.criar_inventario(self.user, criterio='categoria', selecionados=self.p.categoria)
        pagina = self.client.get(reverse('inventario:detalhe', args=[nova.pk]))
        self.assertEqual(pagina.status_code, 200)
        linha = pagina.context['linhas'][0]
        self.assertIsNotNone(linha['conferido_recente'])
        self.assertFalse(linha['participa_marcado'])
        self.assertContains(pagina, 'Incluir na contagem')
        self.assertContains(pagina, 'Estoque registrado:')
        rodada = nova.itens.get().rodadas.get()
        self.assertIsNone(rodada.contado_base)
        self.p.refresh_from_db()
        self.assertEqual(self.p.quantidade, 0)

    def test_lista_manual_mostra_saldo_e_recente(self):
        pagina = self.client.get(reverse('inventario:lista'))
        self.assertContains(pagina, 'Estoque registrado:')
        self.assertContains(pagina, 'Incluir na contagem')

    def test_cancelada_nao_conta(self):
        self.inv.cancelada_em = self.agora
        self.inv.save()
        self.assertEqual(conferidos_recentes([self.p.pk]), {})

    def test_recontagem_pendente_nao_reaproveita_ok_antigo(self):
        from .models import RodadaContagem
        RodadaContagem.objects.create(item=self.r.item, numero=2)
        self.assertEqual(conferidos_recentes([self.p.pk]), {})

    def test_consulta_unica_e_sem_escrita(self):
        with self.assertNumQueries(1):
            self.assertIn(self.p.pk, conferidos_recentes([self.p.pk], self.agora))

    def test_fornecedor_independente_e_preserva_protegidos(self):
        from estoque.models import Fornecedor, ProdutoFornecedor, Funcionario
        f = Fornecedor.objects.create(nome='Outro fornecedor')
        ProdutoFornecedor.objects.create(produto=self.p, fornecedor=f)
        nova = services.criar_inventario(self.user, criterio='fornecedor', selecionados=f.pk)
        url = reverse('inventario:detalhe', args=[nova.pk])
        self.assertIsNotNone(self.client.get(url).context['linhas'][0]['conferido_recente'])
        funcionario = Funcionario.objects.create(nome='Contador')
        rodada = nova.itens.get().rodadas.get()
        services.gerar_acesso(self.user, nova.pk, funcionario.pk, [rodada.pk])
        linha = self.client.get(url).context['linhas'][0]
        self.assertTrue(linha['participante_protegido'])
        self.assertIsNone(linha['conferido_recente'])

    def test_erro_preserva_reinclusao_explicita(self):
        nova = services.criar_inventario(self.user, criterio='manual', selecionados=[self.p.pk])
        rodada = nova.itens.get().rodadas.get()
        resposta = self.client.post(reverse('inventario:detalhe', args=[nova.pk]), {
            'acao': 'acesso', 'versao': nova.versao, 'rodadas': [rodada.pk], 'horas': 24,
        })
        self.assertEqual(resposta.status_code, 200)
        linha = resposta.context['linhas'][0]
        self.assertTrue(linha['participa_marcado'])
        self.assertIsNone(linha['conferido_recente'])

    def test_salvar_participantes_dispensa_recente_sem_apagar_historico(self):
        from estoque.models import Funcionario
        from .selecao import itens_incluidos
        outro = produto(nome='Ainda nao contado', quantidade=0)
        nova = services.criar_inventario(self.user, criterio='manual', selecionados=[self.p.pk, outro.pk])
        rodada = nova.itens.get(produto=outro).rodadas.get()
        funcionario = Funcionario.objects.create(nome='Funcionario teste')
        services.distribuir_participantes(self.user, nova.pk, [rodada.pk],
            {str(rodada.pk): funcionario.pk}, 24, nova.versao)
        self.assertEqual(list(itens_incluidos(nova).values_list('produto_id', flat=True)), [outro.pk])
        self.assertEqual(nova.itens.count(), 2)
        self.r.refresh_from_db()
        self.assertEqual(self.r.contado_base, 0)
        self.p.refresh_from_db()
        self.assertEqual(self.p.quantidade, 0)
