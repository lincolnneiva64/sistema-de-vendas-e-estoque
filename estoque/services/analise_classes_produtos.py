"""Consolidated read-only presentation of existing commercial and frozen-cost rules.

One item query and batched evidence queries, never a query per product. Snapshot
validation is shared with the individual product report; no current cost fallback.
"""
from collections import defaultdict, Counter
from decimal import Decimal
from fractions import Fraction
from unicodedata import normalize, combining

from django.db.models import Count, Sum

from estoque.models import Produto, Categoria
from .analise_desempenho import desempenho_comercial
from .investigacao_comercial import chave_classe, rotulo_classe
from .relatorios_produtos import itens_comerciais, vendas_comerciais
from .relatorios_produtos_indicadores import validar_custos, dinheiro_exato
from inventario.saldo_documental import unidade, numero, projecao_operacional

ZERO = Decimal('0')
CENTAVO = Decimal('.01')
ORDENS = ('faturamento','faturamento_menor','lucro','margem','quantidade','queda','nome','nome_desc',
          'cmv','custo_medio','preco_medio','lucro_unitario')


def nome_ordenacao(nome):
    return ''.join(c for c in normalize('NFKD', nome.casefold()) if not combining(c))


def categorias_disponiveis():
    # Choices describe the official catalog, not every legacy product label.
    # Keep the same confirmed classification as the rows without filtering sales.
    chaves = {chave_classe(c) for c in Categoria.objects.filter(ativa=True)
        .exclude(nome='').order_by().values_list('nome', flat=True)}
    return sorted(((c, rotulo_classe(c)) for c in chaves), key=lambda c: (nome_ordenacao(c[1]), c[0]))


def _grupo(**campos):
    return dict(faturamento=ZERO, custo=ZERO, receita_coberta=ZERO, quantidade_coberta=ZERO,
                itens=0, com_custo=0, quantidades=defaultdict(lambda: ZERO),
                anterior=ZERO, atual_comparado=ZERO, quantidade_anterior=ZERO,
                quantidade_atual_comparada=ZERO, motivos=Counter(),
                financeiro_unidades=defaultdict(lambda: dict(custo=ZERO,
                    quantidade_coberta=ZERO, itens=0, com_custo=0)), **campos)


def _finalizar(g, comparavel):
    unidades = g['quantidades']
    unidade = next(iter(unidades)) if len(unidades) == 1 else ''
    quantidade = unidades.get(unidade) if unidade else None
    por_unidade = []
    for u, q in sorted(unidades.items()):
        financeiro = g['financeiro_unidades'][u]
        coberta = financeiro['quantidade_coberta']
        por_unidade.append(dict(unidade=u, quantidade=q if u else None, **financeiro,
            completa=financeiro['itens'] > 0 and financeiro['itens'] == financeiro['com_custo'],
            custo_medio=(financeiro['custo']/coberta).quantize(CENTAVO) if u and coberta > 0 else None))
    g.update(unidade=unidade, quantidade=quantidade,
             quantidades=por_unidade,
             completa=g['itens'] > 0 and g['itens'] == g['com_custo'])
    lucro = g['receita_coberta'] - g['custo'] if g['com_custo'] else None
    g['lucro'] = lucro
    g['margem'] = (lucro / g['receita_coberta'] * 100).quantize(CENTAVO) if lucro is not None and g['receita_coberta'] > 0 else None
    g['preco_medio'] = (g['faturamento'] / quantidade).quantize(CENTAVO) if quantidade is not None and quantidade > 0 else None
    q = g['quantidade_coberta']
    g['custo_medio'] = (g['custo'] / q).quantize(CENTAVO) if unidade and q > 0 else None
    g['lucro_unitario'] = (lucro / q).quantize(CENTAVO) if unidade and q > 0 and lucro is not None else None
    g['diferenca'] = g['atual_comparado'] - g['anterior'] if comparavel else None
    g['diferenca_quantidade'] = (g['quantidade_atual_comparada'] - g['quantidade_anterior']) if comparavel and unidade else None
    # Templates resolve dictionary keys before attributes. Counter['items']
    # would be zero instead of the mapping method when coverage is partial.
    g['motivos'] = dict(g['motivos'])
    return g


def _somar(destino, origem):
    for campo in ('faturamento', 'custo', 'receita_coberta', 'quantidade_coberta', 'itens', 'com_custo',
                  'anterior', 'atual_comparado', 'quantidade_anterior', 'quantidade_atual_comparada'):
        destino[campo] += origem[campo]
    destino['motivos'].update(origem['motivos'])
    for u, q in origem['quantidades'].items():
        destino['quantidades'][u] += q
    for u, dados in origem['financeiro_unidades'].items():
        for campo, valor in dados.items():
            destino['financeiro_unidades'][u][campo] += valor


def _medias_equivalentes(produto, itens, validacoes, comerciais=None):
    """Presentation denominators only; never change money or cost acceptance.

    Keep per-item exact conversions and the registered commercial denominator.
    Fractions avoid accumulating rounded stock debits. A direct single-unit
    row already has the right denominator and needs no conversion detail.
    """
    if len(produto['apresentacoes'])==1 and (comerciais is None or
            produto['apresentacoes'][0]['unidade']==comerciais['principal']):
        produto['unidade_medias']=produto['unidade']
        return
    bases={unidade(i['custo_unidade_snapshot']) for i in itens if validacoes[i['pk']]['custo'] is not None}
    bases.discard('')
    base=next(iter(bases)) if len(bases)==1 else ''
    comercial = comerciais or {}
    if comercial:
        base = comercial['principal']
    # Keep the packaging cost base as the commercial denominator. A unique
    # relation also converts uncensused commercial quantities, independently
    # of whether their own cost is accepted. Never use rounded stock debits.
    relacoes = defaultdict(set)
    for item in itens:
        prova = validacoes[item['pk']]['prova']
        if not prova:
            continue
        if prova.get('legado'):
            origem = prova['unidade_base']
            destino = item['unidade_comercial']
            quantidade_base = prova['quantidade_base']
        else:
            origem = unidade(prova['detalhe'].unidade_base)
            destino = item['unidade_comercial']
            quantidade_base = abs(prova['prova']['exato'])
        if origem == base and destino != base and quantidade_base > 0:
            relacoes[destino].add(Fraction(item['quantidade']) / quantidade_base)
    unidade_media, fator_media = base, Fraction(1)
    fatores = {u: next(iter(f)) for u, f in relacoes.items() if len(f) == 1}
    total=coberta=Fraction(0)
    impedimentos=[]
    provas=[]
    for i in itens:
        v=validacoes[i['pk']];q=None
        if comercial:
            q = comercial['quantidades'].get(i['pk'])
        elif base and i['unidade_comercial']==base:
            q=Fraction(i['quantidade'])
        elif i['unidade_comercial'] in fatores:
            # The commercial quantity is already in the requested unit. Its
            # price denominator does not require accepting the item's cost.
            # A per-item proof takes precedence when historical factors vary.
            q = Fraction(i['quantidade']) / fatores[i['unidade_comercial']]
        elif base and v['prova']:
            p=v['prova']
            if p.get('legado') and p['unidade_base']==base:
                q=p['quantidade_base']
            elif not p.get('legado') and unidade(p['detalhe'].unidade_base)==base:
                q=abs(p['prova']['exato'])
        if q is None:
            impedimentos.append(dict(item_id=i['pk'],venda_id=i['venda_id'],data=i['venda__data_venda'],
                unidade=i['unidade_comercial'],motivo=(v['motivo'] or
                    f"Não há conversão documentada de {i['unidade_comercial']} para {base or 'uma base comum'} neste item.")))
        else:
            total+=q
            if v['custo'] is not None:coberta+=q
            provas.append(dict(item_id=i['pk'],quantidade_numerador=q.numerator,
                quantidade_denominador=q.denominator,evidencia=v['evidencia']))
    if not comercial and len(bases)!=1:
        impedimentos.insert(0,dict(motivo='As unidades-base dos custos validados não têm uma conversão comum documentada: '+
            (', '.join(sorted(bases)) or 'nenhuma base de custo validada')+'.'))
    completa=bool(base) and not impedimentos
    total *= fator_media
    coberta *= fator_media
    produto.update(unidade_medias=unidade_media,medias_equivalentes=dict(unidade=unidade_media,
        unidade_custo=base, fator_base_numerador=fator_media.numerator,
        fator_base_denominador=fator_media.denominator,
        quantidade_total=(Decimal(total.numerator)/Decimal(total.denominator)) if completa else None,
        quantidade_coberta=(Decimal(coberta.numerator)/Decimal(coberta.denominator)) if base else None,
        quantidade_total_numerador=total.numerator if completa else None,
        quantidade_total_denominador=total.denominator if completa else None,
        quantidade_coberta_numerador=coberta.numerator,quantidade_coberta_denominador=coberta.denominator,
        impedimentos=impedimentos,provas=provas,
        aliases_comerciais=comercial.get('aliases',{})),
        custo_medio=dinheiro_exato(Fraction(produto['custo'])/coberta)
            if base and coberta>0 else None,
        preco_medio=dinheiro_exato(Fraction(produto['faturamento'])/total) if completa and total>0 else None,
        lucro_unitario=dinheiro_exato(Fraction(produto['lucro'])/coberta)
            if base and produto['lucro'] is not None and coberta>0 else None)
    if completa:
        produto.update(unidade=unidade_media,
                       quantidade=Decimal(total.numerator)/Decimal(total.denominator))


def _comerciais_por_produto(itens_por_produto, validacoes):
    """Deterministic registered packaging, with documented historic aliases.

    Inventory selections establish packaging size, not cost availability.
    A renamed base (FD to CT, both 100 UN) is local to this product, never a
    global unit alias. Per-sale exact attribution takes precedence over current
    factors; a rounded stock quantity is never used as the denominator.
    """
    from inventario.models import ItemInventario
    candidates = set()
    for pid, items in itens_por_produto.items():
        first = items[0]
        main = unidade(first.get('produto__unidade_compra'))
        secondary = unidade(first.get('produto__unidade_venda_2'))
        if any(i['unidade_comercial'] not in (main, secondary) for i in items):
            candidates.add(pid)
    history = defaultdict(list)
    if candidates:
        for row in ItemInventario.objects.filter(produto_id__in=candidates).values('pk','produto_id','cadastro'):
            history[row['produto_id']].append(row)
    result = {}
    for pid, items in itens_por_produto.items():
        first = items[0]
        main = unidade(first.get('produto__unidade_compra'))
        secondary = unidade(first.get('produto__unidade_venda_2'))
        factor = numero(first.get('produto__fator_conversao'))
        if not main:
            continue
        aliases = {main: Fraction(1)}
        if factor and factor > 0 and secondary and secondary != main:
            historic = defaultdict(set)
            for row in history[pid]:
                c=row['cadastro']
                f=numero(c.get('fator'))
                if unidade(c.get('secundaria')) == secondary and f and f>0 and c.get('fracionado') is True:
                    historic[unidade(c.get('base'))].add(Fraction(f))
            for old, factors in historic.items():
                if old != main and len(factors)==1:
                    aliases[old]=next(iter(factors))/Fraction(factor)
        quantities = {}
        for i in items:
            u=i['unidade_comercial']
            p=validacoes[i['pk']]['prova']
            if u in aliases:
                quantities[i['pk']]=Fraction(i['quantidade'])*aliases[u]
            elif p:
                old=p['unidade_base'] if p.get('legado') else unidade(p['detalhe'].unidade_base)
                exact=p['quantidade_base'] if p.get('legado') else abs(p['prova']['exato'])
                if old in aliases:
                    quantities[i['pk']]=exact*aliases[old]
            elif (u==secondary and factor and factor>0
                    and (validacoes[i['pk']].get('evidencia') or {}).get('conversao_historica_comprovada') is not False
                    and not validacoes[i['pk']]['motivo'].startswith((
                        'Unidades ou fator histórico contraditórios',
                        'Quantidade vendida e baixa histórica não conciliam'))
                    and unidade(i['estoque_unidade_snapshot'])==main
                    and i.get('estoque_movimentado')==projecao_operacional(i['quantidade'],'divisao',factor)
                    and i['estoque_antes'] is not None and i['estoque_depois'] is not None
                    and i['estoque_antes']-i['estoque_depois']==i['estoque_movimentado']):
                quantities[i['pk']]=Fraction(i['quantidade'])/Fraction(factor)
        # An unrelated current configuration cannot override historic operands.
        if len(quantities)==len(items):
            result[pid]=dict(principal=main,quantidades=quantities,
                aliases={u:dict(numerador=f.numerator,denominador=f.denominator) for u,f in aliases.items()})
    return result


def analise_consolidada(inicio, fim, *, hoje, pesquisa='', classe_de='', classe_ate='',
                       ordem='faturamento', comparacao='anterior'):
    if ordem not in ORDENS:
        raise ValueError('Ordenação inválida.')
    desempenho = desempenho_comercial(inicio, fim, hoje, comparacao)
    comparavel = desempenho.get('comparavel', False)
    base = itens_comerciais().filter(venda__data_venda__range=(inicio, fim))
    campos = ('pk', 'produto_id', 'produto__nome', 'produto__categoria', 'venda_id', 'venda__data_venda',
              'unidade_comercial', 'quantidade', 'valor_total', 'estoque_antes', 'estoque_depois', 'estoque_movimentado',
              'estoque_unidade_snapshot', 'custo_unitario_snapshot', 'custo_total_snapshot',
              'custo_unidade_snapshot', 'custo_origem_snapshot',
              'produto__unidade_compra','produto__unidade_venda_2','produto__fator_conversao')
    itens = list(base.values(*campos))
    validacoes = validar_custos(itens)
    grupos = {}
    itens_por_produto=defaultdict(list)
    total_itens_periodo = ZERO
    def obter(produto_id, nome, categoria, unidade):
        chave = chave_classe(categoria) if produto_id is not None else 'sem-produto'
        identificador = (chave, produto_id, unidade)
        if identificador not in grupos:
            grupos[identificador] = _grupo(chave=chave, produto_id=produto_id,
                nome=nome or 'Produto não vinculado', unidade_linha=unidade)
        return grupos[identificador]
    # A lean values query, as in the existing product indicator service, avoids
    # DECLARE/FETCH/CLOSE round trips to Neon for every streamed cursor. Only
    # the chosen period and required snapshot operands are loaded.
    for item in itens:
        itens_por_produto[item['produto_id']].append(item)
        u = item['unidade_comercial']
        g = obter(item['produto_id'], item['produto__nome'], item['produto__categoria'], u)
        receita = item['valor_total']
        total_itens_periodo += receita
        g['faturamento'] += receita
        g['quantidades'][u] += item['quantidade']
        g['itens'] += 1
        financeiro = g['financeiro_unidades'][u]
        financeiro['itens'] += 1
        custo, motivo = validacoes[item['pk']]['custo'], validacoes[item['pk']]['motivo']
        if custo is not None:
            g['custo'] += custo
            g['com_custo'] += 1
            g['receita_coberta'] += receita
            g['quantidade_coberta'] += item['quantidade']
            financeiro['custo'] += custo
            financeiro['quantidade_coberta'] += item['quantidade']
            financeiro['com_custo'] += 1
        else:
            g['motivos'][motivo] += 1
        if comparavel and item['venda__data_venda'] <= desempenho['fim']:
            g['atual_comparado'] += receita
            g['quantidade_atual_comparada'] += item['quantidade']
    if comparavel:
        anterior = itens_comerciais().filter(venda__data_venda__range=(
            desempenho['referencia_inicio'], desempenho['referencia_fim']))
        for item in anterior.values('produto_id', 'produto__nome', 'produto__categoria', 'unidade_comercial').annotate(
                receita=Sum('valor_total'), quantidade=Sum('quantidade')).order_by():
            u = item['unidade_comercial']
            g = obter(item['produto_id'], item['produto__nome'], item['produto__categoria'], u)
            g['quantidades'].setdefault(u, ZERO)
            g['anterior'] += item['receita']
            g['quantidade_anterior'] += item['quantidade']
    classes = {}
    consolidados = {}
    total = _grupo(nome='Total geral do filtro')
    linhas_sem_vendas = []
    quedas_sem_vendas = 0
    # One selected class is an exact filter; a final class opts into a range.
    minimo, maximo = (nome_ordenacao(rotulo_classe(c)) if c else None
        for c in (classe_de, classe_ate or classe_de))
    for g in grupos.values():
        nome = rotulo_classe(g['chave'])
        if minimo is not None and nome_ordenacao(nome) < minimo or maximo is not None and nome_ordenacao(nome) > maximo:
            continue
        if pesquisa and nome_ordenacao(pesquisa) not in nome_ordenacao(g['nome']):
            continue
        # Keep all existing filtered totals, but do not present groups supplied
        # only by the comparative period as current-period profitability rows.
        _somar(total, g)
        if g['faturamento'] == ZERO and all(q == ZERO for q in g['quantidades'].values()):
            linhas_sem_vendas.append(g['produto_id'])
            if comparavel and g['atual_comparado'] < g['anterior']:
                quedas_sem_vendas += 1
            continue
        classe = classes.setdefault(g['chave'], _grupo(chave=g['chave'], nome=nome, produtos=[]))
        _somar(classe, g)
        # Identity comes from the actual product FK. Missing product links are
        # kept by presentation, never merged through a display name.
        identidade=(g['chave'],g['produto_id'],g['unidade_linha'] if g['produto_id'] is None else '')
        if identidade not in consolidados:
            produto=_grupo(chave=g['chave'],produto_id=g['produto_id'],nome=g['nome'],apresentacoes=[])
            consolidados[identidade]=produto
            classe['produtos'].append(produto)
        produto=consolidados[identidade]
        _somar(produto,g)
        produto['apresentacoes'].append(_finalizar(g,comparavel))
    def ordenar(linhas):
        linhas = sorted(linhas, key=lambda l: (nome_ordenacao(l['nome']), str(l.get('produto_id', l.get('chave', ''))), l.get('unidade', '')))
        if ordem == 'nome': return linhas
        if ordem == 'nome_desc':
            return sorted(linhas, key=lambda l: nome_ordenacao(l['nome']), reverse=True)
        campo = {'lucro':'lucro','margem':'margem','quantidade':'quantidade','queda':'diferenca',
            'cmv':'custo','custo_medio':'custo_medio','preco_medio':'preco_medio',
            'lucro_unitario':'lucro_unitario'}.get(ordem,'faturamento')
        presentes = [l for l in linhas if l[campo] is not None]
        ausentes = [l for l in linhas if l[campo] is None]
        if ordem == 'quantidade':
            return sorted(presentes, key=lambda l: (l['unidade'], -l[campo])) + ausentes
        return sorted(presentes, key=lambda l:l[campo], reverse=ordem not in ('queda','faturamento_menor')) + ausentes
    comerciais = _comerciais_por_produto(itens_por_produto, validacoes)
    for c in classes.values():
        _finalizar(c, comparavel)
        for p in c['produtos']:
            _finalizar(p,comparavel)
            _medias_equivalentes(p,itens_por_produto[p['produto_id']],validacoes,comerciais.get(p['produto_id']))
            p['apresentacoes']=ordenar(p['apresentacoes'])
        c['produtos'] = ordenar(c['produtos'])
    linhas = ordenar(list(classes.values()))
    _finalizar(total, comparavel)
    notas = vendas_comerciais().filter(data_venda__range=(inicio, fim)).aggregate(
        faturamento=Sum('total'), sem_total=Count('pk')-Count('total'))
    produtos = [p for c in linhas for p in c['produtos']]
    apresentacoes=[a for p in produtos for a in p['apresentacoes']]
    return dict(classes=linhas, total=total, desempenho=desempenho, comparavel=comparavel,
                faturamento_notas=notas['faturamento'] or ZERO, total_itens_periodo=total_itens_periodo,
                diferenca_notas=(notas['faturamento'] or ZERO)-total_itens_periodo,
                notas_sem_total=notas['sem_total'], quantidade_linhas=len(apresentacoes),quantidade_produtos=len(produtos),
                linhas_sem_vendas_omitidas=len(linhas_sem_vendas),
                produtos_sem_vendas_omitidos=len(set(linhas_sem_vendas)-{p['produto_id'] for p in produtos}),
                prejuizos=sum(p['lucro'] is not None and p['lucro'] < 0 for p in apresentacoes),
                margens_reduzidas=sum(p['margem'] is not None and 0 <= p['margem'] < 10 for p in apresentacoes),
                quedas=quedas_sem_vendas+sum(p['diferenca'] is not None and p['diferenca'] < 0 for p in apresentacoes),
                validacoes_custo={pk:{k:v[k] for k in ('custo','motivo','evidencia')} for pk,v in validacoes.items()})
