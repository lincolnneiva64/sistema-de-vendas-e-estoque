"""Read-only display projection. Never used by stock or adjustment services."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.utils.dateparse import parse_datetime

from .models import AplicacaoAjuste


def numero(valor):
    n = Decimal(str(valor))
    if not n.is_finite() or abs(n) >= Decimal('1000000000'):
        raise ValueError('Quantidade fora dos limites de apresentação.')
    return n


def equivalencia(valor, unidade, cadastro):
    """Use the recorded conversion, never assume pack sizes from a name."""
    try:
        if valor is None or not isinstance(cadastro, dict):
            return None
        base = str(unidade or '').strip().upper()
        if base != cadastro.get('base') or cadastro.get('continua'):
            return None
        if base == 'UN':
            fator = Decimal(1)
        elif (cadastro.get('fracionado') and cadastro.get('secundaria') == 'UN'
                and not cadastro.get('secundaria_continua')):
            fator = numero(cadastro.get('fator'))
            if fator <= 0 or fator != fator.to_integral_value():
                return None
        else:
            return None
        quantidade = numero(valor)
        bruto = quantidade * fator
        inteiro = bruto.quantize(Decimal(1), rounding=ROUND_HALF_UP)
        embalagens, avulsos = divmod(abs(inteiro), fator)
        return dict(unidades=inteiro, arredondada=bruto != inteiro,
            embalagens=int(embalagens), avulsos=int(avulsos), base=base,
            negativa=inteiro < 0, composicao=base != 'UN' and avulsos != 0 and inteiro >= 0)
    except (InvalidOperation, TypeError, ValueError):
        return None


def cadeia_registrada(aplicacao, rodada):
    """Reuse a persisted application's evidence, without re-running conciliation.

    Require the complete recorded sequence from the immutable OK to application.
    Returning nothing is intentional for legacy/incomplete evidence.
    """
    try:
        dados = aplicacao.evidencia['preparacao']
        ok = aplicacao.proposta.confirmacao
        if (ok.rodada_id != rodada.pk or ok.ok_em != rodada.ok_em
                or parse_datetime(dados['ok_em']) != ok.ok_em
                or dados['confirmacao'] != ok.pk or dados['rodada'] != rodada.pk
                or dados['produto'] != rodada.item.produto_id
                or dados['cadastro'] != rodada.item.cadastro
                or aplicacao.unidade != dados['cadastro']['base']):
            return {}
        esperado, contado = numero(dados['esperado']), numero(dados['contado'])
        diferenca = contado - esperado
        if (esperado != ok.esperado or contado != ok.contado_base
                or diferenca != aplicacao.diferenca
                or aplicacao.antes + diferenca != aplicacao.depois):
            return {}
        docs = dados['documentos']
        sequencia = dados['sequencia']
        if not isinstance(docs, list) or len(docs) > 500 or len(docs) != sequencia - ok.sequencia_ok:
            return {}
        anterior, fisico, posicao = esperado, contado, ok.sequencia_ok
        resultado = {}
        itens, observadores = set(), set()
        instante = ok.ok_em
        for doc in sorted(docs, key=lambda d: d['sequencia']):
            data = parse_datetime(doc['criado_em'])
            antes, depois, saida = numero(doc['antes']), numero(doc['depois']), numero(doc['movimentado'])
            if (doc['sequencia'] != posicao + 1 or doc['produto'] != rodada.item.produto_id
                    or doc['origem'] != 'venda' or doc['referencia'] != f'Venda #{doc["venda"]}'
                    or doc['unidade'] != aplicacao.unidade or antes != anterior
                    or saida <= 0 or antes - saida != depois or numero(doc['quantidade']) != saida
                    or data is None or not instante < data < aplicacao.aplicado_em
                    or doc['item'] in itens or doc['observador'] in observadores):
                return {}
            apos = fisico - saida
            if apos < 0:
                return {}
            resultado[doc['item']] = dict(antes=fisico, depois=apos, documento=doc,
                cadastro=dados['cadastro'], unidade=aplicacao.unidade)
            anterior, fisico, posicao, instante = depois, apos, doc['sequencia'], data
            itens.add(doc['item'])
            observadores.add(doc['observador'])
        if anterior != aplicacao.antes or fisico != aplicacao.depois:
            return {}
        return resultado
    except (KeyError, TypeError, ValueError, AttributeError, InvalidOperation):
        return {}


def apresentar(pagina, rodada):
    """Attach display-only fields, leaving original snapshots untouched."""
    cadastro = rodada.item.cadastro
    pagina.ok_equivalencias = dict(
        esperado=equivalencia(rodada.esperado, cadastro.get('base'), cadastro),
        contado=equivalencia(rodada.contado_base if rodada.ok_em else None, cadastro.get('base'), cadastro),
        diferenca=equivalencia(rodada.contado_base-rodada.esperado, cadastro.get('base'), cadastro)
            if rodada.ok_em and rodada.contado_base is not None and rodada.esperado is not None else None)
    aplicada = AplicacaoAjuste.objects.filter(proposta__rodada=rodada).select_related('proposta__confirmacao').first()
    cadeia = cadeia_registrada(aplicada, rodada) if aplicada else {}
    for linha in pagina:
        if linha['fonte'] == 'regularizacao':
            # This row can belong to another conference/round: do not use this OK's factor.
            registro = linha.get('aplicacao_registrada')
            preparacao = registro.evidencia.get('preparacao') if registro and isinstance(registro.evidencia, dict) else None
            congelado = preparacao.get('cadastro', {}) if isinstance(preparacao, dict) else {}
            linha['saldo_equivalencia'] = equivalencia(linha['saldo'], linha['saldo_unidade'], congelado)
        elif linha['fonte'] == 'venda':
            trecho = cadeia.get(linha['pk'])
            if trecho and linha.get('saldo_tipo') == 'Snapshot registrado':
                doc = trecho['documento']
                tecnicos = linha.get('registros_tecnicos', [])
                if (len(tecnicos) == 1 and tecnicos[0].pk == doc['observador']
                        and tecnicos[0].sequencia == doc['sequencia']
                        and linha['documento'] == doc['venda']
                        and linha['instante'] == parse_datetime(doc['criado_em'])
                        and linha['quantidade'] == numero(doc['quantidade'])
                        and linha['unidade'] == doc['unidade']
                        and linha['saldo_antes'] == numero(doc['antes'])
                        and linha['saldo'] == numero(doc['depois'])):
                    linha['saldo_contagem'] = trecho
                    linha['antes_equivalencia'] = equivalencia(trecho['antes'], trecho['unidade'], trecho['cadastro'])
                    linha['saldo_equivalencia'] = equivalencia(trecho['depois'], trecho['unidade'], trecho['cadastro'])
                    linha['saida_equivalencia'] = equivalencia(linha['saida'], linha['unidade'], trecho['cadastro'])
