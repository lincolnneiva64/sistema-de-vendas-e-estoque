"""Single-item adapter for the existing transactional sale editing service."""
from decimal import Decimal, InvalidOperation
from django.core import signing
from django.db import transaction
from estoque.models import Venda, ItemVenda, Produto, ContaReceber
from .vendas import criar_ou_atualizar_venda

SALT = 'estoque.alterar-quantidade.v1'


def unidades(produto):
    if not produto:
        return []
    resultado = [(produto.unidade_venda_1 or produto.unidade_compra or '').strip().upper()]
    if produto.vende_fracionado:
        resultado.append((produto.unidade_venda_2 or '').strip().upper())
    return list(dict.fromkeys(u for u in resultado if u))


def cadastro(produto):
    return [produto.pk, unidades(produto), str(produto.fator_conversao), produto.vende_fracionado]


def valores(item, quantidade, unidade):
    from estoque.views import _validar_quantidade_produto_unidade
    if not item.produto_id:
        raise ValueError('Produto removido: revise a nota pelo processo apropriado.')
    unidade = str(unidade or item.unidade).strip().upper()
    try:
        qtd = Decimal(str(quantidade).strip().replace(',', '.'))
        if not qtd.is_finite() or not 0 < qtd < Decimal('1000000000') or qtd != qtd.quantize(Decimal('.001')):
            raise ValueError('Informe quantidade positiva com ate tres casas decimais.')
    except (InvalidOperation, TypeError):
        raise ValueError('Quantidade invalida.')
    permitidas = unidades(item.produto)
    antiga = str(item.unidade).strip().upper()
    if unidade not in permitidas or antiga not in permitidas:
        raise ValueError('Unidade incompativel com o cadastro do produto.')
    if unidade == (item.produto.unidade_venda_2 or '').strip().upper() and unidade not in {'KG', 'MIL'} and qtd != qtd.to_integral_value():
        raise ValueError('Informe unidades avulsas inteiras.')
    _validar_quantidade_produto_unidade(item.produto, qtd, unidade)
    preco = item.preco_unitario
    if unidade != antiga:
        fator = Decimal(item.produto.fator_conversao or 0)
        if not fator.is_finite() or fator <= 0:
            raise ValueError('Fator de conversao invalido.')
        # Convert the negotiated price exactly, not a rounded quantity in boxes.
        preco = (preco / fator if antiga == permitidas[0] else preco * fator).quantize(Decimal('.01'))
    total = (qtd * preco).quantize(Decimal('.01'))
    if preco <= 0 or preco >= Decimal('10000000000') or total >= Decimal('10000000000'):
        raise ValueError('Quantidade ou preco fora do limite da nota.')
    return qtd, unidade, preco, total


def previa(usuario, venda, item, quantidade, unidade):
    qtd, unidade, preco, total = valores(item, quantidade, unidade)
    dados = dict(usuario=usuario.pk, venda=venda.pk, item=item.pk, versao=venda.atualizado_em.isoformat(),
        cadastro=cadastro(item.produto), origem=[str(item.quantidade), item.unidade, str(item.preco_unitario), item.produto_id], quantidade=str(qtd), unidade=unidade, preco=str(preco))
    return qtd, unidade, preco, total, signing.dumps(dados, salt=SALT)


@transaction.atomic
def confirmar(usuario, venda_id, item_id, token, quantidade, unidade):
    from estoque.views import _contexto_venda_quitada
    if not getattr(usuario, 'is_authenticated', False):
        raise ValueError('Entre com sua conta para continuar.')
    try:
        dados = signing.loads(token or '', salt=SALT, max_age=3600)
    except signing.BadSignature:
        raise ValueError('Confirmacao invalida ou expirada. Revise novamente a quantidade.')
    if (dados['usuario'], dados['venda'], dados['item']) != (usuario.pk, venda_id, item_id):
        raise ValueError('Confirmacao nao pertence a esta venda ou usuario.')
    venda = Venda.objects.select_for_update().get(pk=venda_id)
    itens = list(ItemVenda.objects.select_for_update().filter(venda=venda).order_by('pk').prefetch_related('produto'))
    item = next((i for i in itens if i.pk == item_id), None)
    if not item or not item.produto_id:
        raise ValueError('Item removido. Reabra a venda.')
    item.produto = Produto.objects.select_for_update().get(pk=item.produto_id)
    list(ContaReceber.objects.select_for_update().filter(venda=venda))
    if venda.cancelada or _contexto_venda_quitada(venda).get('quitada'):
        raise ValueError('Venda cancelada ou com pagamento/baixa: edicao comum bloqueada.')
    if cadastro(item.produto) != dados['cadastro']:
        raise ValueError('Cadastro do produto alterado. Revise novamente a quantidade.')
    # Validate the submitted target against the server-signed preview.
    try:
        qtd = Decimal(str(quantidade).replace(',', '.'))
    except InvalidOperation:
        raise ValueError('Quantidade invalida.')
    if not qtd.is_finite() or qtd != Decimal(dados['quantidade']) or unidade != dados['unidade']:
        raise ValueError('Valores diferentes da previa. Revise novamente a quantidade.')
    preco = Decimal(dados['preco'])
    if (item.quantidade, item.unidade, item.preco_unitario) == (qtd, unidade, preco):
        return venda, item, False  # Lost response / double click: no repeated effects.
    origem = [str(item.quantidade), item.unidade, str(item.preco_unitario), item.produto_id]
    if venda.atualizado_em.isoformat() != dados['versao'] or origem != dados['origem']:
        raise ValueError('A venda foi alterada por outra operacao. Revise novamente.')
    payload = dict(venda_id=venda.pk, cliente_id=venda.cliente_id, data_venda=venda.data_venda.isoformat(),
        data_vencimento=venda.data_vencimento.isoformat() if venda.data_vencimento else '',
        tipo_pagamento=venda.tipo_pagamento, operador=venda.operador, itens=[])
    for atual in itens:
        if not atual.produto_id:
            raise ValueError('A nota possui produto removido. Revise pelo processo apropriado.')
        payload['itens'].append(dict(item_id=atual.pk, produto_id=atual.produto_id, produto_nome=atual.produto.nome,
            quantidade=str(qtd if atual.pk == item_id else atual.quantidade),
            unidade=unidade if atual.pk == item_id else atual.unidade,
            preco_unitario=str(preco if atual.pk == item_id else atual.preco_unitario)))
    total_novo = sum(Decimal(linha['quantidade']) * Decimal(linha['preco_unitario']) for linha in payload['itens'])
    if total_novo >= Decimal('10000000000'):
        raise ValueError('Total da nota fora do limite permitido.')
    from inventario.models import SequenciaEstoque, MovimentoObservado
    from estoque.views import _registrar_evento_venda
    sequencia_antes = SequenciaEstoque.objects.filter(produto_id=item.produto_id).values_list('sequencia', flat=True).first() or 0
    anterior = f'{item.quantidade} {item.unidade}, preco {item.preco_unitario}'
    resultado = criar_ou_atualizar_venda(usuario=usuario, dados=payload)
    # The product lock scopes this attribution to writes from this operation.
    MovimentoObservado.objects.filter(produto_id=item.produto_id, sequencia__gt=sequencia_antes,
        origem__in=['venda', 'devolucao_venda']).update(referencia=f'Venda #{venda.pk}')
    _registrar_evento_venda(resultado.venda, 'quantidade_item_confirmada',
        f'Item #{item.pk}: {anterior} -> {qtd} {unidade}, preco {preco}. Confirmacao pela previa de quantidade.',
        canal='sistema', usuario=usuario.get_username())
    item.refresh_from_db()
    return resultado.venda, item, True
