"""Physical counting only, with the existing package-only product fixture."""
from decimal import Decimal as D
from django.test import TestCase, Client
from django.urls import reverse
from estoque.models import Produto
from . import services, contagem_admin
from .conversao import converter, snapshot
from .conversao_fisica import cadastro_contagem, representacao_fisica
from .models import RodadaContagem, ConfirmacaoContagem, AplicacaoAjuste
from .tests_avulsos_fisicos import AvulsosFixture


class ContagemFracoesTests(AvulsosFixture, TestCase):
    def configurar(self, fator):
        self.produto.fator_conversao = fator
        self.produto.save(update_fields=['fator_conversao'])
        self.produto.refresh_from_db()
        item = self.rodada.item
        item.cadastro = snapshot(self.produto)
        item.save(update_fields=['cadastro'])
        self.rodada = RodadaContagem.objects.select_related('item').get(pk=self.rodada.pk)
        return item.cadastro

    def test_fatores_inteiros_fracoes_avulsas_zero_e_soma(self):
        for fator in (6, 12, 24):
            cadastro = self.configurar(fator)
            for p, a, unidades in [('2', '7', 2*fator+7), ('0.5', '0', fator//2),
                    ('0.5', '7', fator//2+7), ('0', '0', 0), ('0', '7', 7)]:
                with self.subTest(fator=fator, p=p, a=a):
                    principal, avulso, _, base = converter(cadastro, p, a)
                    self.assertEqual((principal, avulso), (D(p), D(a)))
                    self.assertEqual(principal*D(fator)+avulso, unidades)
                    self.assertEqual(base, (D(unidades)/D(fator)).quantize(D('.001')))
            self.assertFalse(self.produto.vende_fracionado)
        self.assertEqual(converter(self.configurar(12), '2', '7')[2], D('2.583333333'))

    def test_invalidos_sem_arredondar_unidade_inteira(self):
        for fator in (6, 12, 24):
            cadastro = self.configurar(fator)
            for p, a in [('.001', '0'), ('0.333', '0'), ('0.5', '.5'),
                    ('-1', '0'), ('NaN', '0'), ('Infinity', '0'), ('', '0'),
                    ('1.0001', '0'), ('1000000000', '0'), ('1', '-1')]:
                with self.subTest(fator=fator, p=p, a=a), self.assertRaises(ValueError):
                    converter(cadastro, p, a)
        self.assertEqual(converter(self.configurar(12), '.25', '7')[2], D('0.833333333'))
        self.assertEqual(converter(self.configurar(24), '.125', '7')[2], D('0.416666667'))

    def test_sem_fator_congelado_nao_infere_do_nome_ou_cadastro_atual(self):
        cadastro = self.configurar(12)
        antigo = dict(cadastro, secundaria='')
        antigo.pop('avulsos_fisicos')
        with self.assertRaises(ValueError):
            converter(antigo, '.5', '7')
        self.produto.fator_conversao = 24
        self.produto.save(update_fields=['fator_conversao'])
        self.assertEqual(converter(cadastro, '.5', '7')[2], D('1.083333333'))

    def test_confirmacao_mobile_idempotente_sem_alterar_produto(self):
        self.configurar(12)
        antes = Produto.objects.values().get(pk=self.produto.pk)
        client = Client()
        convite = reverse('inventario:convite', args=[self.token])
        client.get(convite)
        client.post(convite)
        url = reverse('inventario:contar', args=[self.rodada.pk])
        dados = dict(versao=0, embalagens='0.5', avulsos='7', acao='ok')
        for _ in range(2):
            resposta = client.post(url, dados)
            self.assertEqual(resposta.status_code, 200, resposta.content)
        self.assertEqual(ConfirmacaoContagem.objects.filter(rodada=self.rodada).count(), 1)
        r = RodadaContagem.objects.select_related('item').get(pk=self.rodada.pk)
        self.assertEqual((r.embalagens, r.avulsos), (D('.5'), 7))
        self.assertEqual(D(representacao_fisica(r)['total_unidades']), 13)
        self.assertEqual(client.post(url, {**dados, 'avulsos': '8'}).status_code, 409)
        self.assertEqual(Produto.objects.values().get(pk=self.produto.pk), antes)
        self.assertFalse(AplicacaoAjuste.objects.exists())

    def test_requisicao_invalida_nao_grava_contagem(self):
        self.configurar(6)
        antes = Produto.objects.values().get(pk=self.produto.pk)
        with self.assertRaises(ValueError):
            services.salvar_contagem(self.acesso.pk, self.rodada.pk, 0, '.333', '7', True)
        self.assertFalse(ConfirmacaoContagem.objects.filter(rodada=self.rodada).exists())
        self.assertEqual(Produto.objects.values().get(pk=self.produto.pk), antes)

    def test_exemplo_31_un_preserva_parcelas_e_evento_original(self):
        from estoque.views import _quantidade_estoque_para_unidade_base
        self.configurar(12)
        antes = Produto.objects.values().get(pk=self.produto.pk)
        r = services.salvar_contagem(self.acesso.pk, self.rodada.pk, 0, '2', '7', True)
        c = ConfirmacaoContagem.objects.get(rodada=r)
        self.assertEqual((c.embalagens, c.avulsos), (2, 7))
        self.assertEqual(c.contado_base, D('2.583'))
        evento = self.inv.eventos.get(acao='produto_ok')
        self.assertEqual(D(evento.dados['contagem_fisica']['total_unidades']), 31)
        self.assertEqual(Produto.objects.values().get(pk=self.produto.pk), antes)
        self.produto.refresh_from_db()
        self.assertFalse(self.produto.vende_fracionado)
        with self.assertRaises(ValueError):
            _quantidade_estoque_para_unidade_base(self.produto, D(1), 'UN')

    def test_desktop_admin_usa_mesma_conversao_e_preserva_parcelas(self):
        self.configurar(24)
        antes = Produto.objects.values().get(pk=self.produto.pk)
        token = contagem_admin.autorizacao(self.usuario, self.inv, self.rodada)
        for _ in range(2):
            contagem_admin.confirmar(self.usuario, self.inv.pk, self.rodada.pk, token, '.5', '7')
        c = ConfirmacaoContagem.objects.get(rodada=self.rodada)
        self.assertEqual((c.embalagens, c.avulsos), (D('.5'), 7))
        self.assertEqual(c.contado_base, D('0.792'))
        self.assertEqual(Produto.objects.values().get(pk=self.produto.pk), antes)

    def test_html_mobile_campos_disposicao_e_total_preservados(self):
        self.configurar(12)
        client = Client()
        convite = reverse('inventario:convite', args=[self.token])
        client.get(convite)
        client.post(convite)
        r = client.get(reverse('inventario:mobile'))
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertContains(r, 'step="0.001"')
        self.assertContains(r, '<summary>Unidades avulsas</summary>')
        self.assertContains(r, 'Quantas unidades soltas foram encontradas?')
        self.assertContains(r, 'data-total-fisico')
        self.assertLess(html.index('name="embalagens"'), html.index('name="avulsos"'))
        self.client.force_login(self.usuario)
        detalhe = self.client.get(reverse('inventario:detalhe', args=[self.inv.pk]))
        self.assertEqual(detalhe.status_code, 200)
        self.assertContains(detalhe, 'step="0.001"')


from .tests_relacao_fisica import Fixture as RelacaoFixture


class RelacaoFracoesTests(RelacaoFixture, TestCase):
    def test_relacao_confirmada_sem_venda_avulsa_usa_mesma_regra(self):
        antes = Produto.objects.values().get(pk=self.produto.pk)
        with self.assertRaises(ValueError):
            services.salvar_contagem(self.acesso.pk, self.rodada.pk, 0, '.5', '7', True)
        self.confirmar('12')
        r = services.salvar_contagem(self.acesso.pk, self.rodada.pk, 0, '.5', '7', True)
        self.assertEqual(r.contado_base, D('1.083'))
        self.assertEqual((r.embalagens, r.avulsos), (D('.5'), 7))
        self.assertEqual(Produto.objects.values().get(pk=self.produto.pk), antes)
        self.assertFalse(AplicacaoAjuste.objects.exists())
