"""Fictitious reproduction: one note, two observers, different timestamps."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import re

from django.http import QueryDict
from django.db import connection
from django.template import Context, Engine
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import include, path

from estoque.models import Cliente, Compra, ItemCompra, ItemVenda, ItemVendaRemovido, MovimentacaoEstoqueManual, EventoVenda, Produto, Venda
from .extrato import cronologia
from .models import DetalheMovimentoEstoque, MovimentoObservado


# Reverse only; these test URLs never call a commercial view/service.
urlpatterns = [path('', include(([
    path('vendas/<int:pk>/', lambda request, pk: None, name='venda_detalhe'),
    path('compras/<int:pk>/', lambda request, pk: None, name='compras_detalhe'),
], 'estoque'), namespace='estoque'))]


@override_settings(ROOT_URLCONF=__name__)
class AgrupamentoVendaDocumentalTests(TestCase):
    def inserir(self, modelo, **campos):
        objeto = modelo(**campos)
        modelo.objects.bulk_create([objeto])
        return objeto

    def setUp(self):
        self.hora = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
        self.produto = self.inserir(Produto, nome='Produto FICTICIO para agrupamento',
                                   unidade_compra='UN', quantidade=15)
        self.cliente = self.inserir(Cliente, nome='Cliente FICTICIO')
        for numero, quantidade, antes, depois, hora in (
                (996, 1, 29, 28, self.hora-timedelta(hours=1)),
                (1049, 13, 28, 15, self.hora)):
            venda = self.inserir(Venda, pk=numero, cliente=self.cliente,
                                 data_venda=hora.date(), tipo_pagamento='avista')
            Venda.objects.filter(pk=venda.pk).update(criado_em=hora)
            item = self.inserir(ItemVenda, venda=venda, produto=self.produto,
                quantidade=quantidade, unidade='UN', preco_unitario=1, valor_total=quantidade,
                estoque_antes=antes, estoque_movimentado=quantidade, estoque_depois=depois,
                estoque_unidade_snapshot='UN')
            if numero == 1049:
                self.item = item
        self.primeiro = self.inserir(MovimentoObservado, produto=self.produto,
            sequencia=1, antes=28, depois=1, origem='venda', referencia='Venda #1049',
            registrado_em=self.hora-timedelta(minutes=3))
        self.segundo = self.inserir(MovimentoObservado, produto=self.produto,
            sequencia=2, antes=1, depois=15, origem='devolucao_venda', referencia='Venda #1049',
            registrado_em=self.hora+timedelta(minutes=9))

    def pagina(self, visual='b', numero=1):
        params = QueryDict('', mutable=True)
        params.update(visual=visual, extrato_pagina=numero)
        with CaptureQueriesContext(connection) as consultas:
            pagina = cronologia(ItemCompra.objects.filter(produto=self.produto),
                ItemVenda.objects.filter(produto=self.produto),
                ItemVendaRemovido.objects.filter(produto=self.produto),
                MovimentoObservado.objects.filter(produto=self.produto), params, None,
                self.hora-timedelta(days=1), self.hora+timedelta(days=1), produto_id=self.produto.pk)
        self.assertTrue(all(q['sql'].lstrip().upper().startswith('SELECT') for q in consultas))
        return pagina

    def renderizar(self, pagina, visual):
        raiz = Path(__file__).parent / 'templates' / 'inventario'
        arquivos = {'inventario/'+p.name: '' for p in raiz.glob('*.html')}
        for nome in ('historico_quantidade.html', 'historico_edicoes.html'):
            arquivos['inventario/'+nome] = (raiz/nome).read_text(encoding='utf-8')
        motor = Engine(libraries={'horarios_documentos': 'estoque.templatetags.horarios_documentos'},
                       loaders=[('django.template.loaders.locmem.Loader', arquivos)])
        fonte = (raiz/'historico.html').read_text(encoding='utf-8')
        inicio = fonte.index('{% for registro in extrato %}')
        fim = fonte.index('{% endfor %}', fonte.index('{% empty %}', inicio))+len('{% endfor %}')
        return motor.from_string('{% load horarios_documentos %}'+fonte[inicio:fim]).render(
            Context({'extrato': pagina, 'visual': visual}))

    def conferir(self, pagina, visual, saldo=15):
        linhas = [r for r in pagina if r['fonte']=='venda' and r['documento']==1049]
        self.assertEqual(len(linhas), 1)
        linha = linhas[0]
        self.assertEqual([e.pk for e in linha['evidencias_operacionais']],
                         [self.primeiro.pk, self.segundo.pk])
        self.assertFalse(any(r['fonte']=='digital' and r['pk'] in (self.primeiro.pk,self.segundo.pk)
                             for r in pagina))
        self.assertEqual(linha['operacao_documental']['quantidade'], Decimal(13))
        self.assertEqual(linha['saldo_calculado']['valor'], Decimal(saldo))
        self.assertIsNone(linha['saldo'])  # No physical bank balance is certified.
        html = self.renderizar(pagina, visual)
        self.assertEqual(html.count(f'data-registro="registro-venda-{self.item.pk}"'), 1)
        for evidencia in (self.primeiro, self.segundo):
            self.assertNotIn(f'data-registro="registro-digital-{evidencia.pk}"', html)
            self.assertIn(f'Registro #{evidencia.pk}', html)
            self.assertIn(evidencia.registrado_em.isoformat(), html)
        self.assertIn('Evidências do estoque', html)
        self.assertIn('href="/vendas/1049/"', html)
        corpo = re.search(f'data-registro="registro-venda-{self.item.pk}"[^>]*>(.*?)</tr>',html,re.S).group(1)
        celulas = [' '.join(re.sub('<[^>]+>', ' ', c).split())
                   for c in re.findall(r'<t[dh]\b[^>]*>(.*?)</t[dh]>', corpo, re.S)]
        self.assertEqual(celulas[2], 'Saída −13 UN Quantidade da nota')
        self.assertTrue(celulas[3].startswith(f'{saldo} UN Calculado pelos documentos'))
        self.assertIn('não certificado pelo banco', celulas[3])

    def test_nota_e_duas_evidencias_em_horarios_diferentes(self):
        originais = list(MovimentoObservado.objects.order_by('pk').values())
        for visual in ('a', 'b'):
            self.conferir(self.pagina(visual), visual)
        self.assertEqual(list(MovimentoObservado.objects.order_by('pk').values()), originais)
        self.assertEqual(ItemVenda.objects.get(pk=self.item.pk).quantidade, Decimal(13))

    def test_duas_evidencias_com_origem_venda_tambem_se_agrupam(self):
        MovimentoObservado.objects.filter(pk=self.segundo.pk).update(origem='venda')
        self.conferir(self.pagina(), 'b')

    def test_documento_intermediario_e_paginacao_sao_considerados(self):
        compra = self.inserir(Compra, data_compra=self.hora.date(), estoque_entrada_realizada=True,
            estoque_entrada_realizada_em=self.hora-timedelta(minutes=30), status='finalizada')
        self.inserir(ItemCompra, compra=compra, produto=self.produto, quantidade=2,
                     unidade='UN', preco_unitario=1, valor_total=2)
        MovimentoObservado.objects.bulk_create([MovimentoObservado(produto=self.produto,
            sequencia=n+3, antes=100, depois=99, origem='nao_identificada',
            referencia=f'Registro FICTICIO {n}', registrado_em=self.hora-timedelta(hours=3)+timedelta(seconds=n))
            for n in range(48)])
        # Technical evidence no longer occupies operational pages.
        pagina = self.pagina(numero=2)
        self.assertEqual(pagina.number, 1)
        self.conferir(pagina, 'b', saldo=17)

    def test_referencia_generica_nao_e_associada_por_horario_ou_valor(self):
        MovimentoObservado.objects.filter(pk=self.segundo.pk).update(
            referencia='Gravação de venda', registrado_em=self.hora-timedelta(minutes=1))
        pagina = self.pagina()
        self.assertFalse(any(r['fonte']=='digital' and r['pk']==self.segundo.pk for r in pagina))
        self.assertTrue(MovimentoObservado.objects.filter(pk=self.segundo.pk).exists())
        linha = next(r for r in pagina if r['fonte']=='venda' and r['documento']==1049)
        self.assertEqual([e.pk for e in linha['evidencias_operacionais']], [self.primeiro.pk])
        self.assertIsNone(linha['saldo_calculado'])

    def test_produto_diferente_ou_item_repetido_nao_recebe_vinculo_forcado(self):
        outro = self.inserir(Produto, nome='Outro produto FICTICIO', unidade_compra='UN')
        self.inserir(MovimentoObservado, produto=outro, sequencia=1, antes=5, depois=4,
            origem='venda', referencia='Venda #1049', registrado_em=self.hora)
        self.conferir(self.pagina(), 'b')
        self.inserir(ItemVenda, venda_id=1049, produto=self.produto, quantidade=1,
                     unidade='UN', preco_unitario=1, valor_total=1)
        pagina = self.pagina()
        digitais = {r['pk'] for r in pagina if r['fonte']=='digital'}
        self.assertFalse(digitais)
        self.assertEqual(MovimentoObservado.objects.filter(pk__in=(self.primeiro.pk,self.segundo.pk)).count(), 2)

    def detalhe(self, movimento, operacao, ordem=1, **campos):
        dados = dict(movimento=movimento, operacao_id=operacao, ordem=ordem,
            acao='saida', documento_tipo='venda', documento_id='1049',
            contexto='sistema', executor_nome='Executor FICTICIO', operador_nome='Operador FICTICIO',
            unidade_base='UN', unidade_entrada='UN', quantidade_entrada=1, conversao='nenhuma')
        dados.update(campos)
        return self.inserir(DetalheMovimentoEstoque, **dados)

    def test_compra_com_varias_evidencias_segue_mesma_regra_da_venda(self):
        compra = self.inserir(Compra, data_compra=self.hora.date(), estoque_entrada_realizada=True,
            estoque_entrada_realizada_em=self.hora-timedelta(minutes=30), status='finalizada')
        item = self.inserir(ItemCompra, compra=compra, produto=self.produto, quantidade=81,
                           unidade='UN', preco_unitario=1, valor_total=81)
        registros = []
        for sequencia, antes, depois in ((3, 0, 40), (4, 40, 81)):
            registros.append(self.inserir(MovimentoObservado, produto=self.produto,
                sequencia=sequencia, antes=antes, depois=depois, origem='compra',
                referencia=f'Compra #{compra.pk}', registrado_em=self.hora-timedelta(minutes=30-sequencia)))
        pagina = self.pagina()
        linha = next(r for r in pagina if r['fonte']=='compra' and r['pk']==item.pk)
        self.assertEqual([e.pk for e in linha['evidencias_operacionais']], [e.pk for e in registros])
        self.assertEqual(linha['saldo_calculado']['valor'], Decimal(109))
        self.assertFalse(any(r['fonte']=='digital' and r['pk'] in [e.pk for e in registros] for r in pagina))
        self.conferir(pagina, 'b', saldo=96)

    def test_consumo_proprio_reutiliza_associacao_e_quantidade_documental(self):
        Venda.objects.filter(pk=1049).update(tipo_pagamento='consumo_proprio')
        MovimentoObservado.objects.filter(pk=self.primeiro.pk).update(origem='consumo_proprio')
        self.conferir(self.pagina(), 'b')

    def test_operacoes_com_ids_diferentes_nao_sao_ocultadas(self):
        operacao = '11111111-1111-1111-1111-111111111111'
        self.detalhe(self.primeiro, operacao, quantidade_entrada=27)
        segundo = self.detalhe(self.segundo, operacao, ordem=2, acao='devolucao', quantidade_entrada=14)
        pagina = self.pagina()
        retorno = next(r for r in pagina if r['fonte']=='digital' and r['pk']==self.segundo.pk)
        self.assertEqual(retorno['entrada'], Decimal(14))
        self.assertTrue(retorno['independente'])
        DetalheMovimentoEstoque.objects.filter(pk=segundo.pk).update(
            operacao_id='22222222-2222-2222-2222-222222222222')
        pagina = self.pagina()
        digitais = {r['pk'] for r in pagina if r['fonte']=='digital'}
        self.assertTrue({self.primeiro.pk,self.segundo.pk}.issubset(digitais))
        nota = next(r for r in pagina if r['fonte']=='venda' and r['documento']==1049)
        self.assertEqual(nota['evidencias_operacionais'], [])
        self.assertIsNone(nota['saldo_calculado'])

    def test_edicao_de_outro_produto_nao_invalida_referencia_disponivel(self):
        outro = self.inserir(Produto, nome='Outro SKU FICTICIO', unidade_compra='UN')
        self.inserir(ItemVenda, venda_id=996, produto=outro, quantidade=2,
                     unidade='UN', preco_unitario=1, valor_total=2)
        self.inserir(EventoVenda, venda_id=996, tipo_evento='quantidade_item_alterada',
            descricao='Quantidade alterada na nota: Outro SKU FICTICIO. De 1 UN para 2 UN. Total sera recalculado ao final da edicao.')
        pagina = self.pagina()
        anterior = next(r for r in pagina if r['fonte']=='venda' and r['documento']==996)
        self.assertTrue(anterior['referencia_habilitada'])
        self.assertEqual(anterior['saldo_registrado_operacional']['valor'], Decimal(28))
        self.conferir(pagina, 'b')

    def test_unidade_manual_unica_validacao_na_pagina_e_no_prefixo(self):
        manual = self.inserir(MovimentacaoEstoqueManual, produto=self.produto, tipo='ajuste',
            estoque_antes=5, estoque_depois=4, diferenca=-1, motivo='Ajuste FICTICIO', operador_nome='Operador')
        hora = self.hora-timedelta(minutes=15)
        MovimentacaoEstoqueManual.objects.filter(pk=manual.pk).update(criado_em=hora)
        movimento = self.inserir(MovimentoObservado, produto=self.produto, sequencia=3,
            antes=5, depois=4, origem='ajuste_manual', referencia=f'Movimentação manual #{manual.pk}', registrado_em=hora)
        self.detalhe(movimento, '33333333-3333-3333-3333-333333333333',
            acao='ajuste', documento_tipo='movimentacao_estoque_manual', documento_id=str(manual.pk),
            unidade_base=' un ', unidade_entrada=' un ', quantidade_entrada=4)
        pagina = self.pagina()
        ajuste = next(r for r in pagina if r['fonte']=='manual')
        self.assertEqual(ajuste['ajuste_manual']['unidade'], 'UN')
        nota = next(r for r in pagina if r['fonte']=='venda' and r['documento']==1049)
        self.assertEqual(nota['saldo_calculado']['valor'], Decimal(-9))
        # A second, incompatible attribution cannot be silently accepted by SQL
        # while the visible Python helper rejects it as ambiguous.
        incompativel = self.inserir(MovimentoObservado, produto=self.produto, sequencia=4,
            antes=7, depois=6, origem='ajuste_manual', referencia='Outra referencia',
            registrado_em=self.hora+timedelta(minutes=20))
        self.detalhe(incompativel, '44444444-4444-4444-4444-444444444444',
            acao='ajuste', documento_tipo='movimentacao_estoque_manual', documento_id=str(manual.pk),
            quantidade_entrada=6)
        pagina = self.pagina()
        self.assertEqual(next(r for r in pagina if r['fonte']=='manual')['ajuste_manual']['unidade'], '')
        self.assertIsNone(next(r for r in pagina if r['fonte']=='venda' and r['documento']==1049)['saldo_calculado'])
        MovimentoObservado.objects.bulk_create([MovimentoObservado(produto=self.produto,
            sequencia=10+n, antes=100, depois=99, origem='nao_identificada', referencia=f'FICTICIO {n}',
            registrado_em=self.hora-timedelta(hours=3)+timedelta(seconds=n)) for n in range(48)])
        pagina = self.pagina(numero=2)
        self.assertEqual(pagina.number, 1)
        nota = next(r for r in pagina if r['fonte']=='venda' and r['documento']==1049)
        self.assertIsNone(nota['saldo_calculado'])

    def test_html_distingue_nova_referencia_registrada_de_saldo_calculado(self):
        for numero, quantidade, minuto, snapshot in (
                (1050, 1, 30, dict(estoque_antes=78, estoque_movimentado=1,
                                 estoque_depois=77, estoque_unidade_snapshot='UN')),
                (1051, 2, 40, {})):
            venda = self.inserir(Venda, pk=numero, cliente=self.cliente,
                data_venda=self.hora.date(), tipo_pagamento='avista')
            Venda.objects.filter(pk=numero).update(criado_em=self.hora+timedelta(minutes=minuto))
            self.inserir(ItemVenda, venda=venda, produto=self.produto, quantidade=quantidade,
                unidade='UN', preco_unitario=1, valor_total=quantidade, **snapshot)
        pagina = self.pagina()
        registrada = next(r for r in pagina if r['fonte']=='venda' and r['documento']==1050)
        calculada = next(r for r in pagina if r['fonte']=='venda' and r['documento']==1051)
        self.assertIsNone(registrada['saldo_calculado'])
        self.assertEqual(registrada['saldo_registrado_operacional']['valor'], Decimal(77))
        self.assertEqual(calculada['saldo_calculado']['valor'], Decimal(75))
        self.assertEqual(calculada['saldo_calculado']['anterior'], Decimal(77))
        for visual in ('a', 'b'):
            html = self.renderizar(pagina, visual)
            for linha, esperado in ((registrada, '77 UN Saldo registrado'),
                                    (calculada, '75 UN Calculado pelos documentos')):
                corpo = re.search(f'data-registro="{linha["detalhe_id"]}"[^>]*>(.*?)</tr>',html,re.S).group(1)
                celulas = [' '.join(re.sub('<[^>]+>', ' ', c).split())
                    for c in re.findall(r'<t[dh]\b[^>]*>(.*?)</t[dh]>',corpo,re.S)]
                self.assertTrue(celulas[3].startswith(esperado), celulas)

    def test_prefixo_anterior_ao_intervalo_considera_compra_intermediaria(self):
        compra = self.inserir(Compra, data_compra=self.hora.date(), estoque_entrada_realizada=True,
            estoque_entrada_realizada_em=self.hora-timedelta(minutes=30), status='finalizada')
        self.inserir(ItemCompra, compra=compra, produto=self.produto, quantidade=3,
            unidade='UN', preco_unitario=1, valor_total=3)
        inicio = self.hora-timedelta(minutes=10)
        params = QueryDict('visual=b')
        pagina = cronologia(ItemCompra.objects.filter(produto=self.produto),
            ItemVenda.objects.filter(produto=self.produto, venda__criado_em__gte=inicio),
            ItemVendaRemovido.objects.none(),
            MovimentoObservado.objects.filter(produto=self.produto, registrado_em__gte=inicio),
            params, None, inicio, self.hora+timedelta(days=1), produto_id=self.produto.pk)
        nota = next(r for r in pagina if r['fonte']=='venda')
        self.assertEqual(nota['saldo_calculado']['valor'], Decimal(18))
        self.assertFalse(any(r['fonte']=='compra' for r in pagina))

    def test_vinculo_estruturado_agrupa_sem_referencia_textual_padrao(self):
        for ordem, movimento in enumerate((self.primeiro, self.segundo), 1):
            MovimentoObservado.objects.filter(pk=movimento.pk).update(referencia='Registro estruturado')
            self.detalhe(movimento, '55555555-5555-5555-5555-555555555555', ordem=ordem,
                item_referencia=str(self.item.pk))
        pagina = self.pagina()
        nota = next(r for r in pagina if r['fonte']=='venda' and r['documento']==1049)
        self.assertEqual(len(nota['evidencias_operacionais']), 2)
        self.assertFalse(any(r['fonte']=='digital' for r in pagina))
        self.assertEqual(nota['operacao_documental']['quantidade'], Decimal(13))

    def test_conversao_historica_na_pagina_e_entre_paginas(self):
        # No current catalog factor participates: document=1 PCT, frozen debit=13 UN.
        ItemVenda.objects.filter(pk=self.item.pk).update(quantidade=1, unidade='PCT',
            estoque_antes=None, estoque_depois=None, estoque_movimentado=None)
        MovimentoObservado.objects.filter(pk=self.segundo.pk).delete()
        MovimentoObservado.objects.filter(pk=self.primeiro.pk).update(antes=28, depois=15)
        self.detalhe(self.primeiro, '66666666-6666-6666-6666-666666666666',
            item_referencia=str(self.item.pk), quantidade_entrada=1, unidade_entrada='PCT',
            unidade_base='UN', conversao='multiplicacao', fator=13)
        Produto.objects.filter(pk=self.produto.pk).update(fator_conversao=99)
        pagina = self.pagina()
        nota = next(r for r in pagina if r['fonte']=='venda' and r['documento']==1049)
        self.assertEqual(nota['saldo_calculado']['valor'], Decimal(15))
        self.assertEqual(nota['operacao_documental']['quantidade'], Decimal(1))
        venda = self.inserir(Venda, pk=1052, tipo_pagamento='avista', data_venda=self.hora.date())
        Venda.objects.filter(pk=venda.pk).update(criado_em=self.hora+timedelta(minutes=20))
        self.inserir(ItemVenda, venda=venda, produto=self.produto, quantidade=2,
            unidade='UN', preco_unitario=1, valor_total=2)
        # 49 informative records put the converted sale on the preceding page.
        for n in range(49):
            remocao = self.inserir(ItemVendaRemovido, venda_id=996, produto=self.produto,
                produto_nome_snapshot=self.produto.nome, quantidade_snapshot=1,
                unidade_snapshot='UN', status='removido', estoque_devolvido=False,
                preco_unitario_snapshot=1, valor_total_snapshot=1)
            ItemVendaRemovido.objects.filter(pk=remocao.pk).update(
                criado_em=self.hora-timedelta(minutes=50)+timedelta(seconds=n))
        ultima = next(r for r in self.pagina(numero=2) if r['fonte']=='venda' and r['documento']==1052)
        self.assertEqual(ultima['saldo_calculado']['valor'], Decimal(13))

    def test_venda_editada_nao_recebe_snapshot_de_horario_nao_comprovado(self):
        evento = self.inserir(EventoVenda, venda_id=1049, tipo_evento='quantidade_item_alterada',
            descricao=f'Quantidade alterada na nota: {self.produto.nome}. De 27 UN para 13 UN.')
        EventoVenda.objects.filter(pk=evento.pk).update(criado_em=self.hora+timedelta(minutes=9))
        pagina = self.pagina()
        nota = next(r for r in pagina if r['fonte']=='venda' and r['documento']==1049)
        self.assertTrue(nota['documento_atual'])
        self.assertFalse(nota['referencia_habilitada'])
        self.assertIsNone(nota['saldo_registrado_operacional'])
        self.assertEqual(nota['saldo_calculado']['valor'], Decimal(15))

    def test_evidencia_de_documento_anterior_nao_vira_movimento_no_intervalo(self):
        inicio = self.hora+timedelta(minutes=5)
        params = QueryDict('visual=b')
        pagina = cronologia(ItemCompra.objects.filter(produto=self.produto),
            ItemVenda.objects.filter(produto=self.produto, venda__criado_em__gte=inicio),
            ItemVendaRemovido.objects.none(),
            MovimentoObservado.objects.filter(produto=self.produto, registrado_em__gte=inicio),
            params, None, inicio, self.hora+timedelta(days=1), produto_id=self.produto.pk)
        self.assertFalse(any(r['fonte']=='digital' for r in pagina))

    def test_ajuste_aceita_unidade_base_com_conversao_historica_comprovada(self):
        manual = self.inserir(MovimentacaoEstoqueManual, produto=self.produto, tipo='ajuste',
            estoque_antes=5, estoque_depois=4, diferenca=-1)
        hora = self.hora-timedelta(minutes=15)
        MovimentacaoEstoqueManual.objects.filter(pk=manual.pk).update(criado_em=hora)
        movimento = self.inserir(MovimentoObservado, produto=self.produto, sequencia=3,
            antes=5, depois=4, origem='ajuste_manual',
            referencia=f'Movimentação manual #{manual.pk}', registrado_em=hora)
        self.detalhe(movimento, '77777777-7777-7777-7777-777777777777',
            acao='ajuste', documento_tipo='movimentacao_estoque_manual', documento_id=str(manual.pk),
            unidade_entrada='PCT', unidade_base='UN', quantidade_entrada=2,
            conversao='multiplicacao', fator=2)
        pagina = self.pagina()
        ajuste = next(r for r in pagina if r['fonte']=='manual')
        self.assertEqual(ajuste['ajuste_manual']['unidade'], 'UN')
        nota = next(r for r in pagina if r['fonte']=='venda' and r['documento']==1049)
        self.assertEqual(nota['saldo_calculado']['valor'], Decimal(-9))

    def test_1185_saida_e_devolucao_documentais_sem_dois_cartoes_tecnicos(self):
        hora_venda = self.hora+timedelta(minutes=30)
        hora_retorno = self.hora+timedelta(minutes=45)
        venda = self.inserir(Venda, pk=1185, cliente=self.cliente,
            data_venda=hora_venda.date(), tipo_pagamento='avista')
        Venda.objects.filter(pk=venda.pk).update(criado_em=hora_venda)
        item = self.inserir(ItemVenda, venda=venda, produto=self.produto, quantidade=5,
            unidade='UN', preco_unitario=1, valor_total=5)
        saida = self.inserir(MovimentoObservado, produto=self.produto, sequencia=10,
            antes=15, depois=10, origem='venda', referencia='Venda #1185', registrado_em=hora_venda)
        retorno = self.inserir(MovimentoObservado, produto=self.produto, sequencia=11,
            antes=10, depois=12, origem='devolucao_venda', referencia='Venda #1185', registrado_em=hora_retorno)
        documento = self.inserir(ItemVendaRemovido, venda=venda, produto=self.produto,
            produto_nome_snapshot=self.produto.nome, item_venda_original_id=item.pk,
            quantidade_snapshot=2, unidade_snapshot='UN', preco_unitario_snapshot=1,
            valor_total_snapshot=2, status='removido', estoque_devolvido=True,
            estoque_devolvido_em=hora_retorno)
        ItemVendaRemovido.objects.filter(pk=documento.pk).update(criado_em=hora_retorno)
        self.detalhe(retorno, '88888888-8888-8888-8888-888888888888', acao='devolucao',
            documento_tipo='item_venda_removido', documento_id=str(documento.pk),
            item_referencia=str(item.pk), quantidade_entrada=2)
        for visual in ('a', 'b'):
            pagina = self.pagina(visual)
            linhas = [r for r in pagina if r['documento']==1185]
            self.assertEqual([r['fonte'] for r in linhas], ['venda', 'remocao'])
            self.assertEqual(linhas[0]['operacao_documental']['quantidade'], Decimal(5))
            self.assertEqual(linhas[1]['operacao_documental']['direcao'], 'entrada')
            self.assertEqual(linhas[1]['operacao_documental']['quantidade'], Decimal(2))
            self.assertEqual(linhas[1]['instante'], hora_retorno)
            self.assertEqual(linhas[1]['saldo_calculado']['valor'], Decimal(12))
            html = self.renderizar(pagina, visual)
            self.assertNotIn(f'data-registro="registro-digital-{saida.pk}"', html)
            self.assertNotIn(f'data-registro="registro-digital-{retorno.pk}"', html)
            corpo_saida = re.search(f'data-registro="registro-venda-{item.pk}"[^>]*>(.*?)</tr>', html, re.S).group(1)
            corpo_retorno = re.search(f'data-registro="registro-remocao-{documento.pk}"[^>]*>(.*?)</tr>', html, re.S).group(1)
            self.assertIn('Saída −5 UN', corpo_saida)
            self.assertIn('Entrada +2 UN', corpo_retorno)
            self.assertIn('href="/vendas/1185/"', corpo_saida)
            self.assertIn('href="/vendas/1185/"', corpo_retorno)
        self.assertEqual(MovimentoObservado.objects.filter(pk__in=(saida.pk,retorno.pk)).count(), 2)

    def test_consulta_separada_renderiza_todas_as_evidencias_e_link_de_volta(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from .historico import investigar
        inventario = SimpleNamespace(finalizada_em=None, criado_em=self.hora+timedelta(days=1))
        item = SimpleNamespace(produto_id=self.produto.pk, inventario=inventario,
            cadastro={'nome': self.produto.nome})
        rodada = SimpleNamespace(item=item, ok_em=None)
        periodo = dict(inicio=self.hora-timedelta(days=1), fim=self.hora+timedelta(days=1),
            periodo='completo', erro_periodo='')
        params = QueryDict('visual=b&consulta=evidencias&periodo=completo')
        with patch('inventario.historico.selecionar', return_value=periodo):
            contexto = investigar(rodada, params)
        self.assertIsNone(contexto['extrato'])
        self.assertEqual({m.pk for m in contexto['movimentos']}, {self.primeiro.pk,self.segundo.pk})
        self.assertNotIn('consulta=evidencias', contexto['operacoes_url'])
        raiz = Path(__file__).parent/'templates'/'inventario'
        arquivos = {'inventario/'+p.name: '' for p in raiz.glob('*.html')}
        arquivos['inventario/base.html'] = '{% block content %}{% endblock %}'
        arquivos['inventario/historico.html'] = (raiz/'historico.html').read_text(encoding='utf-8')
        arquivos['inventario/historico_paginacao.html'] = (raiz/'historico_paginacao.html').read_text(encoding='utf-8')
        motor = Engine(libraries={'horarios_documentos': 'estoque.templatetags.horarios_documentos',
            'static':'django.templatetags.static', 'i18n':'django.templatetags.i18n'},
            loaders=[('django.template.loaders.locmem.Loader', arquivos)])
        html = motor.get_template('inventario/historico.html').render(Context(contexto))
        self.assertIn('Evidências técnicas do estoque', html)
        self.assertIn('Voltar às movimentações', html)
        for m in (self.primeiro,self.segundo):
            self.assertIn(f'data-evidencia="{m.pk}"', html)
        self.assertNotIn('data-registro="registro-digital-', html)

    def test_paginacao_operacional_considera_lacuna_tecnica_oculta(self):
        # 49 documentary receipts + the known reference fill the first page.
        for n in range(49):
            compra = self.inserir(Compra, data_compra=self.hora.date(), estoque_entrada_realizada=True,
                estoque_entrada_realizada_em=self.hora-timedelta(hours=2)+timedelta(seconds=n), status='finalizada')
            self.inserir(ItemCompra, compra=compra, produto=self.produto, quantidade=1,
                unidade='UN', preco_unitario=1, valor_total=1)
        lacuna = self.inserir(MovimentoObservado, produto=self.produto, sequencia=40,
            antes=28, depois=20, origem='nao_identificada', referencia='Sem identidade comercial',
            registrado_em=self.hora-timedelta(minutes=20))
        pagina = self.pagina(numero=2)
        self.assertEqual(pagina.number, 2)
        self.assertEqual(pagina.paginator.count, 51)
        self.assertFalse(any(r['fonte']=='digital' for r in pagina))
        nota = next(r for r in pagina if r['documento']==1049)
        self.assertIsNone(nota['saldo_calculado'])
        self.assertTrue(MovimentoObservado.objects.filter(pk=lacuna.pk).exists())

    def test_cancelamento_nao_apaga_saida_original_com_snapshot_de_efeito_comprovado(self):
        Venda.objects.filter(pk=1049).update(cancelada=True)
        pagina = self.pagina()
        nota = next(r for r in pagina if r['fonte']=='venda' and r['documento']==1049)
        self.assertTrue(nota['operacao_documental']['efetivada'])
        self.assertEqual(nota['operacao_documental']['quantidade'], Decimal(13))
        self.assertIn('Saída −13 UN', self.renderizar(pagina, 'b'))
