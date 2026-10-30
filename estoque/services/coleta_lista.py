"""Collection metadata only; never changes prices, stock or receiving states."""
from django.core.exceptions import ValidationError

ESTADOS = {'pendente', 'pego', 'removido'}


def versao(lista):
    return (lista.coleta_mobile or {}).get('versao', 0)


def conferir(lista, dados):
    if (dados.get('versao') != versao(lista)
            or dados.get('atualizada') != lista.atualizado_em.isoformat()):
        raise ValidationError('A lista mudou em outra tela. Reabra a lista para continuar; nenhuma alteração foi aplicada.')


def iniciar_ou_mover(lista, dados):
    conferir(lista, dados)
    estado = lista.coleta_mobile or {}
    itens = dict(estado.get('itens', {}))
    ids = {str(pk) for pk in lista.itens.values_list('produto_id', flat=True) if pk}
    acao = dados.get('acao')
    if acao in ('iniciar_pendente', 'iniciar_pego'):
        if estado.get('iniciada'):
            raise ValidationError('A coleta já foi iniciada. Reabra a lista para consultar o estado salvo.')
        if acao == 'iniciar_pego' and lista.coleta_mobile is not None:
            raise ValidationError('Uma lista nova deve começar na pré-lista.')
        itens = {pk: 'pego' if acao == 'iniciar_pego' else 'pendente' for pk in ids}
    elif acao == 'mover':
        pk = str(dados.get('produto', ''))
        if not estado.get('iniciada') or pk not in ids or dados.get('estado') not in ESTADOS:
            raise ValidationError('Inicie a compra e escolha um produto válido da lista.')
        itens[pk] = dados['estado']
    else:
        raise ValidationError('Ação de coleta inválida.')
    lista.coleta_mobile = dict(iniciada=True, versao=versao(lista)+1, itens=itens)
    lista.save(update_fields=['coleta_mobile'])
    return lista.coleta_mobile


def ao_salvar(lista, produtos, coleta=None):
    """Desktop retains metadata; mobile explicitly supplies every remaining state."""
    anterior = lista.coleta_mobile
    if coleta is not None:
        conferir(lista, coleta)
        if not (anterior or {}).get('iniciada'):
            raise ValidationError('Inicie a compra antes de salvar a coleta.')
        itens = coleta.get('itens')
        if (not isinstance(itens, dict) or set(itens) != set(produtos)
                or any(not isinstance(e, str) or e not in ESTADOS - {'removido'} for e in itens.values())):
            raise ValidationError('O estado dos produtos está incompleto. Confira a lista antes de salvar.')
    elif anterior is None:
        return None
    else:
        itens = {pk: anterior.get('itens', {}).get(pk, 'pendente') for pk in produtos}
    return dict(iniciada=bool((anterior or {}).get('iniciada')),
                versao=versao(lista)+1, itens=itens)
