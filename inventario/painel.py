"""Read-only management projection. Never used by blind counting endpoints."""
from decimal import Decimal

from .apresentacao import equivalencia_visual, quantidade


def explicar_saldos(linha, lote, historico_valido):
    """Explain the summary using its existing read-only, batched evidence.

    These fields never authorize adjustments, replace the OK or feed stock
    services. A conference's own adjustment is not another physical movement.
    """
    from .conciliacao_documental import conciliar
    item, rodada = linha['item'], linha['rodada']
    cadastro = dict(base=item.cadastro.get('base', ''),
                    continua=item.cadastro.get('continua', False))
    dados = dict(entradas=Decimal(0), saidas=Decimal(0), posteriores=0,
                 sem_comprovacao=0, projecao=None, diferenca_atual=None,
                 limite='', unidade=cadastro['base'])
    if not rodada.ok_em or rodada.sequencia_ok is None:
        dados['limite'] = 'Aguardando contagem física confirmada com OK.'
    elif lote is not None:
        comprovados = {m['registro'].pk for m in linha['movimentos'] if m['comprovado_documentalmente']}
        for registro in linha['movimentos']:
            m = registro['registro']
            if m.sequencia <= rodada.sequencia_ok:
                continue
            dados['posteriores'] += 1
            if m.origem in ('compra', 'venda'):
                try:
                    if m.pk not in comprovados:
                        conciliar(rodada, [m], _lote=lote)
                    variacao = m.depois - m.antes
                    dados['entradas' if variacao > 0 else 'saidas'] += abs(variacao)
                except ValueError as exc:
                    dados['sem_comprovacao'] += 1
                    dados['limite'] = dados['limite'] or str(exc)
            elif m.pk not in comprovados:
                # A later gap must not reclassify an already audited adjustment
                # as an unproved physical entry/exit.
                aplicada = getattr(lote, 'aplicacoes_revisao', {}).get(m.referencia) if m.origem == 'ajuste_manual' else None
                if aplicada:
                    from .revisao_historico import validar_aplicacao
                    try:
                        validar_aplicacao(rodada, m, aplicada, lote)
                        continue
                    except ValueError:
                        pass
                dados['sem_comprovacao'] += 1
                dados['limite'] = dados['limite'] or (
                    f'Movimento #{m.pk} · {m.referencia or m.origem}: '
                    'origem ou unidade histórica sem comprovação. Revise o movimento antes de concluir.')
        if (historico_valido and not dados['sem_comprovacao'] and rodada.contado_base is not None
                and item.produto.quantidade is not None):
            dados['projecao'] = rodada.contado_base + dados['entradas'] - dados['saidas']
            dados['diferenca_atual'] = dados['projecao'] - item.produto.quantidade
        else:
            dados['limite'] = dados['limite'] or linha['bloqueio'] or 'Histórico posterior ao OK não comprovado.'
    for campo in ('entradas', 'saidas', 'projecao', 'diferenca_atual'):
        dados[campo + '_texto'] = quantidade(dados[campo], cadastro)
    dados['diferenca_ok_texto'] = quantidade(linha['diferenca'], cadastro)
    linha['explicacao_saldos'] = dados


def resumo_painel(linhas):
    resumo = dict(total=len(linhas), conferidos=0, faltas=0, sobras=0, corretos=0,
                  pendentes=0, com_custo=0, sem_custo=0, revisao=0,
                  impacto=Decimal(0), saldo=Decimal(0), base_financeira=Decimal(0))
    for linha in linhas:
        item, rodada = linha["item"], linha["rodada"]
        unidade_atual = (item.produto.unidade_venda_1 or item.produto.unidade_compra or "").strip().upper()
        # Read-only equivalences: live stock uses its current registered units;
        # the count uses only the conference's frozen conversion.
        cadastro_atual = dict(base=unidade_atual,
            secundaria=getattr(item.produto, 'unidade_venda_2', '') or '',
            fator=getattr(item.produto, 'fator_conversao', None),
            nome=getattr(item.produto, 'nome', item.cadastro.get('nome', '')))
        parcela = getattr(rodada, 'avulsos', None)
        linha['estoque_atual_equivalencia'] = equivalencia_visual(
            item.produto.quantidade, cadastro_atual, avulsos=parcela)
        linha['contado_equivalencia'] = ({} if linha.get('comparacao_fisica') else
            equivalencia_visual(rodada.contado_base if rodada.ok_em else None,
                                item.cadastro, avulsos=parcela))
        diferenca = linha["diferenca"]
        confirmado = (rodada.ok_em is not None and rodada.encerrada_em is None
                      and diferenca is not None)
        resultado = "pendente" if not confirmado else "falta" if diferenca < 0 else "sobra" if diferenca > 0 else "correto"
        fisico = linha.get('comparacao_fisica', {})
        if confirmado and fisico:
            # Physical classification only. Keep decimal balances, costs and the
            # independent administrative/review states untouched.
            d = fisico['dados'].get('diferenca_fisica')
            if fisico.get('normalizacao'):
                resultado = 'correto'
            elif d is None:
                resultado = 'pendente'
            else:
                resultado = 'correto' if Decimal(d) == 0 else 'falta' if Decimal(d) < 0 else 'sobra'
        linha.update(resultado=resultado, resultado_label={"pendente": "Aguardando OK", "falta": "Falta",
                     "sobra": "Sobra", "correto": "Correto"}[resultado], percentual_quantidade=None,
                     base_percentual=None, percentual_motivo="Aguardando OK", estimativa=None,
                     estoque_atual_texto=quantidade(item.produto.quantidade,
                         {"base": unidade_atual or "(unidade indisponível)", "continua": unidade_atual in {"KG", "MIL"}}),
                     revisar=linha["situacao"] == "Revisão necessária")
        if confirmado and resultado == 'pendente':
            linha['resultado_label'] = 'Resíduo decimal' if fisico.get('normalizacao') else 'Referência aproximada'
        if linha["revisar"]:
            resumo["revisao"] += 1
        if not confirmado:
            resumo["pendentes"] += 1
            continue
        resumo["conferidos"] += 1
        resumo[{"falta": "faltas", "sobra": "sobras", "correto": "corretos", "pendente": "pendentes"}[resultado]] += 1
        linha["diferenca_texto"] = fisico["diferenca"] if fisico else quantidade(diferenca, item.cadastro, diferenca=True)
        if fisico and resultado == 'correto':
            linha['diferenca_texto'] = 'Sem diferença'
        if fisico.get('normalizacao'):
            linha['resultado_label'] = 'Ajuste de precisão'
            linha['diferenca_texto'] = 'Ajuste de precisão'
        base = abs(rodada.esperado)
        linha["base_percentual"] = quantidade(base, item.cadastro, diferenca=True)
        if base:
            linha["percentual_quantidade"] = diferenca / base * 100
            linha["percentual_motivo"] = ""
        else:
            linha["percentual_motivo"] = "Esperado no OK igual a zero"
        if fisico:
            dados = fisico['dados']
            d = dados.get('diferenca_fisica')
            if d is not None and Decimal(dados['esperado_unidades']) != 0:
                linha['percentual_quantidade'] = Decimal(d) / abs(Decimal(dados['esperado_unidades'])) * 100
                linha['base_percentual'] = dados['esperado_unidades'].split('.')[0] + ' UN'
            elif d is None:
                linha['percentual_quantidade'] = None
                linha['percentual_motivo'] = 'Referência física aproximada; consulte os detalhes decimais'
        if item.custo_base is None or item.custo_base < 0:
            resumo["sem_custo"] += 1
            continue
        # Sum the displayed monetary amounts. Costs use the same frozen base
        # unit as the reference and count; never sum quantities across units.
        linha["estimativa"] = (diferenca * item.custo_base).quantize(Decimal("0.01"))
        resumo["com_custo"] += 1
        resumo["impacto"] += abs(linha["estimativa"])
        resumo["saldo"] += linha["estimativa"]
        resumo["base_financeira"] += (base * item.custo_base).quantize(Decimal("0.01"))
    resumo["percentual_financeiro"] = (resumo["impacto"] / resumo["base_financeira"] * 100
                                        if resumo["base_financeira"] > 0 else None)
    if not resumo["com_custo"]:
        resumo["impacto"] = resumo["saldo"] = None
    resumo["parcial"] = resumo["sem_custo"] > 0
    return resumo
