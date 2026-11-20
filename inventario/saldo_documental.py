"""Read-only running balance from documents, never a certified stock snapshot."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from fractions import Fraction
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


def decimal_fracao(valor):
    return Decimal(valor.numerator) / Decimal(valor.denominator)


def projecao_operacional(quantidade, conversao, fator):
    """Reproduce commercial rounding; unsupported arithmetic remains a gap."""
    try:
        operando = quantidade.quantize(Decimal('.001'), rounding=ROUND_HALF_EVEN)
        if conversao == 'divisao':
            operando /= fator
        elif conversao == 'multiplicacao':
            operando *= fator
        return operando.quantize(Decimal('.001'), rounding=ROUND_HALF_EVEN)
    except (InvalidOperation, ZeroDivisionError, OverflowError):
        return None


def provar_atribuicao(detalhe):
    """Pure read proof of a frozen operation; never use current product factors.

    Commercial stock quantities are persisted at .001, HALF_EVEN. Require exact
    reproduction of that projection, not a tolerance around the observer delta.
    Keep the rational documentary operand separate from that rounded projection.
    """
    m = detalhe.movimento
    q, fator = numero(detalhe.quantidade_entrada), numero(detalhe.fator)
    antes, depois = numero(m.antes), numero(m.depois)
    base, entrada = unidade(detalhe.unidade_base), unidade(detalhe.unidade_entrada)
    if q is None or q < 0 or not base or not entrada or antes is None or depois is None:
        return None
    if detalhe.acao in ('entrada', 'devolucao', 'cancelamento'):
        sinal = 1
    elif detalhe.acao == 'saida':
        sinal = -1
    else:
        return None  # An edit/absolute adjustment operand is not presumed a delta.
    if detalhe.conversao == 'nenhuma' and fator is None and entrada == base:
        exato = Fraction(q)
    elif detalhe.conversao in ('divisao', 'multiplicacao') and fator is not None and fator > 0:
        exato = Fraction(q) / Fraction(fator) if detalhe.conversao == 'divisao' else Fraction(q) * Fraction(fator)
    else:
        return None
    operacional = projecao_operacional(q, detalhe.conversao, fator)
    if operacional is None:
        return None
    delta = depois - antes
    if delta != sinal * operacional:
        return None
    return dict(exato=sinal * exato, operacional=delta, unidade=base,
        antes=antes, depois=depois, observador=m.pk, sequencia=m.sequencia,
        operacao=str(detalhe.operacao_id), ordem=detalhe.ordem,
        quantidade=q, unidade_entrada=entrada, fator=fator,
        regra='decimal_3_half_even' if detalhe.conversao != 'nenhuma' else 'sem_conversao')


def provar_entrada_legada(linha, m):
    """Receipt writer adds the item's own quantity, with no unit conversion.

    Caller proves unique document/item/observer identity and absence of purchase
    edits in bulk. This does not generalize to sales or unidentified movements.
    A NULL before-balance is never normalized to zero.
    """
    q, antes, depois = numero(linha.get('quantidade')), numero(m.antes), numero(m.depois)
    base = unidade(linha.get('unidade'))
    if (q is None or q <= 0 or antes is None or depois is None or not base
            or m.origem != 'compra' or m.referencia != f'Compra #{linha["documento"]}'
            or depois-antes != q):
        return None
    return dict(exato=Fraction(q), operacional=q, unidade=base, antes=antes,
        depois=depois, observador=m.pk, sequencia=m.sequencia,
        quantidade=q, unidade_entrada=base, fator=None, regra='entrada_documental_sem_conversao')


def sem_efeito(linha):
    # A removed item explicitly without a return records no inventory write at
    # removal time. An unknown observer (even with equal balances) is not proof.
    if linha.get('informacao_sem_efeito'):
        return True
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
    if linha.get('referencia_fisica_invalida'):
        return None
    if linha.get('saldo_certificado'):
        return linha['saldo_certificado']
    historica = linha.get('referencia_historica')
    if historica and numero(historica.get('valor')) is not None and unidade(historica.get('unidade')):
        return historica
    if fonte == 'compra' and linha.get('saldo_tipo') == 'Reconstruído e corroborado':
        saldo = referencia(linha, linha.get('saldo'), linha.get('saldo_unidade'))
        return dict(saldo, antes=linha.get('saldo_antes')) if saldo else None
    if fonte == 'compra':
        original = linha.get('registro_original', {})
        saldo = referencia(linha, original.get('depois'), original.get('unidade'))
        if saldo:
            return dict(saldo, antes=original.get('antes'),
                        origem=f'Saldo registrado na compra #{linha.get("documento") or linha["pk"]}')
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
    if linha.get('referencia_fisica_invalida') or linha.get('saldo_certificado') or linha.get('fluxo_invalido'):
        return None
    if linha.get('operacao_historica'):
        prova = linha['operacao_historica']
        return decimal_fracao(prova['exato']), prova['unidade']
    if fonte in ('manual', 'regularizacao'):
        ajuste = linha.get('ajuste_manual', {})
        valor = numero(linha.get('diferenca_aplicada', ajuste.get('diferenca')))
        base = unidade(linha.get('saldo_unidade') or linha.get('referencia_unidade') or ajuste.get('unidade'))
        if fonte == 'regularizacao' and valor is None:
            antes, depois = numero(linha.get('antes')), numero(linha.get('depois'))
            if antes is not None and depois is not None:
                valor = depois - antes
        return (valor, base) if valor is not None and base else None
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


def avaliar_evidencia(linha):
    """One read evaluator used by opening, both ledgers, and pagination."""
    fixo = ancora(linha)
    prova = linha.get('operacao_historica')
    fluxo = movimento(linha)
    exato = ((linha['fluxo_documental_exato'], fluxo[1])
        if fluxo and 'fluxo_documental_exato' in linha else
        (Fraction(fluxo[0]), fluxo[1]) if fluxo else None)
    operacional = None
    if prova:
        operacional = dict(valor=prova['depois'], antes=prova['antes'],
            unidade=prova['unidade'], origem=f'Registro #{prova["observador"]}',
            delta=prova['operacional'])
    elif (linha['fonte'] == 'venda' and not linha.get('documento_atual')
            and linha.get('referencia_habilitada') and linha.get('unidades_evidencia', {}).get('baixa')):
        antes, depois = numero(linha.get('referencia_antes')), numero(linha.get('referencia_depois'))
        baixa = numero(linha.get('referencia_baixa'))
        base = unidade(linha.get('referencia_unidade'))
        if base and antes is not None and depois is not None and baixa is not None and antes - baixa == depois:
            operacional = dict(valor=depois, antes=antes, unidade=base,
                origem=f'Snapshot da venda #{linha["documento"]}', delta=-baixa)
    elif linha.get('unidade_comprovada') and linha['fonte'] == 'digital':
        antes, depois = numero(linha.get('saldo_antes')), numero(linha.get('saldo'))
        base = unidade(linha.get('saldo_unidade'))
        if antes is not None and depois is not None and base:
            operacional = dict(valor=depois, antes=antes, unidade=base,
                origem=f'Registro #{linha["pk"]}', delta=depois-antes)
            exato = (Fraction(depois-antes), base)
            fixo = dict(operacional)
    if fixo and 'antes' not in fixo:
        original = linha.get('registro_original', {})
        antes = numero(original.get('antes', linha.get('referencia_antes')))
        if antes is not None:
            fixo = dict(fixo, antes=antes)
    if prova and not linha.get('saldo_certificado'):
        # The recorded before-balance is evidence, but the rounded after-balance
        # is not the exact documentary result of a fractional conversion.
        racional = Fraction(prova['antes']) + prova['exato']
        fixo = dict(fixo or {}, antes=prova['antes'], valor=decimal_fracao(racional),
            _exato=racional, unidade=prova['unidade'],
            origem=(fixo or {}).get('origem', f'Registro #{prova["observador"]}'))
    return dict(sem_efeito=sem_efeito(linha), certificado=linha.get('saldo_certificado'),
        ancora=fixo, documental=exato, operacional=operacional,
        motivo=linha.get('limite_evidencia', 'Origem, quantidade ou unidade histórica não comprovada.'))


def estados_anteriores(registros_invertidos):
    """Recover two independent ledgers in one streamed pass through the prefix."""
    trilhas = [dict(soma=Fraction(0), base='', candidato=None, n=0, fim=False,
                   anterior_esperado=None) for _ in range(2)]
    for linha in registros_invertidos:
        e = avaliar_evidencia(linha)
        if e['sem_efeito']:
            continue
        for indice, t in enumerate(trilhas):
            if t['fim']:
                continue
            saldo = e['certificado'] or (e['operacional'] if indice else e['ancora'])
            fluxo = ((Fraction(e['operacional']['delta']), e['operacional']['unidade'])
                if indice and e['operacional'] else e['documental'] if not indice else None)
            if saldo:
                if indice and t['anterior_esperado'] is not None and saldo['valor'] != t['anterior_esperado']:
                    t['fim'] = True
                    continue
                if t['base'] and t['base'] != saldo['unidade']:
                    t['fim'] = True
                    continue
                fracao = saldo.get('_exato', Fraction(saldo['valor'])) + t['soma']
                t['candidato'] = dict(saldo, valor=decimal_fracao(fracao), _exato=fracao, calculado=t['n'] > 0)
                if indice:
                    t['anterior_esperado'] = saldo.get('antes')
                if e['certificado']:
                    t['fim'] = True
                    continue
            if fluxo is None or (t['base'] and t['base'] != fluxo[1]):
                t['fim'] = True
            else:
                t['base'], t['soma'], t['n'] = fluxo[1], t['soma'] + fluxo[0], t['n'] + 1
        if all(t['fim'] for t in trilhas):
            break
    return tuple(t['candidato'] for t in trilhas)


def estado_anterior(registros_invertidos):
    return estados_anteriores(registros_invertidos)[0]


def aplicar_saldos_documentais(pagina, cronologia, preparar_linhas=None, anteriores_intervalo=None,
                                anteriores_pagina=None, registros_pagina=None):
    """Attach a display-only balance; never modify saldo/entrada/saida fields."""
    deslocamento = 0 if registros_pagina else pagina.start_index() - 1
    estado = operacional = None
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
        estado, operacional = estados_anteriores(trechos())
    if estado is None and not deslocamento and getattr(pagina, 'saldo_inicial', None):
        abertura = pagina.saldo_inicial
        valor, base = numero(abertura['valor']), unidade(abertura['unidade'])
        if valor is not None and base:
            estado = dict(valor=valor, unidade=base, origem=abertura.get('origem', 'saldo inicial disponível no extrato'),
                          _exato=Fraction(valor), calculado=False)
            operacional = dict(estado)
    if estado and pagina.number == 1:
        pagina.saldo_inicial = dict(estado,
            tipo='Saldo documental calculado' if estado.get('calculado') else
                 'Saldo certificado por referência física' if estado.get('proposta') else 'Saldo registrado',
            aviso='Origem anterior ao intervalo, seguida apenas por movimentos comprovados.',
            documento=estado['origem'], url='')
    for linha in (registros_pagina() if registros_pagina else pagina):
        linha['saldo_calculado'] = None
        linha['saldo_registrado_operacional'] = None
        linha['saldo_divergente'] = False
        linha['saldo_operacional_calculado'] = None
        fonte = linha['fonte']
        e = avaliar_evidencia(linha)
        linha['avaliacao_historica'] = e
        # Information-only notes retain their recorded snapshots for inspection,
        # while contributing no quantity or anchor to either running ledger.
        if e['sem_efeito']:
            linha['saldo_registrado_operacional'] = referencia(linha,
                linha.get('referencia_depois'), linha.get('referencia_unidade'))
        if e['sem_efeito']:
            continue
        fixo = e['ancora']
        linha['saldo_registrado_operacional'] = linha.get('saldo_registrado_documento') or fixo
        if e['certificado']:
            # A certified application replaces continuity; its correction is
            # already included in this balance and must never be added again.
            estado = dict(fixo, _exato=Fraction(fixo['valor']), calculado=False)
            operacional = dict(estado)
            continue
        op = e['operacional']
        if op:
            if operacional is None or operacional['unidade'] != op['unidade'] or operacional['valor'] != op['antes']:
                operacional = dict(op, valor=op['antes'])
            operacional = dict(operacional, valor=op['valor'], calculado=True)
            linha['saldo_operacional_calculado'] = dict(operacional, anterior=op['antes'])
            linha['saldo_registrado_operacional'] = op
        else:
            operacional = None
        if fonte == 'compra' and not linha.get('saldo_registrado_documento'):
            original = linha.get('registro_original', {})
            linha['saldo_registrado_operacional'] = referencia(
                linha, original.get('depois'), original.get('unidade'))
        if fonte == 'venda' and not fixo:
            # A documentary conflict disables an anchor, not the visibility of
            # the original recorded snapshot. It never seeds the calculation.
            linha['saldo_registrado_operacional'] = referencia(
                linha, linha.get('referencia_depois'), linha.get('referencia_unidade'))
        fluxo = e['documental']
        if estado is None and fixo and fluxo and fixo['unidade'] == fluxo[1]:
            antes = numero(fixo.get('antes', linha.get('referencia_antes')))
            # Rounded operational snapshots do not certify a rational before-
            # balance by subtraction. Only an exact documentary equality seeds it.
            if antes is not None and Fraction(antes) + fluxo[0] == fixo.get('_exato', Fraction(fixo['valor'])):
                estado = dict(fixo, valor=antes, _exato=Fraction(antes), calculado=False)
        if estado and fluxo and estado['unidade'] == fluxo[1]:
            anterior = estado['valor']
            exato = estado.get('_exato', Fraction(anterior)) + fluxo[0]
            estado = dict(estado, valor=decimal_fracao(exato), _exato=exato, calculado=True)
            linha['saldo_calculado'] = dict(estado, anterior=anterior)
            registrado = linha['saldo_registrado_operacional']
            if registrado and registrado['unidade'] == estado['unidade']:
                linha['saldo_divergente'] = registrado['valor'] != estado['valor']
        else:
            # A gap or an incompatible unit cannot inherit a previous balance.
            # A separately recorded reference may start a new segment afterwards.
            estado = dict(fixo, _exato=fixo.get('_exato', Fraction(fixo['valor'])), calculado=False) if fixo else None
