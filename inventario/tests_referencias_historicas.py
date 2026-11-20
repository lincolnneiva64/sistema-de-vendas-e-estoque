"""Physical receipts in the full Django extract, using disposable data only."""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import connection
from django.http import QueryDict
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from estoque.models import Compra, Funcionario, ItemCompra, ItemVenda, ItemVendaRemovido, Produto, Venda
from . import ajustes, services
from .extrato import cronologia
from .models import DetalheMovimentoEstoque, MovimentoObservado, RodadaContagem
from .tests import produto


@override_settings(INVENTARIO_AJUSTES_HABILITADOS=True, INVENTARIO_PRODUTOS_EXCLUIDOS_PILOTO=())
class ReferenciasHistoricasTests(TestCase):
    def setUp(self):
        self.usuario = get_user_model().objects.create_superuser('historico-sintetico', '', 'teste')
        self.funcionario = Funcionario.objects.create(nome='Contador sintético')
        self.produto = produto(nome='Vitarela sintética', quantidade=11, unidade_compra='UN',
                               vende_fracionado=False, unidade_venda_2='')
        self.tempo = timezone.now() - timedelta(days=2)
        self.client.force_login(self.usuario)
        self.rodada = self.contar(11)

    def contar(self, quantidade):
        self.tempo += timedelta(seconds=10)
        with patch('django.utils.timezone.now', return_value=self.tempo):
            inv = services.criar_inventario(self.usuario, 'Conferência sintética', 'manual', [self.produto.pk])
            r = RodadaContagem.objects.get(item__inventario=inv)
            acesso, _ = services.gerar_acesso(self.usuario, inv.pk, self.funcionario.pk, [r.pk])
            services.salvar_contagem(acesso.pk, r.pk, 0, str(quantidade), '0', True)
        return RodadaContagem.objects.select_related('item__produto').get(pk=r.pk)

    def aplicar(self, rodada=None):
        r = rodada or self.rodada
        r = RodadaContagem.objects.select_related('item__produto').get(pk=r.pk)
        self.tempo += timedelta(seconds=10)
        with patch('django.utils.timezone.now', return_value=self.tempo):
            avaliacao = ajustes.avaliar(r)
            self.assertFalse(avaliacao['bloqueio'], avaliacao['bloqueio'])
            token = ajustes.previa_atualizacao(r.item.inventario_id, [avaliacao])
            recibo = ajustes.atualizar(self.usuario, r.item.inventario_id, token, True)[0]
        return recibo

    def documento(self, entrada, quantidade, unidade='UN', desconhecido=False, detalhar=True):
        self.tempo += timedelta(seconds=10)
        q = Decimal(quantidade)
        self.produto.refresh_from_db()
        antes = self.produto.quantidade
        depois = antes + q if entrada else antes - q
        with patch('django.utils.timezone.now', return_value=self.tempo):
            if entrada:
                doc = Compra.objects.create(data_compra=self.tempo.date(), operador='Sintético',
                    estoque_entrada_realizada=True, estoque_entrada_realizada_em=self.tempo)
                item = ItemCompra.objects.create(compra=doc, produto=self.produto, quantidade=q,
                    unidade=unidade, preco_unitario=0, valor_total=0)
            else:
                doc = Venda.objects.create(data_venda=self.tempo.date(), operador='Sintético')
                item = ItemVenda.objects.create(venda=doc, produto=self.produto, quantidade=q,
                    unidade=unidade, preco_unitario=0, valor_total=0)
            Produto.objects.filter(pk=self.produto.pk).update(quantidade=depois)
            m = MovimentoObservado.objects.filter(produto=self.produto).latest('sequencia')
            origem = 'compra' if entrada else 'venda'
            MovimentoObservado.objects.filter(pk=m.pk).update(registrado_em=self.tempo,
                origem='nao_identificada' if desconhecido else origem,
                referencia='' if desconhecido else f'{"Compra" if entrada else "Venda"} #{doc.pk}')
            if not desconhecido and detalhar:
                DetalheMovimentoEstoque.objects.create(movimento=m, operacao_id=uuid4(), ordem=1,
                    documento_tipo=origem, documento_id=str(doc.pk), item_referencia=str(item.pk),
                    acao='entrada' if entrada else 'saida', contexto='autenticado',
                    executor=self.usuario, executor_nome='Sintético', unidade_base=unidade,
                    quantidade_entrada=q, unidade_entrada=unidade, conversao='nenhuma')
        return item

    def pagina(self, inicio=None, fim=None, numero=1, visual='b'):
        inicio = inicio or self.rodada.ok_em - timedelta(seconds=1)
        fim = fim or self.tempo + timedelta(seconds=1)
        return cronologia(ItemCompra.objects.filter(produto=self.produto),
            ItemVenda.objects.filter(produto=self.produto, venda__criado_em__gte=inicio, venda__criado_em__lt=fim),
            ItemVendaRemovido.objects.none(),
            MovimentoObservado.objects.filter(produto=self.produto, registrado_em__gte=inicio, registrado_em__lt=fim),
            QueryDict(f'visual={visual}&extrato_pagina={numero}'), None, inicio, fim,
            produto_id=self.produto.pk)

    def estado(self):
        modelos = [m for app in ('inventario', 'estoque') for m in apps.get_app_config(app).get_models()]
        return {m._meta.label: list(m.objects.order_by('pk').values()) for m in modelos}

    def test_zero_aparece_certificado_sem_movimento_em_ambas_apresentacoes(self):
        recibo = self.aplicar()
        antes = self.estado()
        for visual in ('a', 'b'):
            p = self.pagina(visual=visual)
            self.assertEqual(len(p), 1)
            self.assertEqual(p[0]['saldo_certificado']['valor'], 11)
            self.assertEqual(p[0]['instante'], recibo.aplicado_em)
            self.assertIsNone(p[0]['saldo_calculado'])
        self.assertEqual(antes, self.estado())

    def test_ajuste_aparece_uma_vez_e_reinicia_saldo(self):
        r = self.contar(8)
        recibo = self.aplicar(r)
        venda = self.documento(False, 2)
        p = self.pagina()
        self.assertEqual(sum(bool(l.get('saldo_certificado')) for l in p), 1)
        self.assertFalse(any(l['fonte'] in ('manual', 'digital') for l in p))
        aplicada = next(l for l in p if l.get('saldo_certificado'))
        self.assertEqual(aplicada['url'], reverse('inventario:movimento_manual',
            args=[r.item.inventario_id, r.pk, recibo.decisao.movimentacao_id]))
        self.assertEqual(next(l for l in p if l['pk'] == venda.pk and l['fonte'] == 'venda')['saldo_calculado']['valor'], 6)

    def test_venda_posterior_calculada_sem_snapshot(self):
        self.aplicar()
        self.documento(False, 2)
        p = self.pagina()
        self.assertEqual(p[-1]['saldo_calculado']['anterior'], 11)
        self.assertEqual(p[-1]['saldo_calculado']['valor'], 9)

    def test_compra_posterior_calculada(self):
        self.aplicar()
        self.documento(True, 4)
        self.assertEqual(self.pagina()[-1]['saldo_calculado']['valor'], 15)

    def test_multiplas_conferencias_substituem_continuidade_sem_somar_ajuste(self):
        self.aplicar()
        self.documento(False, 2)
        r = self.contar(6)
        self.aplicar(r)
        self.documento(True, 3)
        p = self.pagina()
        self.assertEqual([l['saldo_certificado']['valor'] for l in p if l.get('saldo_certificado')], [11, 6])
        self.assertEqual(p[-1]['saldo_calculado']['valor'], 9)

    def test_referencia_anterior_abre_intervalo_e_pagina_seguinte(self):
        recibo = self.aplicar()
        self.documento(False, 2)
        inicio = self.tempo + timedelta(seconds=1)
        self.documento(True, 3)
        p = self.pagina(inicio=inicio)
        self.assertEqual(p.saldo_inicial['valor'], 9)
        self.assertEqual(p.saldo_inicial['instante'], recibo.aplicado_em)
        self.assertEqual(p[0]['saldo_calculado']['valor'], 12)

    def test_intervalo_vazio_ainda_mostra_referencia_anterior(self):
        self.aplicar()
        p = self.pagina(inicio=self.tempo + timedelta(seconds=1), fim=self.tempo + timedelta(seconds=2))
        self.assertEqual(len(p), 0)
        self.assertEqual(p.saldo_inicial['valor'], 11)

    def test_sem_referencia_nao_inventa_saldo(self):
        self.documento(False, 2, desconhecido=True)
        p = self.pagina()
        self.assertTrue(all(l['saldo_calculado'] is None for l in p))
        self.assertFalse(any(l.get('saldo_certificado') for l in p))

    def test_lacuna_interrompe_ate_nova_referencia(self):
        self.aplicar()
        self.documento(False, 2, desconhecido=True)
        self.documento(False, 1, detalhar=False)
        self.assertIsNone(self.pagina()[-1]['saldo_calculado'])
        r = self.contar(7)
        self.aplicar(r)
        self.documento(False, 1)
        self.assertEqual(self.pagina()[-1]['saldo_calculado']['valor'], 6)

    def test_unidade_incompativel_nao_propaga_saldo(self):
        self.aplicar()
        self.documento(False, 2, unidade='CX')
        self.assertIsNone(self.pagina()[-1]['saldo_calculado'])

    def test_get_django_completo_mostra_certificacao_e_nao_grava(self):
        self.aplicar()
        self.documento(False, 2)
        antes = self.estado()
        url = reverse('inventario:historico_produto', args=[self.rodada.item.inventario_id, self.rodada.pk])
        with CaptureQueriesContext(connection) as consultas:
            r = self.client.get(url, {'periodo': 'completo'})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Saldo certificado por referência física')
        self.assertContains(r, 'Documental calculado')
        for q in consultas:
            sql = q['sql'].lstrip(' (').upper()
            self.assertTrue(sql.startswith(('SELECT', 'SAVEPOINT', 'RELEASE SAVEPOINT'))
                or sql.startswith('DECLARE ') and ' FOR (SELECT ' in sql, sql[:150])
        self.assertEqual(antes, self.estado())

    def test_paginacao_transporta_saldo_da_referencia_sem_recontar_ajustes(self):
        self.aplicar()
        self.documento(True, 60)
        for _ in range(51):
            self.documento(False, 1)
        primeira = self.pagina()
        segunda = self.pagina(numero=2)
        self.assertEqual(primeira.paginator.count, 53)
        self.assertEqual(segunda[-1]['saldo_calculado']['valor'], 20)
        self.assertEqual(segunda[0]['saldo_calculado']['anterior'], primeira[-1]['saldo_calculado']['valor'])

    def test_recibos_em_lote_sem_consulta_por_referencia(self):
        from .referencias_historicas import preparar
        self.aplicar()
        linhas = list(self.pagina())
        for _ in range(4):
            self.aplicar(self.contar(11))
        todas = list(self.pagina())
        with CaptureQueriesContext(connection) as poucas:
            preparar(linhas, self.produto.pk)
        with CaptureQueriesContext(connection) as muitas:
            preparar(todas, self.produto.pk)
        self.assertEqual(len(poucas), len(muitas))
        self.assertLessEqual(len(muitas), 2)

    def test_busca_do_prefixo_limitada_a_ultima_aplicacao(self):
        self.aplicar()
        self.documento(False, 2)
        nova = self.aplicar(self.contar(9))
        inicio = self.tempo + timedelta(seconds=1)
        self.documento(True, 1)
        with CaptureQueriesContext(connection) as consultas:
            p = self.pagina(inicio=inicio)
        self.assertEqual(p.saldo_inicial['proposta'], str(nova.proposta_id))
        self.assertEqual(p[0]['saldo_calculado']['valor'], 10)
        # Prefix SQL contains a lower bound at the latest application, rather
        # than rebuilding all preceding conferences and documents.
        limites = [q['sql'] for q in consultas if 'UNION ALL' in q['sql'] and ' DESC' in q['sql']]
        self.assertTrue(limites)
        self.assertTrue(any('>=' in sql for sql in limites))

    def test_intervalo_anterior_nao_retroage_referencia_futura(self):
        self.aplicar()
        p = self.pagina(inicio=self.rodada.ok_em - timedelta(days=1), fim=self.rodada.ok_em)
        self.assertEqual(len(p), 0)
        self.assertIsNone(p.saldo_inicial)

    def test_recibo_incompativel_nao_certifica_nem_reutiliza_ancora_antiga(self):
        from .models import EventoAjuste, PropostaAjuste
        self.aplicar()
        r = self.contar(11)
        avaliacao = ajustes.avaliar(r)
        self.tempo += timedelta(seconds=10)
        with patch('django.utils.timezone.now', return_value=self.tempo):
            p = PropostaAjuste.objects.create(rodada=r, confirmacao=r.confirmacoes.latest('versao_entrada'),
                assinatura='assinatura-incompativel-sintetica', evidencia=avaliacao['evidencia'],
                justificativa='Recibo inválido somente para teste', estado='aplicado', autorizado_por=self.usuario)
            EventoAjuste.objects.create(proposta=p, usuario=self.usuario, acao='marco_sem_ajuste',
                dados={'preparacao': avaliacao['evidencia']})
        pagina = self.pagina()
        self.assertTrue(pagina[-1]['referencia_fisica_invalida'])
        self.assertIsNone(pagina[-1]['saldo_calculado'])
        posterior = self.pagina(inicio=self.tempo + timedelta(seconds=1), fim=self.tempo + timedelta(seconds=2))
        self.assertIsNone(posterior.saldo_inicial)

    def test_recibo_com_ajuste_recusado_na_consulta_nao_quebra_html(self):
        r = self.contar(8)
        self.aplicar(r)
        antes = self.estado()
        url = reverse('inventario:historico_produto', args=[r.item.inventario_id, r.pk])
        with patch('inventario.referencias_historicas.normalizar_recibo',
                   side_effect=ValueError('Recibo incompatível simulado')):
            resposta = self.client.get(url, {'periodo': 'completo'})
        self.assertEqual(resposta.status_code, 200)
        linha = next(l for l in resposta.context['extrato'] if l['fonte'] == 'regularizacao')
        self.assertIsNone(linha['saldo_calculado'])
        self.assertIsNone(linha['saldo_certificado'])
        self.assertContains(resposta, 'continuidade indisponível')
        self.assertEqual(antes, self.estado())

    def consultar_dia_posterior(self):
        dia = timezone.localtime(self.tempo).date().isoformat()
        url = reverse('inventario:historico_produto', args=[self.rodada.item.inventario_id, self.rodada.pk])
        resposta = self.client.get(url, {'periodo': 'personalizado',
            'data_inicial': dia, 'data_final': dia})
        self.assertEqual(resposta.status_code, 200)
        return resposta

    def test_http_periodo_posterior_vazio_recupera_abertura_fisica(self):
        recibo = self.aplicar()
        self.tempo += timedelta(days=1)
        antes = self.estado()
        resposta = self.consultar_dia_posterior()
        pagina = resposta.context['extrato']
        self.assertEqual(pagina.saldo_inicial['valor'], 11)
        self.assertEqual(pagina.saldo_inicial['proposta'], str(recibo.proposta_id))
        self.assertEqual(pagina.saldo_inicial['tipo'], 'Saldo certificado por referência física')
        self.assertEqual([r['fonte'] for r in pagina], ['conferencia'])
        self.assertLess(pagina[0]['instante'], resposta.context['inicio'])
        self.assertEqual(antes, self.estado())

    def test_http_periodo_posterior_venda_mantem_origem_fisica(self):
        self.aplicar()
        self.tempo += timedelta(days=1)
        venda = self.documento(False, 2)
        resposta = self.consultar_dia_posterior()
        pagina = resposta.context['extrato']
        self.assertEqual(pagina.saldo_inicial['valor'], 11)
        linha = next(r for r in pagina if r['fonte'] == 'venda' and r['pk'] == venda.pk)
        self.assertEqual(linha['saldo_calculado']['anterior'], 11)
        self.assertEqual(linha['saldo_calculado']['valor'], 9)
        self.assertIn('Referência física aplicada', linha['saldo_calculado']['origem'])

    def test_ok_informativo_nao_amplia_limites_da_consulta(self):
        from .referencias_historicas import limite_anterior
        self.aplicar()
        self.tempo += timedelta(days=1)
        self.documento(False, 2)
        with patch('inventario.extrato.limite_anterior', wraps=limite_anterior) as limite:
            resposta = self.consultar_dia_posterior()
        self.assertTrue(limite.called)
        self.assertTrue(all(c.args[1] >= resposta.context['inicio'] for c in limite.call_args_list))
        pagina = resposta.context['extrato']
        self.assertTrue(any(r['fonte'] == 'conferencia' and r['instante'] < resposta.context['inicio'] for r in pagina))
        self.assertEqual(pagina.saldo_inicial['valor'], 11)

    def test_http_periodo_posterior_com_ajuste_nao_soma_delta_duas_vezes(self):
        rodada = self.contar(8)
        recibo = self.aplicar(rodada)
        self.tempo += timedelta(days=1)
        self.documento(False, 2)
        pagina = self.consultar_dia_posterior().context['extrato']
        self.assertEqual(pagina.saldo_inicial['valor'], 8)
        self.assertEqual(pagina.saldo_inicial['proposta'], str(recibo.proposta_id))
        linha = next(r for r in pagina if r['fonte'] == 'venda')
        self.assertEqual(linha['saldo_calculado']['valor'], 6)
        self.assertIn('Referência física aplicada', linha['saldo_calculado']['origem'])

    def test_http_periodo_posterior_usa_ultima_de_multiplas_conferencias(self):
        self.aplicar()
        recibo = self.aplicar(self.contar(9))
        self.tempo += timedelta(days=1)
        self.documento(False, 2)
        pagina = self.consultar_dia_posterior().context['extrato']
        self.assertEqual(pagina.saldo_inicial['valor'], 9)
        self.assertEqual(pagina.saldo_inicial['proposta'], str(recibo.proposta_id))
        self.assertEqual(next(r for r in pagina if r['fonte'] == 'venda')['saldo_calculado']['valor'], 7)

    def test_http_periodo_posterior_nao_propaga_unidade_incompativel(self):
        self.aplicar()
        self.tempo += timedelta(days=1)
        self.documento(False, 2, unidade='CX')
        pagina = self.consultar_dia_posterior().context['extrato']
        self.assertEqual(pagina.saldo_inicial['valor'], 11)
        self.assertEqual(pagina.saldo_inicial['unidade'], 'UN')
        self.assertIsNone(next(r for r in pagina if r['fonte'] == 'venda')['saldo_calculado'])
