"""Read-only running balance from documents, never a certified stock snapshot."""
from decimal import Decimal, InvalidOperation
from itertools import islice


def numero(valor):
    try:
        valor = Decimal(str(valor))
        return valor if valor.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def unidade(valor):
    valor = str(valor or '').strip().upper()
    return '' if valor in ('', 'NÃO INFORMADA', 'NAO INFORMADA') else valor


def quantidade_historica(detalhe, valor, origem):
    """Convert only through the factor frozen on this operation's attribution."""
    valor = numero(valor)
    base, entrada = unidade(detalhe.unidade_base), unidade(detalhe.unidade_entrada)
    if valor is None or not base or not entrada or unidade(origem) != entrada:
        return None
    fator = numero(detalhe.fator)
    if detalhe.conversao == 'nenhuma':
        return valor if fator is None and entrada == base else None
    if fator is None or fator <= 0:
        return None
    if detalhe.conversao == 'multiplicacao':
        return valor * fator
    if detalhe.conversao == 'divisao':
        return valor / fator
    return None


def sem_efeito(linha):
    # A removed item explicitly without a return records no inventory write at
    # removal time. An unknown observer (even with equal balances) is not proof.
    if (linha['fonte'] == 'venda' and linha.get('cancelada')
            and linha.get('devolvido') is False
            and numero(linha.get('referencia_baixa')) == 0
            and numero(linha.get('referencia_antes')) is not None
            and numero(linha.get('referencia_antes')) == numero(linha.get('referencia_depois'))):
        return True
    return (linha['fonte'] == 'conferencia' or
        linha['fonte'] == 'remocao' and linha.get('origem') == 'removido'
        and linha.get('devolvido') is False and not linha.get('retorno_efetivo'))


def referencia(linha, valor, base):
    valor, base = numero(valor), unidade(base)
    if valor is None or not base:
        return None
    nomes = {'venda': 'Venda', 'compra': 'Compra', 'manual': 'Ajuste manual',
             'regularizacao': 'Regularização'}
    return dict(valor=valor, unidade=base,
        origem=f"{nomes.get(linha['fonte'], 'Registro')} #{linha.get('documento') or linha['pk']}")


def ancora(linha):
    fonte = linha['fonte']
    if fonte == 'manual':
        ajuste = linha.get('ajuste_manual')
        if ajuste:
            return referencia(linha, ajuste['depois'], ajuste['unidade'])
    if fonte == 'regularizacao':
        return referencia(linha, linha.get('saldo', linha.get('referencia_depois')),
                          linha.get('saldo_unidade') or linha.get('referencia_unidade'))
    if fonte == 'manual':
        return referencia(linha, linha.get('referencia_depois'), linha.get('referencia_unidade'))
    if fonte != 'venda' or linha.get('cancelada'):
        return None
    if 'referencia_habilitada' in linha and not linha['referencia_habilitada']:
        return None
    if 'referencia_antes' not in linha:
        if linha.get('saldo_tipo') == 'Snapshot registrado':
            return referencia(linha, linha.get('saldo'), linha.get('saldo_unidade'))
        return None
    antes, baixa, depois = (numero(linha.get(c)) for c in (
        'referencia_antes', 'referencia_baixa', 'referencia_depois'))
    if (any(v is None for v in (antes, baixa, depois)) or baixa <= 0
            or antes - baixa != depois):
        return None
    return referencia(linha, depois, linha.get('referencia_unidade'))


def saida_realizada(linha):
    if not linha.get('cancelada'):
        return True
    antes, baixa, depois = (numero(linha.get(k)) for k in (
        'referencia_antes', 'referencia_baixa', 'referencia_depois'))
    return (all(v is not None for v in (antes, baixa, depois)) and baixa > 0
            and antes - baixa == depois and bool(unidade(linha.get('referencia_unidade'))))


def movimento(linha):
    """None is a gap; a tuple contains the signed documentary amount/unit."""
    fonte = linha['fonte']
    if linha.get('fluxo_historico'):
        return linha['fluxo_historico']
    operacao = linha.get('operacao_documental')
    if operacao:
        if not operacao['efetivada']:
            return None
        valor = numero(operacao['quantidade'])
        base = unidade(operacao['unidade'])
        sinal = 1 if operacao['direcao'] == 'entrada' else -1
    elif fonte in ('compra', 'venda'):
        if fonte == 'venda' and not saida_realizada(linha):
            return None
        valor, base = numero(linha.get('quantidade')), unidade(linha.get('unidade'))
        sinal = 1 if fonte == 'compra' else -1
    elif fonte == 'remocao' and linha.get('retorno_efetivo'):
        valor, base = numero(linha.get('quantidade')), unidade(linha.get('unidade'))
        sinal = 1
    else:
        return None
    if valor is None or valor < 0 or not base:
        return None
    return valor * sinal, base


def estado_anterior(registros_invertidos):
    """Recover the same documentary segment across pages, without resetting it
    at each newer snapshot. A real gap or a manual correction ends the segment.
    """
    acumulado, base = Decimal(0), ''
    candidato = None
    for linha in registros_invertidos:
        fonte = linha['fonte']
        if sem_efeito(linha):
            continue
        saldo = ancora(linha)
        if saldo:
            if base and base != saldo['unidade']:
                return candidato
            candidato = dict(saldo, valor=saldo['valor'] + acumulado)
        if fonte in ('manual', 'regularizacao'):
            return candidato
        fluxo = movimento(linha)
        if fluxo is None:
            return candidato
        valor, atual = fluxo
        if base and base != atual:
            return candidato
        base, acumulado = atual, acumulado + valor
    return candidato


def aplicar_saldos_documentais(pagina, cronologia, preparar_linhas=None, anteriores_intervalo=None,
                                anteriores_pagina=None, registros_pagina=None):
    """Attach a display-only balance; never modify saldo/entrada/saida fields."""
    deslocamento = 0 if registros_pagina else pagina.start_index() - 1
    estado = None
    if anteriores_pagina or deslocamento or anteriores_intervalo:
        def preparadas():
            if anteriores_pagina:
                yield from anteriores_pagina()
                return
            if deslocamento:
                anteriores = cronologia.reverse()[pagina.paginator.count - deslocamento:]
                yield from anteriores.iterator(chunk_size=100)
            if anteriores_intervalo:
                yield from anteriores_intervalo().reverse().iterator(chunk_size=100)
        registros = preparadas()
        def trechos():
            while trecho := list(islice(registros, 100)):
                if preparar_linhas:
                    preparar_linhas(trecho)
                yield from trecho
        estado = estado_anterior(trechos())
    if estado is None and not deslocamento and getattr(pagina, 'saldo_inicial', None):
        abertura = pagina.saldo_inicial
        valor, base = numero(abertura['valor']), unidade(abertura['unidade'])
        if valor is not None and base:
            estado = dict(valor=valor, unidade=base, origem='saldo inicial disponível no extrato')
    for linha in (registros_pagina() if registros_pagina else pagina):
        linha['saldo_calculado'] = None
        linha['saldo_registrado_operacional'] = None
        linha['saldo_divergente'] = False
        fonte = linha['fonte']
        if sem_efeito(linha):
            continue
        fixo = ancora(linha)
        linha['saldo_registrado_operacional'] = linha.get('saldo_registrado_documento') or fixo
        if fonte == 'venda' and not fixo:
            # A documentary conflict disables an anchor, not the visibility of
            # the original recorded snapshot. It never seeds the calculation.
            linha['saldo_registrado_operacional'] = referencia(
                linha, linha.get('referencia_depois'), linha.get('referencia_unidade'))
        if fonte in ('manual', 'regularizacao'):
            # An actual correction starts a new reference, not a sale/receipt.
            estado = fixo
            continue
        fluxo = movimento(linha)
        if estado and fluxo and estado['unidade'] == fluxo[1]:
            anterior = estado['valor']
            estado = dict(estado, valor=anterior + fluxo[0])
            linha['saldo_calculado'] = dict(estado, anterior=anterior)
            registrado = linha['saldo_registrado_operacional']
            if registrado and registrado['unidade'] == estado['unidade']:
                linha['saldo_divergente'] = registrado['valor'] != estado['valor']
        else:
            # A gap or an incompatible unit cannot inherit a previous balance.
            # A separately recorded reference may start a new segment afterwards.
            estado = fixo
