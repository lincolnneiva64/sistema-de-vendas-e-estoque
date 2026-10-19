"""Read-only investigation. Documents never contribute again to journal totals.

The journal has no immutable unit/conversion snapshot. Consequently its numeric
changes cannot safely be summed into physical quantities across historical units.
"""
from datetime import timedelta
from django.core.paginator import Paginator
from django.db.models import Sum, OuterRef, Subquery
from django.utils import timezone
from estoque.models import ItemCompra, ItemVenda, ItemVendaRemovido
from .models import MovimentoObservado
from .rastreabilidade import ORIGENS
from .extrato import cronologia
from .periodos_historico import selecionar


def investigar(rodada, params):
    item = rodada.item
    referencia = rodada.ok_em or item.inventario.finalizada_em or item.inventario.criado_em
    compras = ItemCompra.objects.filter(
        produto_id=item.produto_id, compra__estoque_entrada_realizada=True,
        compra__estoque_entrada_realizada_em__lte=referencia,
    ).select_related('compra__fornecedor').order_by('-compra__estoque_entrada_realizada_em', '-compra_id', 'pk')
    # Paginate purchase documents too; an item is not assumed unique per purchase.
    primeiro_item = ItemCompra.objects.filter(produto_id=item.produto_id,
        compra_id=OuterRef('compra_id')).order_by('pk').values('pk')[:1]
    documentos = compras.filter(pk=Subquery(primeiro_item))
    opcoes = Paginator(documentos, 30).get_page(params.get('compras_pagina'))
    selecionada = None
    if params.get('compra', '').isdigit() and params.get('periodo', 'compra') == 'compra':
        selecionada = compras.filter(compra_id=params['compra']).first()
    if selecionada is None:
        selecionada = compras.first()
    periodo = selecionar(params, referencia, selecionada, documentos, item.produto_id)
    inicio, fim = periodo['inicio'], periodo['fim']
    params = params.copy()
    params['periodo'] = periodo['periodo']
    movimentos = MovimentoObservado.objects.filter(produto_id=item.produto_id)
    vendas = ItemVenda.objects.filter(produto_id=item.produto_id).select_related('venda')
    removidos = ItemVendaRemovido.objects.filter(produto_id=item.produto_id).select_related('venda')
    if inicio and not periodo['erro_periodo']:
        movimentos = movimentos.filter(registrado_em__gte=inicio, registrado_em__lt=fim)
        # Sales have a business date only: boundary-day documents are candidates,
        # not proof they happened after a purchase or before a physical count.
        datas = (timezone.localtime(inicio).date(), timezone.localtime(fim-timedelta(microseconds=1)).date())
        vendas = vendas.filter(venda__data_venda__range=datas)
        removidos = removidos.filter(criado_em__gte=inicio, criado_em__lt=fim)
    else:
        movimentos, vendas, removidos = movimentos.none(), vendas.none(), removidos.none()
    pagina_movimentos = Paginator(movimentos.order_by('sequencia'), 50).get_page(params.get('movimentos_pagina'))
    for movimento in pagina_movimentos:
        movimento.rotulo = ORIGENS.get(movimento.origem, ORIGENS['nao_identificada'])
    contexto = dict(rodada=rodada, item=item, referencia=referencia, inicio=inicio, fim=fim,
                extrato=cronologia(compras, vendas, removidos, movimentos, params, selecionada, inicio, fim,
                                   produto_id=item.produto_id, rodada=rodada),
                compra=selecionada, compras=opcoes,
                itens_compra=(compras.filter(compra_id=selecionada.compra_id).order_by().values('unidade')
                              .annotate(total=Sum('quantidade'))) if selecionada else [],
                intervalo=params.get('intervalo', ''),
                vendas=Paginator(vendas.order_by('venda__data_venda', 'pk'), 50).get_page(params.get('vendas_pagina')),
                removidos=Paginator(removidos.order_by('criado_em', 'pk'), 50).get_page(params.get('removidos_pagina')),
                movimentos=pagina_movimentos,
                diferenca=(rodada.contado_base - rodada.esperado) if rodada.ok_em and rodada.contado_base is not None and rodada.esperado is not None else None,
                fisica=rodada.contado_base if rodada.ok_em else None,
                esperado=rodada.esperado if rodada.ok_em else None)
    contexto.update(periodo)
    completo = params.copy()
    completo['periodo'] = 'completo'
    completo.pop('extrato_pagina', None)
    contexto['completo_url'] = '?' + completo.urlencode()
    contexto['visual'] = 'a' if params.get('visual') == 'a' else 'b'
    contexto['visuais'] = []
    for valor, rotulo in (('a', 'A · Tabela compacta'), ('b', 'B · Linha do tempo')):
        consulta = params.copy()
        consulta['visual'] = valor
        contexto['visuais'].append(dict(valor=valor, rotulo=rotulo, url='?'+consulta.urlencode()))

    for chave in ('compras', 'vendas', 'removidos', 'movimentos'):
        pagina = contexto[chave]
        for sentido, numero in (('anterior', pagina.number - 1), ('proxima', pagina.number + 1)):
            consulta = params.copy()
            consulta[chave + '_pagina'] = numero
            if selecionada:
                consulta['compra'] = selecionada.compra_id
            setattr(pagina, sentido + '_url', '?' + consulta.urlencode())
    return contexto
