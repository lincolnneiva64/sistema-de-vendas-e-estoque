from decimal import Decimal


def entrada(valor, continua=False):
    if valor is None:
        return ""
    if continua:
        return format(valor, ".3f")
    return format(valor, "f").rstrip("0").rstrip(".") if "." in format(valor, "f") else str(valor)


def numero(valor):
    return entrada(valor).replace(".", ",")


def quantidade(valor, cadastro, *, diferenca=False):
    if valor is None:
        return "—"
    if cadastro["continua"]:
        return format(valor, ".3f").replace(".", ",") + " " + cadastro["base"]
    if diferenca and cadastro["fracionado"]:
        secundarias = (valor * Decimal(cadastro["fator"])).quantize(Decimal("0.001"))
        return numero(secundarias) + " " + cadastro["secundaria"]
    return numero(valor) + " " + cadastro["base"]
