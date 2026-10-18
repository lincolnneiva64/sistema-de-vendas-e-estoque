"""Read-only management projection. Never used by blind counting endpoints."""
from decimal import Decimal

from .apresentacao import quantidade


def resumo_painel(linhas):
    resumo = dict(total=len(linhas), conferidos=0, faltas=0, sobras=0, corretos=0,
                  pendentes=0, com_custo=0, sem_custo=0, revisao=0,
                  impacto=Decimal(0), saldo=Decimal(0), base_financeira=Decimal(0))
    for linha in linhas:
        item, rodada = linha["item"], linha["rodada"]
        unidade_atual = (item.produto.unidade_venda_1 or item.produto.unidade_compra or "").strip().upper()
        diferenca = linha["diferenca"]
        confirmado = (rodada.ok_em is not None and rodada.encerrada_em is None
                      and diferenca is not None)
        resultado = "pendente" if not confirmado else "falta" if diferenca < 0 else "sobra" if diferenca > 0 else "correto"
        linha.update(resultado=resultado, resultado_label={"pendente": "Aguardando OK", "falta": "Falta",
                     "sobra": "Sobra", "correto": "Correto"}[resultado], percentual_quantidade=None,
                     base_percentual=None, percentual_motivo="Aguardando OK", estimativa=None,
                     estoque_atual_texto=quantidade(item.produto.quantidade,
                         {"base": unidade_atual or "(unidade indisponível)", "continua": unidade_atual in {"KG", "MIL"}}),
                     revisar=linha["situacao"] == "Revisão necessária")
        if linha["revisar"]:
            resumo["revisao"] += 1
        if not confirmado:
            resumo["pendentes"] += 1
            continue
        resumo["conferidos"] += 1
        resumo[{"falta": "faltas", "sobra": "sobras", "correto": "corretos"}[resultado]] += 1
        linha["diferenca_texto"] = quantidade(diferenca, item.cadastro, diferenca=True)
        base = abs(rodada.esperado)
        linha["base_percentual"] = quantidade(base, item.cadastro, diferenca=True)
        if base:
            linha["percentual_quantidade"] = diferenca / base * 100
            linha["percentual_motivo"] = ""
        else:
            linha["percentual_motivo"] = "Esperado no OK igual a zero"
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
