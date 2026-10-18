"""One database-paginated chronology of documentary and observer evidence.

Balances require independent evidence. Sources are never summed twice or matched
by approximate time, and current product conversions are never used.
"""
import re

from django.core.paginator import Paginator
from django.db.models import (BooleanField, CharField,
                              DecimalField, F, IntegerField, OuterRef, Subquery, Value)
from django.db.models.functions import Cast, TruncDate
from django.urls import reverse
from estoque.models import ItemVenda

from .rastreabilidade import ORIGENS
from .saldos_historicos import analisar


COLUNAS = ('pk', 'dia', 'instante', 'fonte', 'documento', 'quantidade', 'unidade',
           'pessoa', 'cancelada', 'origem', 'antes', 'depois', 'referencia', 'devolvido')


def cronologia(compras, vendas, removidos, movimentos, params, selecionada, inicio, fim):
    decimal = DecimalField(max_digits=12, decimal_places=3)

    def normalizar(qs, fonte, dia, instante, **campos):
        defaults = dict(documento=Value(None, IntegerField()), quantidade=Value(None, decimal),
            unidade=Value('', CharField()), pessoa=Value('', CharField()),
            cancelada=Value(False, BooleanField()), origem=Value('', CharField()),
            antes=Cast(Value(None), decimal), depois=Cast(Value(None), decimal),
            referencia=Value('', CharField()), devolvido=Value(False, BooleanField()))
        defaults.update(campos)
        return qs.order_by().annotate(dia=dia, instante=instante,
            fonte=Value(fonte, CharField()), **defaults).values(*COLUNAS)

    recebidas = compras.filter(compra__estoque_entrada_realizada_em__gte=inicio,
        compra__estoque_entrada_realizada_em__lt=fim) if inicio else compras.none()
    # Alias names must not conflict with concrete model fields (unidade etc.).
    # Project all sources through values() first, so annotate can reuse these names.
    compras_q = normalizar(recebidas.values('pk'), 'compra',
        TruncDate('compra__estoque_entrada_realizada_em'), F('compra__estoque_entrada_realizada_em'),
        documento=F('compra_id'), quantidade=F('quantidade'), unidade=F('unidade'),
        pessoa=F('compra__fornecedor__nome'), cancelada=F('compra__cancelada'))
    vendas_q = normalizar(vendas.values('pk'), 'venda', TruncDate('venda__criado_em'),
        F('venda__criado_em'), documento=F('venda_id'),
        quantidade=F('quantidade'), unidade=F('unidade'), pessoa=F('venda__operador'),
        cancelada=F('venda__cancelada'), origem=F('venda__tipo_pagamento'),
        devolvido=F('venda__estoque_devolvido_cancelamento'))
    removidos_q = normalizar(removidos.values('pk'), 'remocao', TruncDate('criado_em'), F('criado_em'),
        documento=F('venda_id'), quantidade=F('quantidade_snapshot'), unidade=F('unidade_snapshot'),
        pessoa=F('operador'), origem=F('status'), devolvido=F('estoque_devolvido'))
    movimentos_q = normalizar(movimentos.values('pk'), 'digital', TruncDate('registrado_em'), F('registrado_em'),
        origem=F('origem'), antes=F('antes'), depois=F('depois'), referencia=F('referencia'))
    # One return per sale/product, irrespective of its original business date.
    # Its current items do not prove the quantity/unit physically returned.
    devolucoes = ItemVenda.objects.none()
    sem_data = False
    if selecionada and inicio:
        todos = ItemVenda.objects.filter(produto_id=selecionada.produto_id)
        primeiro = todos.filter(venda_id=OuterRef('venda_id')).order_by('pk').values('pk')[:1]
        devolucoes = todos.filter(pk=Subquery(primeiro),
            venda__estoque_devolvido_cancelamento_em__gte=inicio,
            venda__estoque_devolvido_cancelamento_em__lt=fim)
        sem_data = todos.filter(venda__estoque_devolvido_cancelamento=True,
            venda__estoque_devolvido_cancelamento_em__isnull=True).exists()
    devolucoes_q = normalizar(devolucoes.values('pk'), 'cancelamento',
        TruncDate('venda__estoque_devolvido_cancelamento_em'), F('venda__estoque_devolvido_cancelamento_em'),
        documento=F('venda_id'), devolvido=F('venda__estoque_devolvido_cancelamento'))
    unificada = compras_q.union(vendas_q, removidos_q, movimentos_q, devolucoes_q, all=True).order_by(
        'dia', 'instante', 'fonte', 'pk')
    pagina = Paginator(unificada, 50).get_page(params.get('extrato_pagina'))
    pagina.devolucao_sem_data = sem_data
    for linha in pagina:
        fonte = linha['fonte']
        linha.update(entrada=None, saida=None, saldo=None, url='', classe='pendente', fonte_texto='Documento atual')
        if fonte == 'compra':
            linha.update(titulo=f"Compra #{linha['documento']}", classe='compra', entrada=linha['quantidade'],
                nota='Entrada documentada · Fornecedor: ' + (linha['pessoa'] or 'Não disponível'),
                url=reverse('estoque:compras_detalhe', args=[linha['documento']]))
        elif fonte == 'venda':
            tipo = 'Consumo próprio' if linha['origem'] == 'consumo_proprio' else 'Venda'
            linha.update(titulo=f"{tipo} #{linha['documento']}", classe='venda', saida=linha['quantidade'],
                nota='Quantidade documental · Responsável: ' + (linha['pessoa'] or 'Não disponível'),
                url=reverse('estoque:venda_detalhe', args=[linha['documento']]))
        elif fonte == 'cancelamento':
            linha.update(titulo=f"Devolução por cancelamento · Venda #{linha['documento']}",
                nota='Data efetiva registrada da devolução. Quantidade e unidade devolvidas não comprovadas; não somada novamente aos registros digitais.',
                saldo_aviso='Sequência interrompida: não há snapshot imutável da quantidade e unidade devolvidas.',
                saldo_continuidade='Sequência interrompida', saldo_pontual=True,
                url=reverse('estoque:venda_detalhe', args=[linha['documento']]))
        elif fonte == 'remocao':
            linha.update(titulo=f"Remoção/devolução · Venda #{linha['documento']}",
                nota='Estado atual: ' + linha['origem'] + (' · Estoque devolvido' if linha['devolvido'] else ' · Sem devolução de estoque confirmada'),
                url=reverse('estoque:venda_detalhe', args=[linha['documento']]))
        else:
            linha.update(titulo=ORIGENS.get(linha['origem'], ORIGENS['nao_identificada']),
                fonte_texto='Registro digital', nota=linha['referencia'] or 'Origem sem documento identificado',
                classe='compra' if linha['origem'] == 'compra' else 'venda' if linha['origem'] == 'venda' else 'pendente')
            # Only explicit document references can create links; no fuzzy matching.
            referencia = re.fullmatch(r'(Compra|Venda) #(\d+)', linha['referencia'])
            if referencia:
                destino = 'compras_detalhe' if referencia[1] == 'Compra' else 'venda_detalhe'
                linha['url'] = reverse('estoque:' + destino, args=[int(referencia[2])])
        if linha['cancelada']:
            linha['nota'] += ' · Cancelada atualmente'
        linha['detalhe_id'] = f"registro-{fonte}-{linha['pk']}"
    if selecionada:
        analisar(selecionada.produto_id, pagina, fim)
    for sentido, numero in (('anterior', pagina.number - 1), ('proxima', pagina.number + 1)):
        consulta = params.copy()
        consulta['extrato_pagina'] = numero
        if selecionada:
            consulta['compra'] = selecionada.compra_id
        setattr(pagina, sentido + '_url', '?' + consulta.urlencode())
    return pagina
