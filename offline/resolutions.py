"""Administrative reservations, including commands never received by the server.

All authenticated sale sends and administrative writers take the same actor
lock before operation/parent locks. No commercial service is called here.
"""
import copy
import re
from uuid import UUID
from django.core.exceptions import ValidationError
from django.db import transaction, connection
from .models import (AbandonoVendaOffline, ControleResolucaoVendaOffline,
                     EncerramentoVendaOffline, OperacaoSincronizacao, RevisaoVendaOffline)


def lock_operation(operation_id):
    # Also arbitrate absent UUIDs between different principals. The actor lock
    # orders chain writers; this transaction lock protects the global UUID.
    if connection.vendor == 'postgresql':
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",['offline-sale:'+str(operation_id)])


def lock_sales(actor, operation_id=None):
    ControleResolucaoVendaOffline.objects.get_or_create(actor=actor)
    ControleResolucaoVendaOffline.objects.select_for_update().get(actor=actor)
    if operation_id is not None:
        lock_operation(operation_id)


def result(row):
    return {'status':'encerrada_sem_venda', 'kind':'abandono',
            'operation_id':str(row.operation_id), 'closure_id':str(row.closure_id),
            'hash':row.payload_hash, 'actor_id':str(row.actor_id),
            'environment_id':row.environment_id, 'original_device_id':row.comando['device_id'],
            'device_id':str(row.device_id), 'record_id':None, 'motivo':row.motivo,
            'closed_at':row.encerrado_em.isoformat(), 'original_receipt':row.evidencia['receipt']}


def compatible(command, row):
    from .services import command_hash
    return (row.actor_id == int(command['actor_id']) and row.environment_id == command['environment_id']
            and row.payload_hash == command_hash(command) and row.comando == command)


def children(command, user, environment):
    """Direct dependencies, including orphan records and absent reserved children.

    Requiring each direct child to have been closed after its own dependencies
    were checked establishes a bottom-up closure of the entire chain.
    """
    from .services import command_hash
    identifier=command['operation_id']
    rows=list(OperacaoSincronizacao.objects.filter(actor=user,environment_id=environment,
        payload__revisao__original_operation_id=identifier))
    linked=RevisaoVendaOffline.objects.filter(original__operation_id=identifier).select_related('substituta')
    known={str(row.operation_id):row for row in rows}
    for relation in linked:
        child=relation.substituta
        if relation.actor_id != user.pk or relation.environment_id != environment:
            raise ValidationError('Vínculo incompatível. Operação preservada.')
        known[str(child.operation_id)]=child
    unresolved=[]
    for child in known.values():
        revision=child.comando.get('payload',{}).get('revisao',{})
        if (child.actor_id != user.pk or child.environment_id != environment or child.type != 'criar_venda'
                or child.payload_hash != command_hash(child.comando) or child.payload != child.comando.get('payload')
                or revision.get('original_operation_id') != identifier
                or revision.get('original_hash') != command_hash(command)):
            raise ValidationError('Cadeia de vendas incompatível. Histórico preservado.')
        closed=AbandonoVendaOffline.objects.filter(operation_id=child.operation_id).first()
        if closed:
            if not compatible(child.comando,closed):
                raise ValidationError('Encerramento da substituta incompatível.')
        elif not EncerramentoVendaOffline.objects.filter(original=child).exists():
            unresolved.append({'command':child.comando,'payload_hash':child.payload_hash})
    return unresolved


def inspect(operation_id, expected, user, environment):
    from .services import command_hash
    if expected.get('actor_id') != str(user.pk) or expected.get('environment_id') != environment:
        raise ValidationError('Venda pertence a outra conta ou ambiente.')
    if (not isinstance(expected.get('hash'),str) or not re.fullmatch('[0-9a-f]{64}',expected['hash'])
            or str(UUID(expected.get('original_device_id',''))) != expected.get('original_device_id')):
        raise ValidationError('Identidade da consulta inválida.')
    closed=AbandonoVendaOffline.objects.filter(operation_id=operation_id).first()
    op=OperacaoSincronizacao.objects.filter(operation_id=operation_id).first()
    command=closed.comando if closed else op.comando if op else None
    if command and (command.get('actor_id') != str(user.pk) or command.get('environment_id') != environment
            or command.get('device_id') != expected.get('original_device_id')
            or command_hash(command) != expected.get('hash')):
        raise ValidationError('Não foi possível conferir a identidade da venda.')
    base={'operation_id':str(operation_id),'hash':expected.get('hash')}
    if closed and (closed.actor_id != user.pk or closed.environment_id != environment
            or closed.payload_hash != expected.get('hash')):
        raise ValidationError('Encerramento incompatível com esta conta.')
    if op and (op.actor_id != user.pk or op.environment_id != environment or op.type != 'criar_venda'
            or op.payload_hash != expected.get('hash') or op.comando != command
            or op.payload != command.get('payload')):
        raise ValidationError('Venda incompatível com a consulta.')
    if closed:
        if op and isinstance(op.resultado,dict) and op.resultado.get('record_id') is not None:
            return {**base,'lookup':'oficial','record_id':op.resultado['record_id']}
        return {**base,'lookup':'encontrada','closure':result(closed)}
    if op:
        if op.actor_id != user.pk or op.environment_id != environment or op.type != 'criar_venda':
            raise ValidationError('Venda incompatível.')
        receipt=op.resultado
        if (not isinstance(receipt,dict) or receipt.get('operation_id') != str(operation_id)
                or receipt.get('hash') != expected.get('hash') or receipt.get('status') != op.status
                or op.status not in {'confirmada','conflito'}):
            return {**base,'lookup':'aguardando'}
        if receipt.get('record_id') is not None:
            return {**base,'lookup':'oficial','record_id':receipt['record_id']}
        old=EncerramentoVendaOffline.objects.filter(original=op).first()
        if old:
            from .closures import closure_result
            return {**base,'lookup':'encontrada','closure':closure_result(old)}
    return {**base,'lookup':'nao_encontrada'}


@transaction.atomic
def resolve(operation_id, data, user, environment):
    from .services import validate_command, command_hash, commercial_conflict
    if (not isinstance(data,dict) or set(data) != {'command','payload_hash','closure_id','device_id','motivo','confirm'}
            or data['confirm'] is not True or not isinstance(data['motivo'],str)
            or not data['motivo'].strip() or len(data['motivo']) > 2000):
        raise ValidationError('Confirme o encerramento e informe o motivo.')
    for key in ('closure_id','device_id'):
        if not isinstance(data[key],str) or str(UUID(data[key])) != data[key]:
            raise ValidationError('Solicitação incompatível.')
    command=validate_command({**data['command'],'payload_hash':data['payload_hash']},user,environment)
    if command['type'] != 'criar_venda' or command['operation_id'] != str(operation_id):
        raise ValidationError('Somente esta venda pode ser resolvida.')
    lock_sales(user,operation_id)
    expected={'actor_id':str(user.pk),'environment_id':environment,
              'original_device_id':command['device_id'],'hash':command_hash(command)}
    state=inspect(operation_id,expected,user,environment)
    if state['lookup'] in {'encontrada','oficial','aguardando'}:
        return state
    op=OperacaoSincronizacao.objects.filter(operation_id=operation_id).first()
    if op and (op.comando != command or op.payload != command['payload'] or not commercial_conflict(op)):
        raise ValidationError('Venda sem conflito comercial definitivo. Consulte o resultado antes de continuar.')
    revision=command['payload'].get('revisao')
    if revision:
        parent=OperacaoSincronizacao.objects.filter(operation_id=revision['original_operation_id']).first()
        if (not parent or parent.actor_id != user.pk or parent.environment_id != environment
                or parent.payload_hash != revision['original_hash'] or command_hash(parent.comando) != parent.payload_hash):
            raise ValidationError('Não foi possível conferir a venda original.')
    pending=children(command,user,environment)
    if pending:
        return {**state,'lookup':'dependencias','children':pending}
    row=AbandonoVendaOffline.objects.create(operation_id=operation_id,closure_id=data['closure_id'],actor=user,
        environment_id=environment,device_id=data['device_id'],payload_hash=command_hash(command),comando=copy.deepcopy(command),
        motivo=data['motivo'].strip(),evidencia={'receipt':copy.deepcopy(op.resultado) if op else None,
            'operation_existed':bool(op),'dependencies_checked':True})
    return {**state,'lookup':'encontrada','closure':result(row)}
