from decimal import Decimal, InvalidOperation, localcontext


ZERO = Decimal("0.001")
MAXIMO = Decimal("1000000000")
CONTINUAS = {"KG", "MIL"}  # Same explicit continuous units as the current sales engine.


def decimal_entrada(valor):
    if valor is None or str(valor).strip() == "":
        return None
    try:
        numero = Decimal(str(valor).strip().replace(",", "."))
        if not numero.is_finite() or numero < 0 or numero >= MAXIMO or numero != numero.quantize(ZERO):
            raise ValueError("Informe quantidade não negativa com até três casas decimais.")
        return numero
    except (InvalidOperation, TypeError):
        raise ValueError("Quantidade inválida.")


def avulsos_fisicos(cadastro):
    """Only an explicit frozen UN conversion; never infer it from a product name."""
    try:
        fator = Decimal(cadastro.get("fator", "0"))
        return (cadastro.get("avulsos_fisicos") is True and not cadastro["fracionado"]
                and cadastro["base"] in {"PCT", "CX", "FD", "DZ"}
                and cadastro["secundaria"] == "UN" and fator.is_finite()
                and fator > 1 and fator == fator.to_integral_value())
    except (InvalidOperation, ValueError, KeyError, TypeError):
        return False


def cadastro_compativel(produto, cadastro):
    atual = snapshot(produto)
    # Old frozen snapshots did not retain UN for package-only products.
    # They keep their original single-field rule; do not reinterpret history.
    if "avulsos_fisicos" not in cadastro:
        atual.pop("avulsos_fisicos", None)
        if not cadastro["fracionado"]:
            atual["secundaria"] = ""
    return atual == cadastro


def snapshot(produto):
    base = (produto.unidade_venda_1 or produto.unidade_compra or "").strip().upper()
    secundaria = (produto.unidade_venda_2 or "").strip().upper()
    fator = Decimal(produto.fator_conversao or 0)
    if not base:
        raise ValueError(f"Revise a unidade principal de {produto.nome}.")
    fracionado = bool(produto.vende_fracionado)
    if fracionado and (not secundaria or secundaria == base or fator <= 0):
        raise ValueError(f"Revise a conversão de {produto.nome}.")
    cadastro = {"nome": produto.nome, "codigo": produto.codigo or "", "categoria": produto.categoria or "",
            "base": base, "secundaria": secundaria if fracionado else "", "fator": str(fator),
            "fracionado": fracionado, "continua": base in CONTINUAS,
            "secundaria_continua": secundaria in CONTINUAS, "regra": 1}
    if (not fracionado and base in {"PCT", "CX", "FD", "DZ"} and secundaria == "UN"
            and fator.is_finite() and fator > 1 and fator == fator.to_integral_value()):
        cadastro["secundaria"] = "UN"
        cadastro["avulsos_fisicos"] = True
    return cadastro


def converter(cadastro, embalagens, avulsos):
    principal, avulso = decimal_entrada(embalagens), decimal_entrada(avulsos)
    if principal is None:
        raise ValueError("Preencha a quantidade principal, inclusive quando for zero.")
    if not cadastro["continua"] and principal != principal.to_integral_value():
        raise ValueError("Informe embalagens ou unidades completas em números inteiros.")
    fisicos = avulsos_fisicos(cadastro)
    if fisicos and avulso is None:
        avulso = Decimal(0)
    if cadastro["fracionado"] or fisicos:
        if avulso is None:
            raise ValueError("Preencha os avulsos, inclusive quando forem zero.")
        if not cadastro["secundaria_continua"] and avulso != avulso.to_integral_value():
            raise ValueError("Informe unidades avulsas inteiras.")
    elif avulso not in (None, Decimal(0)):
        raise ValueError("Produto sem conversão para avulsos.")
    with localcontext() as contexto:
        contexto.prec = 36
        total = principal + ((avulso or 0) / Decimal(cadastro["fator"]) if cadastro["fracionado"] or fisicos else 0)
        if total >= MAXIMO or total.quantize(ZERO) >= MAXIMO:
            raise ValueError("Quantidade acima do limite do estoque.")
        return principal, avulso, total.quantize(Decimal("0.000000001")), total.quantize(ZERO)
