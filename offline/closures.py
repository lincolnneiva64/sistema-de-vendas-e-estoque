"""Global administrative closure, serialized with revision creation on the parent."""
import copy
import re
from uuid import UUID
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from .models import OperacaoSincronizacao, RevisaoVendaOffline, EncerramentoVendaOffline
from .services import commercial_conflict, command_hash


def closure_result(closure):
    op = closure.original
    return {'status':'encerrada_sem_venda', 'closure_id':str(closure.closure_id),
            'operation_id':str(op.operation_id), 'hash':closure.original_hash,
            'actor_id':str(closure.actor_id), 'environment_id':closure.environment_id,
            'original_device_id':str(op.device_id), 'device_id':str(closure.device_id),
            'motivo':closure.motivo, 'closed_at':closure.encerrado_em.isoformat(),
            'record_id':None, 'original_receipt':closure.evidencia['receipt']}


def checked_original(operation_id, data, user, environment, lock=False):
    if data.get('actor_id') != str(user.pk) or data.get('environment_id') != environment:
        raise ValidationError('Identidade ou ambiente incompatível.')
    query = OperacaoSincronizacao.objects.select_for_update() if lock else OperacaoSincronizacao.objects
    op = query.filter(operation_id=operation_id).first()
    if (not op or op.actor_id != user.pk or op.environment_id != environment
            or op.type != 'criar_venda' or str(op.device_id) != data.get('original_device_id')
            or op.payload_hash != data.get('hash') or command_hash(op.comando) != op.payload_hash
            or op.payload != op.comando.get('payload')
            or any(op.comando.get(k) != v for k,v in {'operation_id':str(op.operation_id),
                'device_id':str(op.device_id),'actor_id':str(op.actor_id),
                'environment_id':op.environment_id,'type':'criar_venda','aggregate_id':str(op.operation_id)}.items())):
        raise ValidationError('Original ausente ou comando incompatível.')
    return op


@transaction.atomic
def close_conflict(operation_id, data, user, environment):
    required = {'closure_id','hash','actor_id','environment_id','original_device_id','device_id','motivo','confirm'}
    if not isinstance(data,dict) or set(data) != required or data['confirm'] is not True:
        raise ValidationError('Confirmação explícita obrigatória.')
    for key in ('closure_id','original_device_id','device_id'):
        if not isinstance(data[key],str) or str(UUID(data[key])) != data[key]:
            raise ValidationError('Identidade de encerramento inválida.')
    if not isinstance(data['motivo'],str) or not data['motivo'].strip() or len(data['motivo']) > 2000:
        raise ValidationError('Informe o motivo de até 2000 caracteres.')
    if not isinstance(data['hash'],str) or not re.fullmatch('[0-9a-f]{64}',data['hash']):
        raise ValidationError('Hash inválido.')
    op = checked_original(operation_id,data,user,environment,lock=True)
    existing = EncerramentoVendaOffline.objects.filter(original=op).first()
    if existing:
        if (str(existing.closure_id) != data['closure_id'] or existing.motivo != data['motivo'].strip()
                or str(existing.device_id) != data['device_id']):
            raise ValidationError('Original já encerrada por outra solicitação. Consulte o resultado oficial.')
        return closure_result(existing)
    if not commercial_conflict(op):
        raise ValidationError('Somente conflito comercial definitivo sem venda pode ser encerrado.')
    # Include orphan/legacy commands, not only committed lineage links. A new
    # uncommitted revision waiting on this parent will instead be rejected below
    # by _process_sale_creation after this transaction commits.
    if (op.payload.get('revisao')
            or RevisaoVendaOffline.objects.filter(Q(original=op) | Q(substituta=op)).exists()
            or OperacaoSincronizacao.objects.filter(actor=user,environment_id=environment,type='criar_venda',
                payload__revisao__original_operation_id=str(op.operation_id)).exists()):
        raise ValidationError('Existe revisão/substituta. Resolva sua situação antes de encerrar a original.')
    if EncerramentoVendaOffline.objects.filter(closure_id=data['closure_id']).exists():
        raise ValidationError('Identidade de encerramento já utilizada.')
    closure = EncerramentoVendaOffline.objects.create(original=op,closure_id=data['closure_id'],
        original_hash=op.payload_hash,actor=user,environment_id=environment,device_id=data['device_id'],
        motivo=data['motivo'].strip(),evidencia={'receipt':copy.deepcopy(op.resultado),
            'original_device_id':str(op.device_id), 'original_sequence':op.comando.get('sequence'),
            'original_hash':op.payload_hash,'revisions_checked':True,'confirmed_without_sale':True,'record_id':None})
    return closure_result(closure)
