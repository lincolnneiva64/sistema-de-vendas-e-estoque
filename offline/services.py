import hashlib
import json
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from locacoes.models import EventoLocacao, Locacao, TarefaOperacionalLocacao
from estoque.models import EntregaRotaItem, EventoVenda
from .models import OperacaoSincronizacao, RevisaoVendaOffline, EncerramentoVendaOffline
from .sale_commands import SALE_OPERATION_TYPE, execute_sale, validate_sale_payload
from estoque.services.vendas import ErroGravarVenda

PROTOCOL_VERSION = 1
OPERATION_TYPE = "observacao_operacional"
COMMAND_KEYS = {"operation_id", "device_id", "actor_id", "environment_id", "type", "schema_version", "aggregate_id", "payload", "created_at", "sequence"}
ACTIVE_STATUSES = {"pendente", "parcial", "nao_possivel"}


def command_hash(command):
    encoded = json.dumps(command, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_command(data, user, environment):
    if not isinstance(data, dict) or set(data) != COMMAND_KEYS | {"payload_hash"}:
        raise ValidationError("Formato de operacao invalido.")
    command = {key: data[key] for key in COMMAND_KEYS}
    for key in ("operation_id", "device_id"):
        if not isinstance(command[key], str) or str(UUID(command[key])) != command[key]:
            raise ValidationError("UUID invalido.")
    if command["actor_id"] != str(user.pk):
        raise ValidationError("A operacao pertence a outro usuario.")
    if command["environment_id"] != environment:
        raise ValidationError("Ambiente incorreto.")
    sale_note = command["type"] == "observacao_entrega_venda"
    sale_create = command["type"] == SALE_OPERATION_TYPE
    if command["type"] not in {OPERATION_TYPE, "observacao_entrega_venda", SALE_OPERATION_TYPE} or type(command["schema_version"]) is not int or command["schema_version"] != 1:
        raise ValidationError("Protocolo de operacao incompativel.")
    if sale_create:
        if command["aggregate_id"] != command["operation_id"]:
            raise ValidationError("Identidade da nova venda invalida.")
    elif not isinstance(command["aggregate_id"], str) or not command["aggregate_id"].isdigit() or len(command["aggregate_id"]) > 18:
        raise ValidationError("Tarefa invalida.")
    if type(command["sequence"]) is not int or not 0 < command["sequence"] <= 9007199254740991:
        raise ValidationError("Sequencia invalida.")
    created = parse_datetime(command["created_at"]) if isinstance(command["created_at"], str) else None
    if created is None or timezone.is_naive(created):
        raise ValidationError("Data invalida.")
    payload = command["payload"]
    if sale_create:
        validate_sale_payload(payload)
        if data["payload_hash"] != command_hash(command):
            raise ValidationError("Hash do comando invalido.")
        return command
    expected = {"rota_id", "venda_id", "tarefa_status", "observacao"} if sale_note else {"locacao_id", "tarefa_status", "observacao"}
    if not isinstance(payload, dict) or set(payload) != expected:
        raise ValidationError("Conteudo de observacao invalido.")
    for key in ({"rota_id", "venda_id"} if sale_note else {"locacao_id"}):
        if not isinstance(payload[key], str) or not payload[key].isdigit() or len(payload[key]) > 18:
            raise ValidationError("Identificador invalido.")
    statuses = {value for value, _ in EntregaRotaItem.STATUS_CHOICES} - {"cancelada"} if sale_note else ACTIVE_STATUSES
    if payload["tarefa_status"] not in statuses:
        raise ValidationError("Estado de tarefa invalido.")
    note = payload["observacao"]
    if not isinstance(note, str) or not note.strip() or len(note) > 2000:
        raise ValidationError("Informe uma observacao de ate 2000 caracteres.")
    if data["payload_hash"] != command_hash(command):
        raise ValidationError("Hash do comando invalido.")
    return command


@transaction.atomic
def process_operation(command, user):
    digest = command_hash(command)
    # get_or_create uses the database UNIQUE constraint to arbitrate concurrent inserts.
    operation, created = OperacaoSincronizacao.objects.get_or_create(
        operation_id=command["operation_id"],
        defaults={
            "device_id": command["device_id"], "actor": user,
            "environment_id": command["environment_id"], "type": command["type"],
            "schema_version": command["schema_version"], "aggregate_id": command["aggregate_id"],
            "payload": command["payload"], "payload_hash": digest, "comando": command,
            "status": "enviando",
        },
    )
    if not created:
        operation = OperacaoSincronizacao.objects.select_for_update().get(pk=operation.pk)
        if operation.actor_id != user.pk or operation.payload_hash != digest or operation.comando != command:
            return {"operation_id": command["operation_id"], "status": "conflito", "hash": digest,
                    "code": "uuid_comando_divergente", "erro": "UUID ja utilizado com outro conteudo."}, 409
        return operation.resultado, 409 if operation.status == "conflito" else 200

    if command["type"] == SALE_OPERATION_TYPE:
        return _process_sale_creation(operation, command, user, digest)

    if command["type"] == "observacao_entrega_venda":
        return _process_sale_note(operation, command, user, digest)

    # Same lock order as rental registration routines: task, then rental.
    task = TarefaOperacionalLocacao.objects.select_for_update().filter(pk=int(command["aggregate_id"])).first()
    rental = Locacao.objects.select_for_update().filter(pk=task.locacao_id).first() if task else None
    payload = command["payload"]
    compatible = bool(task and rental and str(task.locacao_id) == payload["locacao_id"]
                      and task.status == payload["tarefa_status"] and task.status in ACTIVE_STATUSES
                      and rental.status not in {"cancelada", "devolvida", "devolvida_com_avaria"})
    now = timezone.now()
    result = {"operation_id": command["operation_id"], "hash": digest, "status": "confirmada" if compatible else "conflito",
              "record_id": None, "completed_at": now.isoformat()}
    if compatible:
        event = EventoLocacao.objects.create(
            locacao=rental, tipo="observacao_offline",
            descricao=f"Tarefa #{task.pk}: {payload['observacao']}", responsavel=user.get_username()[:120],
        )
        operation.referencia = event
        result["record_id"] = event.pk
    else:
        result["erro"] = "Tarefa/locacao ausente ou alterada. Revisao necessaria; observacao preservada."
        operation.erro = result["erro"]
    operation.status = result["status"]
    operation.resultado = result
    operation.concluido_em = now
    operation.save(update_fields=["referencia", "status", "resultado", "concluido_em", "erro"])
    return result, 200 if compatible else 409


def _process_sale_creation(operation, command, user, digest):
    revision = command["payload"].get("revisao")
    revision_error = None
    if revision:
        original = OperacaoSincronizacao.objects.select_for_update().filter(
            operation_id=revision["original_operation_id"]).first()
        if (not original or original.pk == operation.pk or original.actor_id != user.pk
                or original.environment_id != command["environment_id"]
                or original.type != SALE_OPERATION_TYPE
                or original.payload_hash != revision["original_hash"]
                or command_hash(original.comando) != original.payload_hash):
            revision_error = "Origem da revisao incompatível. Operacao original preservada."
        elif EncerramentoVendaOffline.objects.filter(original=original).exists():
            revision_error = "Original encerrada administrativamente sem venda. Nova revisao bloqueada."
        elif not commercial_conflict(original):
            revision_error = "Origem nao esta em conflito comercial. Consulte a venda oficial antes de prosseguir."
        elif RevisaoVendaOffline.objects.filter(original=original).exists():
            revision_error = "Esta operacao ja possui uma substituta. Consulte a revisao existente."
        elif any(command["payload"].get(field) != original.payload.get(field)
                 for field in ("data_venda", "operador")):
            revision_error = "Data e operador originais nao podem ser alterados nesta revisao."
        else:
            RevisaoVendaOffline.objects.create(
                original=original, substituta=operation, actor=user,
                environment_id=command["environment_id"], original_hash=original.payload_hash,
                motivo_original=original.resultado["erro"],
                revisada_em=parse_datetime(revision["revisada_em"]),
            )
    try:
        # Roll back all sale effects before persisting a definitive conflict.
        # The outer process_operation transaction also covers receipt completion.
        with transaction.atomic():
            if revision_error:
                raise ErroGravarVenda(revision_error)
            sale = execute_sale(command["payload"], user).venda
    except ErroGravarVenda as exc:
        sale = None
        error = exc.mensagem
    now = timezone.now()
    result = {"operation_id": command["operation_id"], "hash": digest,
              "status": "confirmada" if sale else "conflito",
              "record_id": sale.pk if sale else None, "completed_at": now.isoformat()}
    if sale is None:
        result["erro"] = error
        if revision_error:
            result["code"] = "revisao_origem_bloqueada"
            result["conflict_kind"] = "tecnico"
        operation.erro = error
    operation.status, operation.resultado, operation.concluido_em = result["status"], result, now
    operation.save(update_fields=["status", "resultado", "concluido_em", "erro"])
    return result, 200 if sale else 409


def commercial_conflict(operation):
    receipt = operation.resultado
    completed = parse_datetime(receipt.get("completed_at", "")) if isinstance(receipt, dict) and isinstance(receipt.get("completed_at"), str) else None
    return (operation.type == SALE_OPERATION_TYPE and operation.status == "conflito"
            and isinstance(receipt, dict) and receipt.get("status") == "conflito"
            and receipt.get("operation_id") == str(operation.operation_id)
            and receipt.get("hash") == operation.payload_hash and receipt.get("record_id") is None
            and isinstance(receipt.get("erro"), str) and bool(receipt["erro"].strip())
            and isinstance(receipt.get("completed_at"), str)
            and completed is not None and timezone.is_aware(completed)
            and receipt.get("conflict_kind", "comercial") == "comercial"
            and not receipt.get("code"))


def revision_chain(operation):
    """Read-only lineage, scoped and hash-checked before returning receipts."""
    chain, seen = [], {operation.pk}
    while True:
        relation = RevisaoVendaOffline.objects.select_related("substituta").filter(original=operation).first()
        if not relation:
            return chain
        child = relation.substituta
        if (child.pk in seen or child.actor_id != operation.actor_id
                or relation.actor_id != operation.actor_id or relation.environment_id != operation.environment_id
                or child.environment_id != operation.environment_id or child.type != SALE_OPERATION_TYPE
                or relation.original_hash != operation.payload_hash
                or command_hash(child.comando) != child.payload_hash
                or not isinstance(child.resultado, dict)
                or child.resultado.get("status") != child.status or child.status not in {"confirmada", "conflito"}
                or child.resultado.get("operation_id") != str(child.operation_id)
                or child.resultado.get("hash") != child.payload_hash):
            raise ValidationError("Cadeia de revisao incompatível.")
        chain.append({"original_operation_id": str(operation.operation_id),
                      "replacement_operation_id": str(child.operation_id),
                      "original_hash": relation.original_hash, "relacao": relation.relacao,
                      "actor_id": str(relation.actor_id), "environment_id": relation.environment_id,
                      "revisada_em": relation.revisada_em.isoformat(),
                      "registrado_em": relation.registrado_em.isoformat(),
                      "motivo_original": relation.motivo_original,
                      "hash": child.payload_hash, "receipt": child.resultado})
        seen.add(child.pk)
        operation = child


def _process_sale_note(operation, command, user, digest):
    item = EntregaRotaItem.objects.select_for_update().select_related("venda").filter(pk=int(command["aggregate_id"])).first()
    payload = command["payload"]
    compatible = bool(item and str(item.rota_id) == payload["rota_id"]
                      and str(item.venda_id) == payload["venda_id"] and item.status == payload["tarefa_status"]
                      and item.status != "cancelada" and not item.venda.cancelada)
    now = timezone.now()
    result = {"operation_id": command["operation_id"], "hash": digest,
              "status": "confirmada" if compatible else "conflito", "record_id": None, "completed_at": now.isoformat()}
    if compatible:
        event = EventoVenda.objects.create(venda=item.venda, tipo_evento="observacao_offline",
            descricao=f"Rota #{item.rota_id}, bloco #{item.pk}: {payload['observacao']}",
            canal="offline", usuario=user.get_username()[:120])
        result["record_id"] = event.pk
    else:
        result["erro"] = "Entrega/venda ausente ou alterada. Observacao preservada para revisao."
        operation.erro = result["erro"]
    operation.status, operation.resultado, operation.concluido_em = result["status"], result, now
    operation.save(update_fields=["status", "resultado", "concluido_em", "erro"])
    return result, 200 if compatible else 409
