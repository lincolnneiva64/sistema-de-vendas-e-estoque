import hashlib
import json
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from locacoes.models import EventoLocacao, Locacao, TarefaOperacionalLocacao
from estoque.models import EntregaRotaItem, EventoVenda
from .models import OperacaoSincronizacao

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
    if command["type"] not in {OPERATION_TYPE, "observacao_entrega_venda"} or type(command["schema_version"]) is not int or command["schema_version"] != 1:
        raise ValidationError("Protocolo de operacao incompativel.")
    if not isinstance(command["aggregate_id"], str) or not command["aggregate_id"].isdigit() or len(command["aggregate_id"]) > 18:
        raise ValidationError("Tarefa invalida.")
    if type(command["sequence"]) is not int or not 0 < command["sequence"] <= 9007199254740991:
        raise ValidationError("Sequencia invalida.")
    created = parse_datetime(command["created_at"]) if isinstance(command["created_at"], str) else None
    if created is None or timezone.is_naive(created):
        raise ValidationError("Data invalida.")
    payload = command["payload"]
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
        if operation.actor_id != user.pk or operation.payload_hash != digest or operation.comando != command:
            return {"operation_id": command["operation_id"], "status": "conflito", "hash": digest, "erro": "UUID ja utilizado com outro conteudo."}, 409
        return operation.resultado, 409 if operation.status == "conflito" else 200

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
