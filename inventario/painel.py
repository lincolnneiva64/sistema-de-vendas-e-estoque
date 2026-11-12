"""Arithmetic of displayed quantities, separate from authorized stock adjustments."""
from decimal import Decimal, InvalidOperation

from .apresentacao import equivalencia_visual, quantidade


def resumo_painel(linhas):
    resumo = dict(total=len(linhas), conferidos=0, comparaveis=0, faltas=0, sobras=0, corretos=0,
                  pendentes=0, anteriores=0, com_custo=0, sem_custo=0, revisao=0,
                  impacto=Decimal(0), saldo=Decimal(0), base_financeira=Decimal(0))
    for linha in linhas:
        item, rodada = linha['item'], linha['rodada']
        unidade = (item.produto.unidade_venda_1 or item.produto.unidade_compra or '').strip().upper()
        cadastro = dict(base=unidade or '(unidade indisponível)', continua=unidade in {'KG', 'MIL'})
        comparacao = linha.setdefault('comparacao_atual', dict(
            contado=None, projetada=False, limite='Comparação atual não comprovada.'))
        projetado = linha.get('comparacao_operacional', {}).get('contado', comparacao['contado'])
        original = getattr(rodada, 'contado_base', projetado) if rodada.ok_em else None
        base_original = str(item.cadastro.get('base') or '').strip().upper()
        operacional = (rodada.ok_em is not None and rodada.encerrada_em is None
            and projetado is not None and item.produto.quantidade is not None
            and unidade == base_original and not comparacao['limite'])
        linha['comparacao_operacional'] = dict(contado=projetado,
            diferenca=projetado - item.produto.quantidade if operacional else None,
            comprovada=operacional, limite=comparacao['limite'])
        comparacao.update(estoque=item.produto.quantidade, unidade=unidade,
            contado=original, contado_original=original, diferenca=None, percentual=None,
            estimativa=None, base_percentual=None, percentual_motivo='',
            contado_texto=quantidade(original, item.cadastro), diferenca_texto='Unidades sem conversão comprovada',
            conversao_texto='', custo_unitario=item.custo_base)
        comparacao['estoque_texto'] = quantidade(comparacao['estoque'], cadastro)
        fator = None
        if original is not None and unidade and unidade != base_original:
            # Only the relationship already frozen in the conference can bridge units.
            try:
                candidato = Decimal(str(item.cadastro.get('fator', '0')))
                if (unidade == item.cadastro.get('secundaria') and candidato.is_finite()
                        and candidato > 0 and (item.cadastro.get('fracionado') or item.cadastro.get('avulsos_fisicos'))):
                    fator = candidato
            except (InvalidOperation, ValueError, TypeError):
                pass
            if fator is not None:
                comparacao['contado'] = original * fator
                # Preserve loose units instead of multiplying a three-decimal
                # packaging projection back into an inexact physical count.
                fisica = (linha.get('comparacao_fisica') or {}).get('dados', {})
                if unidade == 'UN' and fisica.get('total_unidades') is not None:
                    comparacao['contado'] = Decimal(fisica['total_unidades'])
                    if comparacao['contado'] != original * fator:
                        comparacao['contado_texto'] = '≈ ' + comparacao['contado_texto']
                comparacao['conversao_texto'] = quantidade(comparacao['contado'], cadastro)
                if item.custo_base is not None:
                    comparacao['custo_unitario'] = item.custo_base / fator
        comparavel = (rodada.ok_em is not None and original is not None
            and comparacao['estoque'] is not None and bool(unidade)
            and (unidade == base_original or fator is not None))
        comparacao['anterior'] = bool(not operacional or comparacao.get('projetada')
            or linha.get('marco') or fator is not None)
        comparacao['projecao_texto'] = quantidade(projetado, cadastro) if operacional else ''
        resultado = 'pendente'
        if comparavel:
            diferenca = comparacao['contado'] - comparacao['estoque']
            comparacao.update(diferenca=diferenca,
                diferenca_texto=quantidade(diferenca, cadastro).replace('-', '−', 1))
            resultado = ('anterior' if comparacao['anterior'] else
                'falta' if diferenca < 0 else 'sobra' if diferenca > 0 else 'correto')
            base = abs(comparacao['estoque'])
            comparacao['base_percentual'] = quantidade(base, cadastro)
            if base:
                comparacao['percentual'] = diferenca / base * 100
            else:
                comparacao['percentual_motivo'] = 'Estoque exibido igual a zero; percentual não definido.'
        else:
            comparacao['percentual_motivo'] = ('Contagem ainda não registrada.' if original is None else
                'Saldo ou conversão entre as unidades não comprovados.')
        comparacao['resultado'] = resultado
        comparacao['situacao'] = ('Aguardando OK' if not rodada.ok_em else 'Não comparável') if not comparavel else {
            'falta': 'Falta', 'sobra': 'Sobra', 'correto': 'Sem diferença',
            'anterior': 'Comparação com contagem anterior'}[resultado]
        comparacao['destaque'] = resultado if resultado in ('falta', 'sobra') else 'neutro'
        # Explanatory equivalences never replace the primary comparison unit.
        linha['estoque_atual_texto'] = comparacao['estoque_texto']
        linha['estoque_atual_equivalencia'] = equivalencia_visual(
            comparacao['estoque'], item.cadastro) if comparavel and unidade == base_original else {}
        linha['contado_equivalencia'] = equivalencia_visual(
            original, item.cadastro) if original is not None else {}
        linha.update(resultado=resultado, resultado_label=comparacao['situacao'],
            percentual_quantidade=comparacao['percentual'], base_percentual=comparacao['base_percentual'],
            percentual_motivo=comparacao['percentual_motivo'], estimativa=None,
            revisar=linha['situacao'] == 'Revisão necessária')
        resumo['revisao'] += int(linha['revisar'])
        resumo['conferidos'] += int(rodada.ok_em is not None and rodada.encerrada_em is None)
        resumo['comparaveis'] += int(comparavel)
        resumo[{'falta': 'faltas', 'sobra': 'sobras', 'correto': 'corretos', 'pendente': 'pendentes', 'anterior': 'anteriores'}[resultado]] += 1
        if not comparavel:
            continue
        if item.custo_base is None or item.custo_base < 0:
            resumo['sem_custo'] += 1
            continue
        comparacao['estimativa'] = (comparacao['diferenca'] * comparacao['custo_unitario']).quantize(Decimal('.01'))
        linha['estimativa'] = comparacao['estimativa']
        resumo['com_custo'] += 1
        resumo['impacto'] += abs(comparacao['estimativa'])
        resumo['saldo'] += comparacao['estimativa']
        resumo['base_financeira'] += (abs(comparacao['estoque']) * comparacao['custo_unitario']).quantize(Decimal('.01'))
    resumo['percentual_financeiro'] = (resumo['impacto'] / resumo['base_financeira'] * 100
                                      if resumo['base_financeira'] > 0 else None)
    if not resumo['com_custo']:
        resumo['impacto'] = resumo['saldo'] = None
    resumo['parcial'] = resumo['sem_custo'] > 0 or resumo['pendentes'] > 0 or resumo['anteriores'] > 0
    return resumo
