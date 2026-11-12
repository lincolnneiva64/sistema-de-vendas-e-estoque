"""Read-only table comparison. Historical OKs never become current differences."""
from decimal import Decimal

from .apresentacao import equivalencia_visual, quantidade


def resumo_painel(linhas):
    resumo = dict(total=len(linhas), conferidos=0, comparaveis=0, faltas=0, sobras=0, corretos=0,
                  pendentes=0, com_custo=0, sem_custo=0, revisao=0,
                  impacto=Decimal(0), saldo=Decimal(0), base_financeira=Decimal(0))
    for linha in linhas:
        item, rodada = linha['item'], linha['rodada']
        unidade = (item.produto.unidade_venda_1 or item.produto.unidade_compra or '').strip().upper()
        cadastro = dict(base=unidade or '(unidade indisponível)', continua=unidade in {'KG', 'MIL'})
        comparacao = linha.setdefault('comparacao_atual', dict(
            contado=None, projetada=False, limite='Comparação atual não comprovada.'))
        comparacao.update(estoque=item.produto.quantidade, unidade=unidade,
            diferenca=None, percentual=None, estimativa=None, base_percentual=None,
            percentual_motivo='', contado_texto='Não comparável', diferenca_texto='Não comparável')
        comparacao['estoque_texto'] = quantidade(comparacao['estoque'], cadastro)
        # A current conversion cannot bridge a historical unit change.
        comparavel = (rodada.ok_em is not None and rodada.encerrada_em is None
            and comparacao['contado'] is not None and comparacao['estoque'] is not None
            and unidade == item.cadastro.get('base') and not comparacao['limite'])
        if not comparavel:
            comparacao['limite'] = comparacao['limite'] or 'Unidade, saldo ou referência temporal incompatível.'
        resultado = 'pendente'
        if comparavel:
            diferenca = comparacao['contado'] - comparacao['estoque']
            comparacao.update(diferenca=diferenca,
                contado_texto=quantidade(comparacao['contado'], cadastro),
                diferenca_texto=quantidade(diferenca, cadastro))
            resultado = 'falta' if diferenca < 0 else 'sobra' if diferenca > 0 else 'correto'
            base = abs(comparacao['estoque'])
            comparacao['base_percentual'] = quantidade(base, cadastro)
            if base:
                comparacao['percentual'] = diferenca / base * 100
            else:
                comparacao['percentual_motivo'] = 'Estoque exibido igual a zero; percentual não definido.'
        else:
            comparacao['percentual_motivo'] = comparacao['limite']
        comparacao['resultado'] = resultado
        comparacao['situacao'] = ('Aguardando OK' if not rodada.ok_em else 'Não comparável') if not comparavel else {
            'falta': 'Falta', 'sobra': 'Sobra', 'correto': 'Sem diferença'}[resultado]
        comparacao['destaque'] = resultado if resultado in ('falta', 'sobra') else 'neutro'
        # Explanatory equivalences never replace the primary comparison unit.
        linha['estoque_atual_texto'] = comparacao['estoque_texto']
        linha['estoque_atual_equivalencia'] = equivalencia_visual(
            comparacao['estoque'], item.cadastro) if comparavel else {}
        linha['contado_equivalencia'] = equivalencia_visual(
            comparacao['contado'], item.cadastro) if comparavel else {}
        linha.update(resultado=resultado, resultado_label=comparacao['situacao'],
            percentual_quantidade=comparacao['percentual'], base_percentual=comparacao['base_percentual'],
            percentual_motivo=comparacao['percentual_motivo'], estimativa=None,
            revisar=linha['situacao'] == 'Revisão necessária')
        resumo['revisao'] += int(linha['revisar'])
        resumo['conferidos'] += int(rodada.ok_em is not None and rodada.encerrada_em is None)
        resumo['comparaveis'] += int(comparavel)
        resumo[{'falta': 'faltas', 'sobra': 'sobras', 'correto': 'corretos', 'pendente': 'pendentes'}[resultado]] += 1
        if not comparavel:
            continue
        if item.custo_base is None or item.custo_base < 0:
            resumo['sem_custo'] += 1
            continue
        comparacao['estimativa'] = (comparacao['diferenca'] * item.custo_base).quantize(Decimal('.01'))
        linha['estimativa'] = comparacao['estimativa']
        resumo['com_custo'] += 1
        resumo['impacto'] += abs(comparacao['estimativa'])
        resumo['saldo'] += comparacao['estimativa']
        resumo['base_financeira'] += (abs(comparacao['estoque']) * item.custo_base).quantize(Decimal('.01'))
    resumo['percentual_financeiro'] = (resumo['impacto'] / resumo['base_financeira'] * 100
                                      if resumo['base_financeira'] > 0 else None)
    if not resumo['com_custo']:
        resumo['impacto'] = resumo['saldo'] = None
    resumo['parcial'] = resumo['sem_custo'] > 0 or resumo['pendentes'] > 0
    return resumo
