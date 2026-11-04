from datetime import date
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

from django.test import TestCase
from django.urls import reverse
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from tempfile import TemporaryDirectory
from offline.browser_support import Chrome
from .tests_edicao_compra import CHROME

from .tests_edicao_compra import EdicaoFixture
from .models import Compra, ContaPagar, ParcelaCompra, PagamentoContaPagar, EdicaoCompra, ConciliacaoCompra, Produto


class BoletosFinalizadaTests(EdicaoFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.compra.total = self.compra.total_produtos = Decimal('3938.96')
        self.compra.valor_nota_boleto = Decimal('3928.67')
        self.compra.data_vencimento = date(2026, 10, 31)
        self.compra.save()
        self.item.quantidade = 1
        self.item.preco_unitario = self.item.valor_total = Decimal('3938.96')
        self.item.save()
        self.conta.valor_original = self.conta.valor_em_aberto = Decimal('3938.96')
        self.conta.data_vencimento = date(2026, 10, 31)
        self.conta.numero_parcela = self.conta.total_parcelas = 1
        self.conta.save()
        ParcelaCompra.objects.create(compra=self.compra, numero=1, valor='3928.67', data_vencimento=date(2026, 10, 31))

    def boletos(self, valores=('1958.19', '1341.12', '639.65')):
        dados = self.payload(**{'acao_compra': 'salvar_correcao_simples', 'quantidade[]': ['1'],
            'preco_unitario[]': ['3938.96'], 'valor_cobrado': '3938.96',
            'compra_forma_cobranca_nota': 'varios_boletos', 'compra_quantidade_boletos': '3',
            'compra_valor_nota_boleto': str(sum(map(Decimal, valores)))})
        itens = list(self.compra.itens.order_by('pk'))
        dados.update({'produto_id[]': [str(i.produto_id) for i in itens],
            'item_edicao_id[]': [str(i.pk) for i in itens],
            **{campo + '[]': [str(getattr(i, campo)) for i in itens]
               for campo in ('quantidade', 'preco_unitario', 'unidade')},
            'observacao_item[]': [i.observacao for i in itens]})
        contas = list(self.compra.contas_pagar.exclude(status=ContaPagar.STATUS_CANCELADA).order_by('numero_parcela'))
        for n, valor in enumerate(valores, 1):
            dados[f'compra_parcela_valor_{n}'] = valor
            dados[f'compra_parcela_vencimento_{n}'] = '2026-11-08'
            dados[f'compra_conta_id_{n}'] = str(contas[n-1].pk) if n <= len(contas) else ''
        return dados

    def salvar(self, dados):
        resposta = self.client.post(self.url, dados, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(resposta.status_code, 200, resposta.content)
        self.assertTrue(resposta.json()['sucesso'])

    def test_tres_boletos_mesmo_vencimento_reabertura_e_reenvio(self):
        estoque = self.produto.quantidade
        item = self.compra.itens.values().get()
        dados = self.boletos()
        self.salvar(dados)
        self.salvar(dados)
        self.compra.refresh_from_db(); self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, estoque)
        self.assertEqual(self.compra.itens.values().get(), item)
        self.assertEqual(self.compra.status, Compra.STATUS_FINALIZADA)
        self.assertEqual(self.compra.total, Decimal('3938.96'))
        self.assertEqual(self.compra.valor_nota_boleto, Decimal('3938.96'))
        self.assertEqual(self.compra.data_vencimento, date(2026, 11, 8))
        self.assertEqual(list(self.compra.parcelas_nota.values_list('valor', flat=True)), list(map(Decimal, ('1958.19','1341.12','639.65'))))
        contas = self.compra.contas_pagar.exclude(status=ContaPagar.STATUS_CANCELADA)
        self.assertEqual(contas.count(), 3)
        self.assertEqual(set(contas.values_list('total_parcelas', flat=True)), {3})
        self.assertEqual(set(contas.values_list('data_vencimento', flat=True)), {date(2026,11,8)})
        self.assertEqual(sum(c.valor_em_aberto for c in contas), Decimal('3938.96'))
        self.assertEqual(EdicaoCompra.objects.count(), 1)
        self.assertEqual(len(self.client.get(self.url).context['pagamento_nota_compra']['linhas']), 3)
        self.salvar(self.boletos())  # New token after reopening is also safe.
        self.assertEqual(self.compra.contas_pagar.count(), 3)

    def test_edicao_posterior_preserva_ids_e_valor_financeiro_distinto(self):
        self.salvar(self.boletos())
        ids = list(self.compra.contas_pagar.values_list('pk', flat=True))
        self.salvar(self.boletos(('1950.00', '1300.00', '678.67')))
        self.assertEqual(list(self.compra.contas_pagar.values_list('pk', flat=True)), ids)
        self.compra.refresh_from_db()
        self.assertEqual(self.compra.total_produtos, Decimal('3938.96'))
        self.assertEqual(self.compra.total, Decimal('3938.96'))
        self.assertEqual(self.compra.valor_nota_boleto, Decimal('3928.67'))

    def test_pagamento_inclusive_cancelado_bloqueia_substituicao(self):
        for cancelado in (False, True):
            with self.subTest(cancelado=cancelado):
                pagamento = PagamentoContaPagar.objects.create(conta=self.conta, valor=10,
                    data_pagamento=date(2026,10,9), cancelado=cancelado)
                self.assert_bloqueado(self.boletos(), 'pagamento registrado')
                pagamento.delete()

    def test_conta_paga_ou_parcial_bloqueia(self):
        for status in (ContaPagar.STATUS_PAGA, ContaPagar.STATUS_PARCIAL):
            self.conta.status = status; self.conta.save()
            self.assert_bloqueado(self.boletos(), 'saldo movimentado')

    def test_conciliacao_bloqueia(self):
        edicao = EdicaoCompra.objects.create(compra=self.compra, usuario=self.user,
            operacao=uuid4(), motivo='Anterior', payload_hash='anterior', antes={}, depois={})
        ConciliacaoCompra.objects.create(compra=self.compra, edicao=edicao, tipo='erro_lancamento',
            valor_pago=0, saldo_a_pagar='3938.96')
        resposta = self.client.post(self.url, self.boletos(), HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(resposta.status_code, 422)
        self.assertIn('conciliação auditada', resposta.json()['erro'])
        self.assertEqual(self.compra.parcelas_nota.count(), 1)

    def test_falha_na_parcela_reverte_contas_e_plano(self):
        with patch('estoque.services.edicao_compra.ParcelaCompra.objects.create', side_effect=ValueError('Falha de parcela')):
            self.assert_bloqueado(self.boletos(), 'Falha de parcela')

    def test_alterar_itens_e_boletos_juntos_nao_ignora_boletos(self):
        dados = self.boletos(); dados['preco_unitario[]'] = ['4000']
        self.assert_bloqueado(dados, 'conciliação específica')

    def test_soma_invalida_nao_salva_parcialmente(self):
        dados = self.boletos(); dados['compra_parcela_valor_3'] = '639.64'
        resposta = self.client.post(self.url, dados, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(resposta.status_code, 422)
        self.assertEqual(self.compra.parcelas_nota.get().valor, Decimal('3928.67'))
        self.assertEqual(self.compra.contas_pagar.count(), 1)

    def test_documento_legado_bloqueia(self):
        self.conta.documento_legado = 'documento-vinculado'; self.conta.save()
        self.assert_bloqueado(self.boletos(), 'documento legado')

    def test_movimento_financeiro_bloqueia(self):
        self.movimento_automatico_compra_avista()
        self.assert_bloqueado(self.boletos(), 'movimento financeiro')

    def conciliacao_mercadoria(self):
        # Produce a real audited correction through the existing application flow.
        self.compra.total = self.compra.total_produtos = Decimal('3928.67'); self.compra.save()
        self.item.preco_unitario = self.item.valor_total = Decimal('3928.67'); self.item.save()
        self.conta.valor_original = self.conta.valor_em_aberto = Decimal('3928.67'); self.conta.save()
        sprite = Produto.objects.create(nome='Sprite teste', quantidade=0, preco_compra='10.29',
            preco_vista=15, preco_prazo=16, unidade_compra='PCT')
        dados = self.boletos()
        dados = {k: v for k, v in dados.items() if not k.startswith('compra_')}
        dados.update({'produto_id[]': [str(self.produto.pk), str(sprite.pk)],
            'item_edicao_id[]': [str(self.item.pk), ''], 'quantidade[]': ['1', '1'],
            'preco_unitario[]': ['3928.67', '10.29'], 'unidade[]': ['UN', 'PCT'],
            'observacao_item[]': [self.item.observacao, '']})
        self.salvar(dados)
        self.compra.refresh_from_db()
        self.conta.refresh_from_db()
        return ConciliacaoCompra.objects.get(compra=self.compra)

    def snapshot_protegido(self):
        from inventario.models import MovimentoObservado
        return {m.__name__: list(m.objects.order_by('pk').values()) for m in (
            ConciliacaoCompra, Produto, MovimentoObservado,
        )} | {'itens': list(self.compra.itens.order_by('pk').values())}

    def test_conciliacao_mercadoria_reparcela_preserva_historico_e_reenvios(self):
        conciliacao = self.conciliacao_mercadoria()
        original = EdicaoCompra.objects.filter(pk=conciliacao.edicao_id).values().get()
        protegido = self.snapshot_protegido()
        dados = self.boletos()
        self.salvar(dados); self.salvar(dados)
        self.salvar(self.boletos())
        self.assertEqual(self.snapshot_protegido(), protegido)
        self.assertEqual(EdicaoCompra.objects.filter(pk=conciliacao.edicao_id).values().get(), original)
        self.conta.refresh_from_db()
        self.assertEqual(self.conta.valor_original, Decimal('1958.19'))
        self.assertEqual(self.compra.contas_pagar.count(), 3)
        self.assertEqual(list(self.compra.parcelas_nota.values_list('valor', flat=True)),
            list(map(Decimal, ('1958.19', '1341.12', '639.65'))))
        auditoria = self.compra.edicoes.order_by('-pk').first()
        self.assertEqual(auditoria.antes['conciliacao'], auditoria.depois['conciliacao'])

    def test_conciliacao_sem_dinheiro_nao_permite_alterar_divida(self):
        self.conciliacao_mercadoria()
        resposta = self.client.post(self.url, self.boletos(('1958.19', '1341.12', '639.64')),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(resposta.status_code, 422)
        self.assertEqual(self.compra.contas_pagar.count(), 1)

    def test_conciliacoes_financeiras_e_saldos_divergentes_continuam_bloqueados(self):
        conciliacao = self.conciliacao_mercadoria()
        casos = ({'tipo': 'devolucao'}, {'tipo': 'diferenca'}, {'valor_pago': Decimal('1')},
            {'valor_devolvido': Decimal('1')}, {'credito_fornecedor': Decimal('1')},
            {'saldo_a_pagar': Decimal('3938.95')})
        for alteracoes in casos:
            with self.subTest(alteracoes=alteracoes):
                originais = {k: getattr(conciliacao, k) for k in alteracoes}
                ConciliacaoCompra.objects.filter(pk=conciliacao.pk).update(**alteracoes)
                antes = self.snapshot_protegido()
                resposta = self.client.post(self.url, self.boletos(), HTTP_X_REQUESTED_WITH='XMLHttpRequest')
                self.assertEqual(resposta.status_code, 422)
                self.assertEqual(self.snapshot_protegido(), antes)
                self.assertEqual(self.compra.contas_pagar.count(), 1)
                ConciliacaoCompra.objects.filter(pk=conciliacao.pk).update(**originais)

    def test_conciliacao_efeitos_financeiros_ou_auditoria_divergente_bloqueiam(self):
        conciliacao = self.conciliacao_mercadoria()
        edicao = conciliacao.edicao
        for campo, valor in (('pago_registrado_antes', '10.00'), ('devolvido_antes', '1.00'),
            ('correcao_registro_financeiro', {'id': 999}), ('movimento_devolucao_id', 999),
            ('registro_automatico_retificavel', True), ('total_corrigido', '3938.95')):
            with self.subTest(campo=campo):
                efeitos = dict(conciliacao.efeitos, **{campo: valor})
                ConciliacaoCompra.objects.filter(pk=conciliacao.pk).update(efeitos=efeitos)
                EdicaoCompra.objects.filter(pk=edicao.pk).update(depois=dict(edicao.depois, ajustes=efeitos))
                resposta = self.client.post(self.url, self.boletos(), HTTP_X_REQUESTED_WITH='XMLHttpRequest')
                self.assertEqual(resposta.status_code, 422)
        ConciliacaoCompra.objects.filter(pk=conciliacao.pk).update(efeitos=conciliacao.efeitos)
        # Deliberately leave the linked audit different.
        resposta = self.client.post(self.url, self.boletos(), HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(resposta.status_code, 422)

    def test_conciliacao_muda_apos_abertura_invalida_token(self):
        conciliacao = self.conciliacao_mercadoria()
        dados = self.boletos()
        ConciliacaoCompra.objects.filter(pk=conciliacao.pk).update(valor_pago=1)
        resposta = self.client.post(self.url, dados, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(resposta.status_code, 422)
        self.assertIn('mudaram', resposta.json()['erro'])

    def test_conciliacao_mercadoria_rollback_na_auditoria(self):
        self.conciliacao_mercadoria()
        antes = self.snapshot_protegido()
        with patch('estoque.services.edicao_compra.EdicaoCompra.objects.create', side_effect=ValueError('Falha auditada')):
            resposta = self.client.post(self.url, self.boletos(), HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(resposta.status_code, 422)
        self.assertEqual(self.snapshot_protegido(), antes)
        self.assertEqual(self.compra.contas_pagar.count(), 1)

    def test_conciliacao_mercadoria_pagamentos_ativos_e_cancelados_bloqueiam(self):
        self.conciliacao_mercadoria()
        from .services.edicao_compra import estado_compra
        for valor, cancelado in (('10.00', False), ('3938.96', False), ('10.00', True)):
            with self.subTest(valor=valor, cancelado=cancelado):
                pagamento = PagamentoContaPagar.objects.create(conta=self.conta, valor=valor,
                    cancelado=cancelado, data_pagamento=self.compra.data_compra)
                antes = estado_compra(self.compra)
                resposta = self.client.post(self.url, self.boletos(), HTTP_X_REQUESTED_WITH='XMLHttpRequest')
                self.assertEqual(resposta.status_code, 422)
                self.assertEqual(estado_compra(self.compra), antes)
                pagamento.delete()

    def test_conciliacao_mercadoria_rota_historica_e_saldo_movimentado(self):
        self.conciliacao_mercadoria()
        self.conta.valor_em_aberto = Decimal('3938.95'); self.conta.save()
        resposta = self.client.post(self.url, self.boletos(), HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(resposta.status_code, 422)
        self.conta.valor_em_aberto = Decimal('3938.96'); self.conta.save()
        dados = self.boletos(); dados['acao_compra'] = 'salvar_edicao_finalizada'
        self.salvar(dados)
        self.assertEqual(self.compra.contas_pagar.count(), 3)


class BoletosFinalizadaBrowserTests(EdicaoFixture, StaticLiveServerTestCase):
    def test_botao_real_salva_tres_boletos_desktop_e_mobile(self):
        dados = {k: v for k, v in self.payload(acao_compra='salvar_correcao_simples').items()
            if not k.startswith('compra_')}
        resposta = self.client.post(self.url, dados, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(resposta.status_code, 200, resposta.content)
        conciliacao = ConciliacaoCompra.objects.values().get(compra=self.compra)
        for largura in (1280, 390):
            with self.subTest(largura=largura), TemporaryDirectory(prefix='boletos-finalizada-', ignore_cleanup_errors=True) as profile:
                chrome = Chrome(CHROME, profile).start()
                try:
                    tab = chrome.tab()
                    tab.call('Emulation.setDeviceMetricsOverride', {'width': largura, 'height': 900, 'deviceScaleFactor': 1, 'mobile': largura == 390})
                    tab.call('Network.setCookie', {'name':'sessionid', 'value':self.client.cookies['sessionid'].value, 'url':self.live_server_url})
                    tab.call('Page.navigate', {'url':self.live_server_url + self.url})
                    tab.wait("document.readyState === 'complete' && !!document.getElementById('compraFormaCobrancaNota')")
                    tab.evaluate("""document.getElementById('motivoEdicaoCompra').value='Conferencia dos boletos';
                        const forma = document.getElementById('compraFormaCobrancaNota');
                        forma.value='varios_boletos'; forma.dispatchEvent(new Event('change',{bubbles:true}));
                        const quantidade = document.getElementById('compraQuantidadeBoletos');
                        quantidade.value='3'; quantidade.dispatchEvent(new Event('change',{bubbles:true})); true""")
                    tab.wait("!!document.querySelector('[name=compra_parcela_valor_3]')")
                    tab.evaluate("""[40,30,30].forEach((valor,i)=>{
                        const campo=document.querySelector('[name=compra_parcela_valor_'+(i+1)+']');
                        campo.value=String(valor); campo.dispatchEvent(new Event('input',{bubbles:true}));
                        document.querySelector('[name=compra_parcela_vencimento_'+(i+1)+']').value='2026-11-08';
                        }); document.getElementById('btnSalvarCompra').click(); true""")
                    tab.wait("location.pathname === '" + reverse('estoque:compras_detalhe', args=[self.compra.pk]) + "'")
                    self.assertEqual(self.compra.parcelas_nota.count(), 3)
                    self.assertEqual(self.compra.contas_pagar.exclude(status=ContaPagar.STATUS_CANCELADA).count(), 3)
                    self.produto.refresh_from_db()
                    self.assertEqual(self.produto.quantidade, 20)
                    self.assertEqual(ConciliacaoCompra.objects.values().get(compra=self.compra), conciliacao)
                finally:
                    chrome.stop()
