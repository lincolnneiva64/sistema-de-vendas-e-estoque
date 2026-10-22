"""Participacao explicita, auditada sem apagar itens ou rodadas existentes."""
from django.db import transaction
from django.db.models import OuterRef, Subquery, Value
from django.db.models.functions import Coalesce

from .models import EventoInventario, ItemInventario, Inventario, RodadaContagem

RETIRADO = "produto_retirado"
INCLUIDO = "produto_reincluido"


def itens_incluidos(inventario=None):
    ultimo = EventoInventario.objects.filter(
        inventario_id=OuterRef("inventario_id"), rodada__item_id=OuterRef("pk"),
        acao__in=[RETIRADO, INCLUIDO],
    ).order_by("-pk").values("acao")[:1]
    itens = ItemInventario.objects.alias(
        _participacao=Coalesce(Subquery(ultimo), Value(INCLUIDO)),
    ).exclude(_participacao=RETIRADO)
    return itens.filter(inventario=inventario) if inventario is not None else itens


def rodadas_incluidas(inventario=None):
    return RodadaContagem.objects.filter(item__in=itens_incluidos(inventario))


def bloqueio_retirada(item):
    rodadas = list(item.rodadas.all())
    if len(rodadas) != 1:
        return "Histórico de rodadas exige tratamento específico. Os registros serão preservados."
    for rodada in rodadas:
        if (getattr(rodada, "atribuicao", None) is not None or rodada.numero != 1
                or rodada.versao or rodada.encerrada_em or rodada.ok_em
                or any(getattr(rodada, campo) is not None for campo in (
                    "embalagens", "avulsos", "contado", "contado_base", "esperado", "revisada_em"))
                or rodada.confirmacoes.all() or rodada.sinalizacoes.all() or rodada.propostas.all()):
            return "Já atribuído ou com evidência de contagem. Exige tratamento específico; os registros serão preservados."
    return ""


def opcoes_selecao(inventario):
    incluidos = set(itens_incluidos(inventario).values_list("pk", flat=True))
    itens = inventario.itens.select_related("produto").prefetch_related(
        "rodadas__atribuicao", "rodadas__confirmacoes", "rodadas__sinalizacoes", "rodadas__propostas",
    ).order_by("cadastro__nome", "pk")
    return [dict(item=item, incluido=item.pk in incluidos, bloqueio=bloqueio_retirada(item)) for item in itens]


@transaction.atomic
def salvar_selecao(usuario, inventario_id, selecionados, versao, motivo):
    from . import services
    services.permitir(usuario, "gerenciar_inventario")
    inventario = Inventario.objects.select_for_update().get(pk=inventario_id)
    services.exigir_nao_cancelada(inventario)
    if inventario.finalizada_em:
        raise ValueError("Conferência finalizada. A seleção histórica não pode ser alterada.")
    try:
        ids = {int(pk) for pk in selecionados}
        versao = int(versao)
    except (TypeError, ValueError):
        raise ValueError("Seleção ou versão inválida. Atualize a página.")
    if versao != inventario.versao:
        raise ValueError("A conferência mudou. Atualize a página antes de alterar a seleção.")
    opcoes = opcoes_selecao(inventario)
    todos = {opcao["item"].pk for opcao in opcoes}
    if not ids or not ids.issubset(todos):
        raise ValueError("Selecione pelo menos um produto desta conferência.")
    motivo = str(motivo or "").strip()
    alteradas = [o for o in opcoes if o["incluido"] != (o["item"].pk in ids)]
    if not alteradas:
        return inventario
    if not motivo or len(motivo) > 2000:
        raise ValueError("Justifique a alteração da seleção em até 2000 caracteres.")
    # Validate the entire change before recording anything; the inventory lock
    # serializes this operation with assignment, counting and finalization.
    for opcao in alteradas:
        item = opcao["item"]
        if opcao["bloqueio"]:
            raise ValueError(f'{item.cadastro["nome"]}: {opcao["bloqueio"]}')
        if item.pk in ids and (not item.produto.ativo or item.produto.excluido):
            raise ValueError("Produto indisponível para reinclusão. Revise o cadastro.")
    for opcao in alteradas:
        item = opcao["item"]
        services.evento(inventario, INCLUIDO if item.pk in ids else RETIRADO, usuario,
            rodada=list(item.rodadas.all())[0], item_id=item.pk, produto_id=item.produto_id,
            nome=item.cadastro["nome"], motivo=motivo, ciclo=inventario.ciclo)
    services.atualizar_versao(inventario)
    if any(o["item"].pk not in ids for o in alteradas):
        inventario.pendencias_conclusao = services.concluir_se_pronta(
            inventario, usuario=usuario, origem="retirada_explicita")
    return inventario
