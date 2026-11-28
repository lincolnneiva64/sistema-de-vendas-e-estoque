"""Investigações determinísticas sobre itens comerciais, sempre somente leitura.

Categorias são literais, salvo o mapeamento confirmado abaixo. Nenhum custo, conversão ou
causa comercial é presumido. Os totais das notas nunca são somados via itens.
"""
from decimal import Decimal
from unicodedata import normalize, combining

from django.conf import settings
from django.core.paginator import Paginator
from django.db.models import Case, CharField, Count, F, Q, Sum, Value, When
from django.db.models.functions import Concat

from estoque.analise_evolucao import variacao
from estoque.models import Produto
from .relatorios_produtos import itens_comerciais

ZERO = Decimal('0.00')
CENTAVO = Decimal('0.01')

# Autorização explícita: texto legado → classe oficial Variedades (#34, ativa).
# Somente na análise; não altera Produto.categoria nem presume outros aliases.
CLASSES_LEGADAS_CONFIRMADAS = {'VARIEDADES': 'Variedades'}


def chave_classe(categoria):
    categoria = CLASSES_LEGADAS_CONFIRMADAS.get(categoria, categoria)
    return 'c:' + categoria if categoria else 'sem-classe'


def normalizar_chave_classe(chave):
    # Compatibilidade somente com URLs da correspondência confirmada.
    return chave_classe(chave[2:]) if chave and chave.startswith('c:') else chave


def rotulo_classe(chave):
    return {'sem-classe': 'Sem classe informada',
            'sem-produto': 'Sem produto vinculado'}.get(chave, chave[2:])


def base_comparativa(desempenho):
    atual = Q(venda__data_venda__range=(desempenho['inicio'], desempenho['fim']))
    anterior = Q(venda__data_venda__range=(desempenho['referencia_inicio'], desempenho['referencia_fim']))
    base = itens_comerciais().filter(atual | anterior).annotate(
        classe_chave=Case(
            When(produto__isnull=True, then=Value('sem-produto')),
            *(When(produto__categoria=origem, then=Value(chave_classe(destino)))
              for origem, destino in CLASSES_LEGADAS_CONFIRMADAS.items()),
            When(Q(produto__categoria__isnull=True) | Q(produto__categoria=''), then=Value('sem-classe')),
            default=Concat(Value('c:'), F('produto__categoria')), output_field=CharField()))
    return base, anterior, atual


def medidas(anterior, atual, *, contagens=False):
    resultado = dict(anterior=Sum('valor_total', filter=anterior, default=ZERO),
                     atual=Sum('valor_total', filter=atual, default=ZERO),
                     sem_valor=Count('pk') - Count('valor_total'))
    if contagens:
        for periodo, filtro in (('anterior', anterior), ('atual', atual)):
            resultado['notas_' + periodo] = Count('venda_id', filter=filtro, distinct=True)
            resultado['clientes_' + periodo] = Count('venda__cliente_id', filter=filtro, distinct=True)
    return resultado


def completar(linha):
    linha['diferenca'] = linha['atual'] - linha['anterior']
    linha['percentual'] = variacao(linha['atual'], linha['anterior'])
    return linha


def ordenar(linhas, direcao='impacto'):
    if direcao == 'quedas':
        return sorted(linhas, key=lambda l: (l['diferenca'], str(l.get('id', l.get('chave', '')))))
    if direcao == 'altas':
        return sorted(linhas, key=lambda l: (-l['diferenca'], str(l.get('id', l.get('chave', '')))))
    return sorted(linhas, key=lambda l: (-abs(l['diferenca']), l['diferenca'], str(l.get('id', l.get('chave', '')))))


def destaques(linhas):
    return dict(queda=min((l for l in linhas if l['diferenca'] < ZERO), key=lambda l: l['diferenca'], default=None),
                alta=max((l for l in linhas if l['diferenca'] > ZERO), key=lambda l: l['diferenca'], default=None))


def investigar_classes(desempenho, direcao='impacto'):
    if not desempenho.get('comparavel'):
        return dict(disponivel=False, motivo=desempenho.get('motivo') or 'Sem períodos equivalentes para comparar.')
    base, anterior, atual = base_comparativa(desempenho)
    linhas = list(base.values('classe_chave').annotate(**medidas(anterior, atual)).order_by())
    totais = {p: sum((l[p] for l in linhas), ZERO) for p in ('anterior', 'atual')}
    diferenca = totais['atual'] - totais['anterior']
    if (any(l['sem_valor'] for l in linhas) or totais['anterior'] != desempenho['anterior']
            or totais['atual'] != desempenho['atual'] or diferenca != desempenho['diferenca']):
        return dict(disponivel=False, divergencia=True, totais=totais,
                    motivo='Os valores dos itens não conferem com as notas comerciais ao centavo. A investigação foi interrompida; confira os documentos antes de atribuir a diferença às classes ou aos produtos.')
    por_chave = {}
    for linha in linhas:
        linha['chave'] = linha.pop('classe_chave')
        por_chave[linha['chave']] = linha
    # Inclui classes sem vendas, aplicando apenas a correspondência confirmada.
    for categoria in Produto.objects.order_by().values_list('categoria', flat=True).distinct():
        chave = chave_classe(categoria)
        por_chave.setdefault(chave, dict(chave=chave, anterior=ZERO, atual=ZERO))
    for linha in por_chave.values():
        completar(linha)
        linha['nome'] = rotulo_classe(linha['chave'])
        linha['pontos'] = (linha['diferenca'] / desempenho['anterior'] * 100).quantize(CENTAVO) if desempenho['anterior'] > ZERO else None
    linhas = ordenar(por_chave.values(), direcao)
    return dict(disponivel=True, linhas=linhas, por_chave=por_chave,
                anterior=totais['anterior'], atual=totais['atual'], diferenca=diferenca,
                percentual=variacao(totais['atual'], totais['anterior']), **destaques(linhas))


def agregar_unidades(base, campos, anterior, atual):
    valores = {}
    for periodo, filtro in (('anterior', anterior), ('atual', atual)):
        valores['quantidade_' + periodo] = Sum('quantidade', filter=filtro, default=ZERO)
        valores['valor_' + periodo] = Sum('valor_total', filter=filtro, default=ZERO)
    linhas = base.values(*campos, 'unidade_comercial').annotate(**valores).order_by('unidade_comercial')
    resultado = {}
    for linha in linhas:
        for periodo in ('anterior', 'atual'):
            quantidade = linha['quantidade_' + periodo]
            linha['preco_' + periodo] = (linha['valor_' + periodo] / quantidade).quantize(CENTAVO) if linha['unidade_comercial'] and quantidade > ZERO else None
            if not linha['unidade_comercial']:
                linha['quantidade_' + periodo] = None
        resultado.setdefault(tuple(linha[c] for c in campos), []).append(linha)
    return resultado


COLUNAS_ORDENACAO = frozenset({'nome', 'anterior', 'atual', 'diferenca', 'percentual'})


def ordenar_coluna(linhas, coluna, sentido='desc'):
    """Ordena registros completos; ausência de base fica sempre no final."""
    if coluna not in COLUNAS_ORDENACAO or sentido not in ('asc', 'desc'):
        raise ValueError('Ordenação inválida.')
    def chave(linha):
        valor = linha[coluna]
        if coluna == 'nome':
            return ''.join(c for c in normalize('NFKD', valor.casefold()) if not combining(c))
        return valor
    # Empates mantêm identidade determinística em todas as páginas e direções.
    base = sorted(linhas, key=lambda l: str(l.get('id', l.get('chave', ''))))
    presentes = [l for l in base if l[coluna] is not None]
    ausentes = [l for l in base if l[coluna] is None]
    return sorted(presentes, key=chave, reverse=sentido == 'desc') + ausentes


def investigar_produtos(desempenho, classe, *, pagina=1, direcao='impacto', coluna=None, sentido='desc'):
    if not desempenho.get('comparavel'):
        return dict(disponivel=False, motivo=desempenho.get('motivo') or 'Sem períodos equivalentes para comparar.')
    base, anterior, atual = base_comparativa(desempenho)
    base = base.filter(classe_chave=classe['chave'])
    linhas = list(base.values('produto_id', 'produto__nome').annotate(**medidas(anterior, atual, contagens=True)).order_by())
    if (any(l['sem_valor'] for l in linhas) or
            any(sum((l[p] for l in linhas), ZERO) != classe[p] for p in ('anterior', 'atual'))):
        return dict(disponivel=False, motivo='Os produtos não conferem com os valores da classe. Consulte novamente antes de interpretar a mudança.')
    for linha in linhas:
        linha.update(id=linha.pop('produto_id'), nome=linha.pop('produto__nome') or 'Produto não vinculado')
        completar(linha)
    destaque = destaques(linhas)
    pagina = Paginator(ordenar_coluna(linhas, coluna, sentido) if coluna else ordenar(linhas, direcao), 25).get_page(pagina)
    ids = {l['id'] for l in pagina}
    ids.update(l['id'] for l in destaque.values() if l is not None)
    filtro_ids = Q(produto_id__in=ids - {None})
    if None in ids:
        filtro_ids |= Q(produto_id__isnull=True)
    unidades = agregar_unidades(base.filter(filtro_ids), ['produto_id'], anterior, atual) if ids else {}
    for linha in linhas:
        if linha['id'] in ids:
            linha['unidades'] = unidades.get((linha['id'],), [])
    return dict(disponivel=True, linhas=pagina, classe=classe, **destaque)


def investigar_clientes_produto(desempenho, produto_id, *, pagina=1, direcao='impacto', unidade=''):
    if not desempenho.get('comparavel'):
        return dict(disponivel=False, motivo=desempenho.get('motivo') or 'Sem períodos equivalentes para comparar.')
    base, anterior, atual = base_comparativa(desempenho)
    base = base.filter(produto_id=produto_id)
    if unidade:
        base = base.filter(unidade_comercial=unidade)
    resumo = completar(base.aggregate(**medidas(anterior, atual, contagens=True)))
    if resumo['sem_valor']:
        return dict(disponivel=False, motivo='Há itens sem valor. Confira as notas antes de interpretar a mudança.')
    linhas = list(base.values('venda__cliente_id', 'venda__cliente__nome').annotate(**medidas(anterior, atual, contagens=True)).order_by())
    if any(sum((l[p] for l in linhas), ZERO) != resumo[p] for p in ('anterior', 'atual')):
        return dict(disponivel=False, motivo='As compras por cadastro não conferem com os valores do produto. Consulte novamente.')
    from .inteligencia_clientes import coletivos_confirmados
    confirmados = coletivos_confirmados()
    ausentes = 0
    for linha in linhas:
        linha.update(id=linha.pop('venda__cliente_id'), nome=linha.pop('venda__cliente__nome') or 'Sem cliente vinculado')
        completar(linha)
        if not linha['notas_atual']:
            linha['comportamento'] = 'Sem compras deste produto no período atual'
            ausentes += int(linha['id'] is not None and linha['id'] not in confirmados)
        elif not linha['notas_anterior']:
            linha['comportamento'] = 'Comprou este produto somente no período atual'
        else:
            linha['comportamento'] = ('Reduziu o valor comprado deste produto' if linha['diferenca'] < ZERO else
                                      'Aumentou o valor comprado deste produto' if linha['diferenca'] > ZERO else
                                      'Manteve o valor comprado deste produto')
        linha['coletivo'] = linha['id'] in confirmados
    pagina = Paginator(ordenar(linhas, direcao), 25).get_page(pagina)
    ids = [l['id'] for l in pagina if l['id'] is not None]
    filtro = Q(venda__cliente_id__in=ids)
    if any(l['id'] is None for l in pagina):
        filtro |= Q(venda__cliente_id__isnull=True)
    unidades = agregar_unidades(base.filter(filtro), ['venda__cliente_id'], anterior, atual) if pagina.object_list else {}
    for linha in pagina:
        linha['unidades'] = unidades.get((linha['id'],), [])
    resumo['unidades'] = agregar_unidades(base, [], anterior, atual).get((), [])
    return dict(disponivel=True, resumo=resumo, linhas=pagina, por_id={l['id']: l for l in linhas},
                ausentes=ausentes, unidade=unidade, **destaques(linhas))


def outros_produtos_somente_atual(desempenho, produto_id, cliente_id, *, pagina=1):
    base, anterior, atual = base_comparativa(desempenho)
    linhas = (base.filter(venda__cliente_id=cliente_id).exclude(produto_id=produto_id)
        .values('produto_id', 'produto__nome').annotate(**medidas(anterior, atual, contagens=True))
        .filter(notas_anterior=0, notas_atual__gt=0).order_by('-atual', 'produto_id'))
    return Paginator(linhas, 25).get_page(pagina)
