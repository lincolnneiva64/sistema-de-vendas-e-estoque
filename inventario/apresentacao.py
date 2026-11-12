from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext


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


def comparacao_fisica(dados):
    """Format only frozen evidence. Decimal residuals never become physical units."""
    if not dados:
        return {}
    fator = Decimal(dados['fator'])

    def composicao(total, aproximada=False):
        pacotes, unidades = divmod(Decimal(total), fator)
        return ('≈ ' if aproximada else '') + numero(pacotes) + ' ' + dados['base'] + ' + ' + numero(unidades) + ' UN = ' + numero(Decimal(total)) + ' UN'

    esperado = dados.get('esperado_unidades')
    diferenca = dados.get('diferenca_fisica')
    natureza = dados.get('natureza')
    if natureza == 'normalizacao_decimal':
        texto = 'Equivalência aproximada — resíduo decimal; sem falta física comprovada'
    elif diferenca is None:
        texto = 'Diferença física não comprovada — referência aproximada'
    else:
        d = Decimal(diferenca)
        texto = 'Equivalência física' if d == 0 else ('Falta ' if d < 0 else 'Sobra ') + numero(abs(d)) + ' UN'
    return dict(esperado=composicao(esperado, not dados.get('esperado_canonico')) if esperado is not None else '',
        encontrado=composicao(dados['total_unidades']), diferenca=texto,
        dados=dados, normalizacao=natureza == 'normalizacao_decimal')


def equivalencia_visual(valor, cadastro, *, avulsos=None):
    """Display the frozen conversion; product names never define physical units."""
    base = str(cadastro.get('base') or '').strip().upper()
    destino = str(cadastro.get('secundaria') or '').strip().upper()
    if not base or not destino or base == destino or valor is None:
        return {}
    try:
        valor, fator = Decimal(str(valor)), Decimal(str(cadastro.get('fator', '0')))
        if not valor.is_finite() or not fator.is_finite() or fator <= 0:
            return {}
        with localcontext() as contexto:
            contexto.prec = 36
            exato = valor * fator
        exibido = exato
    except (InvalidOperation, ValueError, TypeError):
        return {}
    aproximado = exibido != exato
    return dict(texto=('≈ ' if aproximado else '') + numero(exibido) + ' ' + destino,
                exato=numero(exato) + ' ' + destino, aproximado=aproximado)
