from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.utils import timezone

from estoque.models import EventoVenda, ItemVenda, SeparacaoVenda, SeparacaoVendaItem, Venda


STATUS_ITENS_COM_PENDENCIA = {
    SeparacaoVendaItem.STATUS_NAO_ENCONTRADO,
    SeparacaoVendaItem.STATUS_QUANTIDADE_INSUFICIENTE,
}


UNIDADES_PESO_REAL = {"KG"}


TIPOS_EVENTO_ALTERACAO_FISICA = {
    "quantidade_item_alterada",
    "item_removido_da_nota",
    "produto_adicionado_na_nota",
    "item_adicionado_na_nota",
    "remocao_item_desfeita",
}


def marco_ultima_conferencia_separacao(separacao):
    marcos = [
        marco
        for marco in (
            separacao.finalizado_em,
            separacao.revisao_concluida_em,
        )
        if marco is not None
    ]

    return max(marcos) if marcos else None


def eventos_fisicos_apos_separacao(separacao):
    marco = marco_ultima_conferencia_separacao(separacao)
    if marco is None:
        return []

    ultimo_atendimento = (
        EventoVenda.objects.filter(
            venda_id=separacao.venda_id,
            tipo_evento="alteracao_separacao_atendida",
            criado_em__gt=marco,
        )
        .order_by("-criado_em", "-id")
        .first()
    )

    if ultimo_atendimento is not None:
        marco = ultimo_atendimento.criado_em

    return list(
        EventoVenda.objects.filter(
            venda_id=separacao.venda_id,
            tipo_evento__in=TIPOS_EVENTO_ALTERACAO_FISICA,
            criado_em__gt=marco,
        ).order_by("criado_em", "id")
    )


def _separar_quantidade_unidade(texto):
    partes = texto.strip().rsplit(" ", 1)
    if len(partes) != 2:
        return None, None
    return partes[0].strip(), partes[1].strip()


def _dados_evento_item_adicionado(evento):
    descricao = (evento.descricao or "").strip()
    prefixo = "Item adicionado na nota: "

    if not descricao.startswith(prefixo):
        return None

    detalhe = descricao[len(prefixo):]
    if ", quantidade " not in detalhe:
        return None

    produto, restante = detalhe.split(", quantidade ", 1)
    quantidade_unidade = restante.split(",", 1)[0].rstrip(".").strip()
    quantidade, unidade = _separar_quantidade_unidade(quantidade_unidade)

    if quantidade is None:
        return None

    return {
        "tipo": "adicionado",
        "produto": produto.strip(),
        "quantidade": quantidade,
        "unidade": unidade,
        "evento_id": evento.id,
    }


def _dados_evento_item_removido(evento):
    descricao = (evento.descricao or "").strip()
    prefixo = "Item removido da nota: "

    if not descricao.startswith(prefixo):
        return None

    detalhe = descricao[len(prefixo):]
    if ", quantidade " not in detalhe:
        return None

    produto, restante = detalhe.split(", quantidade ", 1)
    quantidade_unidade = restante.split(",", 1)[0].rstrip(".").strip()
    quantidade, unidade = _separar_quantidade_unidade(quantidade_unidade)

    if quantidade is None:
        return None

    return {
        "tipo": "removido",
        "produto": produto.strip(),
        "quantidade": quantidade,
        "unidade": unidade,
        "evento_id": evento.id,
    }


def _dados_evento_remocao_desfeita(evento):
    descricao = (evento.descricao or "").strip()
    prefixo = "Remocao de item desfeita: "

    if not descricao.startswith(prefixo):
        return None

    detalhe = descricao[len(prefixo):]
    if ", quantidade " not in detalhe:
        return None

    produto, restante = detalhe.split(", quantidade ", 1)
    quantidade_unidade = restante.split(",", 1)[0].rstrip(".").strip()
    quantidade, unidade = _separar_quantidade_unidade(quantidade_unidade)

    if quantidade is None:
        return None

    return {
        "tipo": "remocao_desfeita",
        "produto": produto.strip(),
        "quantidade": quantidade,
        "unidade": unidade,
        "evento_id": evento.id,
    }


def _dados_evento_quantidade_alterada(evento):
    descricao = (evento.descricao or "").strip()
    prefixo = "Quantidade alterada na nota: "

    if not descricao.startswith(prefixo):
        return None

    detalhe = descricao[len(prefixo):]
    if ". De " not in detalhe or " para " not in detalhe:
        return None

    produto, restante = detalhe.split(". De ", 1)
    anterior_texto, novo_texto = restante.split(" para ", 1)
    novo_texto = novo_texto.split(".", 1)[0].strip()

    quantidade_anterior, unidade_anterior = _separar_quantidade_unidade(
        anterior_texto
    )
    quantidade_nova, unidade_nova = _separar_quantidade_unidade(novo_texto)

    if quantidade_anterior is None or quantidade_nova is None:
        return None

    return {
        "tipo": "quantidade_alterada",
        "produto": produto.strip(),
        "quantidade_anterior": quantidade_anterior,
        "unidade_anterior": unidade_anterior,
        "quantidade_nova": quantidade_nova,
        "unidade_nova": unidade_nova,
        "evento_id": evento.id,
    }


def alteracoes_fisicas_apos_separacao(separacao):
    alteracoes = []

    for evento in eventos_fisicos_apos_separacao(separacao):
        dados = None

        if evento.tipo_evento in {
            "produto_adicionado_na_nota",
            "item_adicionado_na_nota",
        }:
            dados = _dados_evento_item_adicionado(evento)

        elif evento.tipo_evento == "item_removido_da_nota":
            dados = _dados_evento_item_removido(evento)

        elif evento.tipo_evento == "remocao_item_desfeita":
            dados = _dados_evento_remocao_desfeita(evento)

        elif evento.tipo_evento == "quantidade_item_alterada":
            dados = _dados_evento_quantidade_alterada(evento)

        if dados:
            alteracoes.append(dados)

    return alteracoes



def _decimal_quantidade(valor):
    try:
        return Decimal(str(valor).replace(",", "."))
    except (InvalidOperation, TypeError, ValueError):
        return None


def consolidar_alteracoes_fisicas_apos_separacao(separacao):
    alteracoes = alteracoes_fisicas_apos_separacao(separacao)
    quantidades = {}
    outras = []

    for alteracao in alteracoes:
        if alteracao["tipo"] != "quantidade_alterada":
            outras.append(alteracao)
            continue

        chave = (
            alteracao["produto"],
            alteracao["unidade_anterior"],
            alteracao["unidade_nova"],
        )

        if chave not in quantidades:
            quantidades[chave] = dict(alteracao)
        else:
            quantidades[chave]["quantidade_nova"] = alteracao["quantidade_nova"]
            quantidades[chave]["evento_id"] = alteracao["evento_id"]

    resultado = list(outras)

    for alteracao in quantidades.values():
        anterior = _decimal_quantidade(alteracao["quantidade_anterior"])
        nova = _decimal_quantidade(alteracao["quantidade_nova"])

        if anterior is None or nova is None:
            resultado.append(alteracao)
            continue

        diferenca = nova - anterior

        if diferenca == 0:
            continue

        resultado.append({
            "tipo": "acrescentar" if diferenca > 0 else "retirar",
            "produto": alteracao["produto"],
            "quantidade": abs(diferenca),
            "unidade": alteracao["unidade_nova"],
            "evento_id": alteracao["evento_id"],
        })

    resultado.sort(key=lambda item: item["evento_id"])
    return resultado


def _normalizar_unidade(unidade):
    return str(unidade or "").strip().upper()


def item_separacao_registra_peso_real(item):
    return _normalizar_unidade(getattr(item, "unidade_snapshot", "")) in UNIDADES_PESO_REAL


def item_separacao_tem_pendencia(item):
    if item.status == SeparacaoVendaItem.STATUS_NAO_ENCONTRADO:
        return True
    if item.status == SeparacaoVendaItem.STATUS_QUANTIDADE_INSUFICIENTE:
        return not item_separacao_registra_peso_real(item)
    return False


def usuario_autenticado_ou_none(usuario):
    if getattr(usuario, "is_authenticated", False):
        return usuario
    return None


def _quantidade_item_venda_snapshot(item):
    return Decimal(item.quantidade or "0").quantize(Decimal("0.001"))


def _produto_nome_item_venda(item):
    return item.produto.nome if item.produto else "Produto nao identificado"


def _item_separacao_igual_venda_atual(item_sep, item_venda):
    return (
        item_sep.produto_nome_snapshot == _produto_nome_item_venda(item_venda)
        and (item_sep.unidade_snapshot or "") == (item_venda.unidade or "")
        and Decimal(item_sep.quantidade_solicitada or "0").quantize(Decimal("0.001"))
        == _quantidade_item_venda_snapshot(item_venda)
    )




def sincronizar_checklist_aberto_com_venda(separacao):
    """
    Sincroniza uma separacao que ainda nunca foi concluida com a venda atual.

    Itens que nao mudaram preservam a conferencia.
    Itens novos entram pendentes.
    Itens alterados voltam para pendente.
    Itens removidos saem do checklist.

    Nao cria revisao.
    """
    if not separacao:
        return False

    # Se ja houve conclusao/revisao, este nao e mais um checklist original aberto.
    if separacao.finalizado_em or separacao.teve_revisao or separacao.revisao_pendente:
        return False

    if not divergencias_separacao_venda(separacao):
        return False

    with transaction.atomic():
        separacao = (
            SeparacaoVenda.objects
            .select_for_update()
            .select_related("venda")
            .get(pk=separacao.pk)
        )

        # Confere novamente depois do lock.
        if separacao.finalizado_em or separacao.teve_revisao or separacao.revisao_pendente:
            return False

        itens_atuais = {
            item.id: item
            for item in ItemVenda.objects.select_related("produto")
            .filter(venda=separacao.venda)
            .order_by("id")
        }
        itens_snapshot = {
            item.item_venda_id: item
            for item in SeparacaoVendaItem.objects.select_for_update()
            .filter(separacao=separacao)
        }

        removidos = [
            item_sep.pk
            for item_id, item_sep in itens_snapshot.items()
            if item_id not in itens_atuais
        ]
        if removidos:
            SeparacaoVendaItem.objects.filter(pk__in=removidos).delete()

        alterou = bool(removidos)

        for item_id, item_venda in itens_atuais.items():
            item_sep = itens_snapshot.get(item_id)
            produto_nome = _produto_nome_item_venda(item_venda)
            unidade = item_venda.unidade or ""
            quantidade = _quantidade_item_venda_snapshot(item_venda)

            if not item_sep:
                SeparacaoVendaItem.objects.create(
                    separacao=separacao,
                    item_venda=item_venda,
                    produto_nome_snapshot=produto_nome,
                    unidade_snapshot=unidade,
                    quantidade_solicitada=quantidade,
                )
                alterou = True
                continue

            if _item_separacao_igual_venda_atual(item_sep, item_venda):
                continue

            item_sep.produto_nome_snapshot = produto_nome
            item_sep.unidade_snapshot = unidade
            item_sep.quantidade_solicitada = quantidade
            item_sep.quantidade_separada = None
            item_sep.status = SeparacaoVendaItem.STATUS_PENDENTE
            item_sep.observacao = ""
            item_sep.save(update_fields=[
                "produto_nome_snapshot",
                "unidade_snapshot",
                "quantidade_solicitada",
                "quantidade_separada",
                "status",
                "observacao",
                "atualizado_em",
            ])
            alterou = True

        if alterou:
            recalcular_status_separacao(separacao)

        return alterou


def marcar_revisao_pendente_se_necessario(separacao):
    if not separacao or not separacao.finalizado_em:
        return False
    if separacao.revisao_pendente:
        return False
    if not divergencias_separacao_venda(separacao):
        return False

    agora = timezone.now()
    separacao.revisao_pendente = True
    separacao.revisao_solicitada_em = agora
    separacao.save(update_fields=["revisao_pendente", "revisao_solicitada_em", "atualizado_em"])
    return True




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
                    sincronizar_checklist_aberto_com_venda(separacao)
                    separacao.refresh_from_db()
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
    elif any(item_separacao_tem_pendencia(item) for item in itens):
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
        if novo_status == SeparacaoVenda.STATUS_SEPARADA and separacao.revisao_pendente:
            separacao.revisao_pendente = False
            separacao.teve_revisao = True
            separacao.revisao_concluida_em = agora
            campos.extend(["revisao_pendente", "teve_revisao", "revisao_concluida_em"])
    elif status_anterior in {SeparacaoVenda.STATUS_SEPARADA, SeparacaoVenda.STATUS_COM_PENDENCIA}:
        separacao.finalizado_em = None
        campos.append("finalizado_em")

    if campos:
        campos.append("atualizado_em")
        separacao.save(update_fields=list(dict.fromkeys(campos)))

    return separacao.status


def divergencias_separacao_venda(separacao):
    divergencias = []
    itens_separacao_prefetch = getattr(separacao, "_prefetched_objects_cache", {}).get("itens")
    if itens_separacao_prefetch is not None:
        itens_separacao = list(itens_separacao_prefetch)
    else:
        itens_separacao = list(separacao.itens.select_related("item_venda", "item_venda__produto"))

    itens_venda_prefetch = getattr(separacao.venda, "_prefetched_objects_cache", {}).get("itens")
    if itens_venda_prefetch is not None:
        itens_atuais = {item.id: item for item in itens_venda_prefetch}
    else:
        itens_atuais = {
            item.id: item
            for item in ItemVenda.objects.select_related("produto").filter(venda=separacao.venda)
        }
    ids_snapshot = {
        item.item_venda_id
        for item in itens_separacao
        if item.item_venda_id is not None
    }
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
        quantidade_peso_real = (
            Decimal(item_sep.quantidade_separada or "0").quantize(Decimal("0.001"))
            if item_sep.quantidade_separada is not None
            else None
        )
        peso_real_reconciliado = (
            item_separacao_registra_peso_real(item_sep)
            and item_sep.status == SeparacaoVendaItem.STATUS_CONFERIDO
            and quantidade_peso_real is not None
            and quantidade_atual == quantidade_peso_real
        )

        if quantidade_atual != quantidade_snapshot and not peso_real_reconciliado:
            divergencias.append(
                f"Quantidade alterada em {nome_atual}: {quantidade_snapshot} -> {quantidade_atual}."
            )

    return divergencias
