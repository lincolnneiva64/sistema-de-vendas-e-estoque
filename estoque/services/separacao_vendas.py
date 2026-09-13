from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from estoque.models import ItemVenda, SeparacaoVenda, SeparacaoVendaItem, Venda


STATUS_ITENS_COM_PENDENCIA = {
    SeparacaoVendaItem.STATUS_NAO_ENCONTRADO,
    SeparacaoVendaItem.STATUS_QUANTIDADE_INSUFICIENTE,
}


def usuario_autenticado_ou_none(usuario):
    if getattr(usuario, "is_authenticated", False):
        return usuario
    return None


def _proximo_numero_sequencial_dia(data_sequencia):
    sequencias = list(
        SeparacaoVenda.objects
        .select_for_update()
        .filter(data_sequencia=data_sequencia)
        .order_by("-numero_sequencial_dia")
        .values_list("numero_sequencial_dia", flat=True)[:1]
    )
    return (sequencias[0] if sequencias else 0) + 1


def criar_ou_obter_separacao_venda(venda, usuario=None, responsavel=None):
    if venda.cancelada:
        raise ValueError("Venda cancelada nao pode ser enviada para separacao.")

    usuario = usuario_autenticado_ou_none(usuario)

    for _tentativa in range(3):
        try:
            with transaction.atomic():
                venda_bloqueada = Venda.objects.select_for_update().get(pk=venda.pk)
                if venda_bloqueada.cancelada:
                    raise ValueError("Venda cancelada nao pode ser enviada para separacao.")

                separacao = SeparacaoVenda.objects.filter(venda=venda_bloqueada).first()
                if separacao:
                    responsavel_atualizado = False
                    if responsavel and separacao.responsavel_id != responsavel.id:
                        separacao.responsavel = responsavel
                        separacao.save(update_fields=["responsavel", "atualizado_em"])
                        responsavel_atualizado = True
                    return separacao, False, responsavel_atualizado

                data_sequencia = timezone.localdate()
                separacao = SeparacaoVenda.objects.create(
                    venda=venda_bloqueada,
                    enviado_por=usuario,
                    responsavel=responsavel,
                    data_sequencia=data_sequencia,
                    numero_sequencial_dia=_proximo_numero_sequencial_dia(data_sequencia),
                )

                itens = list(
                    ItemVenda.objects.select_related("produto")
                    .filter(venda=venda_bloqueada)
                    .order_by("id")
                )
                SeparacaoVendaItem.objects.bulk_create([
                    SeparacaoVendaItem(
                        separacao=separacao,
                        item_venda=item,
                        produto_nome_snapshot=item.produto.nome if item.produto else "Produto nao identificado",
                        unidade_snapshot=item.unidade or "",
                        quantidade_solicitada=Decimal(item.quantidade or "0").quantize(Decimal("0.001")),
                    )
                    for item in itens
                ])
            return separacao, True, False
        except IntegrityError:
            separacao = SeparacaoVenda.objects.filter(venda_id=venda.pk).first()
            if separacao:
                return separacao, False, False

    separacao = SeparacaoVenda.objects.filter(venda_id=venda.pk).first()
    if separacao:
        return separacao, False, False
    raise IntegrityError("Nao foi possivel atribuir a sequencia diaria da separacao.")


def recalcular_status_separacao(separacao, usuario=None):
    itens = list(separacao.itens.all())
    status_anterior = separacao.status
    agora = timezone.now()

    if not itens or all(item.status == SeparacaoVendaItem.STATUS_PENDENTE for item in itens):
        novo_status = SeparacaoVenda.STATUS_ENVIADA
    elif any(item.status in STATUS_ITENS_COM_PENDENCIA for item in itens):
        novo_status = SeparacaoVenda.STATUS_COM_PENDENCIA
    elif all(item.status == SeparacaoVendaItem.STATUS_CONFERIDO for item in itens):
        novo_status = SeparacaoVenda.STATUS_SEPARADA
    else:
        novo_status = SeparacaoVenda.STATUS_EM_SEPARACAO

    campos = []
    if separacao.status != novo_status:
        separacao.status = novo_status
        campos.append("status")

    if novo_status != SeparacaoVenda.STATUS_ENVIADA and not separacao.iniciado_em:
        separacao.iniciado_em = agora
        campos.append("iniciado_em")

    if novo_status in {SeparacaoVenda.STATUS_SEPARADA, SeparacaoVenda.STATUS_COM_PENDENCIA}:
        if not separacao.finalizado_em and all(item.status != SeparacaoVendaItem.STATUS_PENDENTE for item in itens):
            separacao.finalizado_em = agora
            campos.append("finalizado_em")
        usuario = usuario_autenticado_ou_none(usuario)
        if usuario and not separacao.separado_por_id:
            separacao.separado_por = usuario
            campos.append("separado_por")
    elif status_anterior in {SeparacaoVenda.STATUS_SEPARADA, SeparacaoVenda.STATUS_COM_PENDENCIA}:
        separacao.finalizado_em = None
        campos.append("finalizado_em")

    if campos:
        campos.append("atualizado_em")
        separacao.save(update_fields=list(dict.fromkeys(campos)))

    return separacao.status


def divergencias_separacao_venda(separacao):
    divergencias = []
    itens_separacao = list(separacao.itens.select_related("item_venda", "item_venda__produto"))
    itens_atuais = {
        item.id: item
        for item in ItemVenda.objects.select_related("produto").filter(venda=separacao.venda)
    }
    ids_snapshot = {item.item_venda_id for item in itens_separacao}
    ids_atuais = set(itens_atuais)

    for item_id in sorted(ids_snapshot - ids_atuais):
        divergencias.append(f"Item #{item_id} foi removido da venda apos o envio para separacao.")

    for item_id in sorted(ids_atuais - ids_snapshot):
        item = itens_atuais[item_id]
        nome = item.produto.nome if item.produto else "Produto nao identificado"
        divergencias.append(f"Item {nome} foi adicionado a venda apos o envio para separacao.")

    for item_sep in itens_separacao:
        item_atual = itens_atuais.get(item_sep.item_venda_id)
        if not item_atual:
            continue
        quantidade_atual = Decimal(item_atual.quantidade or "0").quantize(Decimal("0.001"))
        quantidade_snapshot = Decimal(item_sep.quantidade_solicitada or "0").quantize(Decimal("0.001"))
        nome_atual = item_atual.produto.nome if item_atual.produto else "Produto nao identificado"
        unidade_atual = item_atual.unidade or ""

        if nome_atual != item_sep.produto_nome_snapshot:
            divergencias.append(
                f"Produto alterado no item #{item_atual.id}: {item_sep.produto_nome_snapshot} -> {nome_atual}."
            )
        if unidade_atual != (item_sep.unidade_snapshot or ""):
            divergencias.append(
                f"Unidade alterada em {nome_atual}: {item_sep.unidade_snapshot or '-'} -> {unidade_atual or '-'}."
            )
        if quantidade_atual != quantidade_snapshot:
            divergencias.append(
                f"Quantidade alterada em {nome_atual}: {quantidade_snapshot} -> {quantidade_atual}."
            )

    return divergencias
