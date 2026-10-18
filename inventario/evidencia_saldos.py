"""Pure evidence checks: no writes, current conversions or inferred opening zero."""
import re


def unidade(valor):
    return str(valor or '').strip().upper()


def snapshot(registro, conflito=''):
    if conflito:
        return None, conflito
    if registro.get('cancelada') or registro.get('devolvido'):
        return None, 'Venda cancelada ou devolução registrada; sequência interrompida.'
    antes, movimento, depois = (registro.get(k) for k in ('antes', 'movimentado', 'depois'))
    if any(v is None for v in (antes, movimento, depois)):
        return None, 'Snapshot incompleto; sequência interrompida.'
    base = unidade(registro.get('base'))
    if not base or base != unidade(registro.get('unidade')):
        return None, 'Unidade ou conversão histórica não comprovada; nenhuma conversão atual foi usada.'
    if movimento <= 0 or min(antes, depois) < 0 or antes - movimento != depois:
        return None, 'Snapshot aritmeticamente inconsistente; sequência interrompida.'
    if registro.get('quantidade') != movimento:
        return None, 'Quantidade documental diverge do snapshot; sequência interrompida.'
    return dict(antes=antes, depois=depois, base=base), ''


BENIGNOS = frozenset(('venda_gravada', 'nota_visualizada', 'whatsapp_aberto',
    'whatsapp_confirmado', 'checklist_cliente_whatsapp_aberto', 'checklist_cliente_enviado',
    'pedido_parcial'))


def conflito_eventos(eventos, outros_nomes):
    """Ignore a stock edit only if its exact named item is independently another SKU.

    Unknown events, target edits and ambiguous names invalidate the candidate.
    A different name alone is never proof that the event concerned another product.
    """
    padroes = {
        'item_adicionado_na_nota': r'Item adicionado na nota: (.+), quantidade ',
        'item_removido_da_nota': r'Item removido da nota: (.+), quantidade ',
        'quantidade_item_alterada': r'Quantidade alterada na nota: (.+)\. De ',
    }
    for evento in eventos:
        if evento['tipo_evento'] in BENIGNOS:
            continue
        padrao = padroes.get(evento['tipo_evento'])
        match = re.match(padrao, evento.get('descricao', '')) if padrao else None
        if match and match[1].strip().casefold() in outros_nomes:
            continue
        return 'Edição do produto ou evento sem atribuição segura; snapshot não certificado.'
    return ''


def ponte(anterior, posterior, compras, obstaculos=False):
    """Corroborate one receipt using two independently stored sale snapshots.

    This is documentary corroboration, not proof that pre-observer history is complete.
    Both endpoints must have passed snapshot() including edit checks beforehand.
    """
    if obstaculos or anterior is None or posterior is None or len(compras) != 1:
        return None
    compra = compras[0]
    if (compra.get('cancelada') or compra.get('editada') or compra.get('quantidade') is None
            or compra['quantidade'] <= 0 or not compra.get('instante')):
        return None
    if not (anterior['instante'] < compra['instante'] < posterior['instante']):
        return None
    if not (anterior['base'] == posterior['base'] == unidade(compra.get('unidade'))):
        return None
    saldo = anterior['depois'] + compra['quantidade']
    if saldo != posterior['antes']:
        return None
    return dict(antes=anterior['depois'], depois=saldo, base=anterior['base'],
        origem=f"Reconstruído e corroborado: snapshot da venda #{anterior['documento']} + entrada documental; saldo antes da venda #{posterior['documento']} confirma o resultado.",
        limite='Não é snapshot do recebimento nem certificação integral de movimentos anteriores ao observador.')
