"""Audited corrections of direct payments; never a new receiving pipeline."""
import hashlib
import json
from copy import copy
from decimal import Decimal
from uuid import UUID

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from estoque.documentos_horarios import FUSO_DOCUMENTOS
from estoque.models import ContaFinanceira, FechamentoRotaRecebimento, Funcionario, MovimentoFinanceiro
from .models import CorrecaoPagamentoLocacao, EventoLocacao, Locacao, PagamentoLocacao


def authorized(user):
    return bool(user.is_authenticated and user.is_active and user.is_superuser)


def require_actor(user):
    if not authorized(user) or not get_user_model().objects.filter(
            pk=user.pk, is_active=True, is_superuser=True).exists():
        raise PermissionDenied('Sua conta não tem autorização para corrigir recebimentos.')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def scalar(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    return value.isoformat() if hasattr(value, 'isoformat') else str(value)


def snapshot(payment):
    """Complete original row, original movement, and preceding audit identity."""
    return {
        'pagamento': {f.attname: scalar(getattr(payment, f.attname)) for f in payment._meta.concrete_fields},
        'movimento': ({f.attname: scalar(getattr(payment.movimento_financeiro, f.attname))
                       for f in MovimentoFinanceiro._meta.concrete_fields}
                      if payment.movimento_financeiro_id else None),
        'ultima_correcao': str(payment.correcoes.order_by('-pk').values_list('operacao_uuid', flat=True).first() or ''),
    }


def version(payment):
    return digest(snapshot(payment))


def validate_context(payment, new_date):
    if connection.vendor != 'postgresql':
        raise ValidationError('A correção exige PostgreSQL para verificar os fechamentos com segurança.')
    if payment.operacao_recebimento_cliente_id:
        raise ValidationError('Recebimento unificado: corrija a operação completa. Não é permitida edição individual.')
    if payment.locacao.status == Locacao.STATUS_CANCELADA:
        raise ValidationError('Não é possível corrigir pagamento de locação cancelada por este fluxo.')
    if not payment.movimento_financeiro_id:
        raise ValidationError('Pagamento sem movimento financeiro verificável. A correção foi bloqueada.')
    old_date = timezone.localtime(payment.data_hora, FUSO_DOCUMENTOS).date()
    movement = payment.movimento_financeiro
    # Legacy timestamps may have been converted with a different timezone.
    # Include the stored accounting date as well; never infer a safe period.
    dates = {old_date, new_date, movement.data}
    if FechamentoRotaRecebimento.objects.filter(data_referencia__in=dates).exists():
        raise ValidationError('Existe fechamento na data original ou corrigida. Não é possível editar este recebimento.')
    if (movement.tipo != MovimentoFinanceiro.TIPO_ENTRADA or movement.origem != 'locacao'
            or movement.valor != payment.valor or movement.conta_destino_id
            or movement.compra_id or movement.pagamento_conta_pagar_id
            or movement.operador != payment.responsavel):
        raise ValidationError('O movimento financeiro não corresponde ao pagamento. A correção foi bloqueada.')
    expected = PagamentoLocacao.conta_financeira_para_forma(payment.forma_pagamento, criar=False)
    if not expected or expected.pk != movement.conta_id:
        raise ValidationError('Não foi possível verificar a conta original do recebimento.')


def freeze_receipt(payment, notice, corrected=False):
    from .views import _mensagem_recibo_whatsapp, _whatsapp_recibo_url
    if corrected:
        payment = copy(payment)
        payment.recibo_status = 'pendente'
        payment.recibo_enviado_em = payment.recibo_dispensado_em = None
        payment.recibo_enviado_por = payment.recibo_dispensado_por = ''
    return render_to_string('locacoes/recibo.html', {
        'pagamento': payment, 'locacao': payment.locacao,
        'mensagem_recibo': _mensagem_recibo_whatsapp(payment),
        'whatsapp_url': _whatsapp_recibo_url(payment), 'modo_publico': True,
        'aviso_correcao': notice,
    })


@transaction.atomic
def correct_direct_payment(*, payment_id, rental_id, actor, operation_id, expected_version,
                           valor, data_hora, forma_pagamento, responsavel_id, observacao, motivo):
    require_actor(actor)
    try:
        operation_id = UUID(str(operation_id))
        value = Decimal(valor)
        if not value.is_finite() or value != value.quantize(Decimal('0.01')) or value <= 0:
            raise ValueError
        value = value.quantize(Decimal('0.01'))
    except (ValueError, ArithmeticError, TypeError):
        raise ValidationError('Informe um valor positivo com até duas casas decimais e uma operação válida.')
    if not motivo.strip():
        raise ValidationError('Informe o motivo da correção.')
    if forma_pagamento not in dict(PagamentoLocacao.FORMA_CHOICES):
        raise ValidationError('Forma de pagamento inválida.')
    if data_hora is None or timezone.is_naive(data_hora):
        raise ValidationError('Informe a data e hora do recebimento com fuso válido.')
    request_hash = digest({
        'pagamento': payment_id, 'locacao': rental_id, 'usuario': actor.pk, 'versao': expected_version,
        'valor': str(value.quantize(Decimal('0.01'))), 'data_hora': data_hora.isoformat(),
        'forma': forma_pagamento, 'responsavel_id': str(responsavel_id),
        'observacao': observacao.strip(), 'motivo': motivo.strip(),
    })
    # Lock the closure table before checking for absent rows. This conflicts with
    # INSERT/UPDATE/DELETE by the existing closure flow without changing that flow.
    if connection.vendor != 'postgresql':
        raise ValidationError('A correção exige PostgreSQL para verificar os fechamentos com segurança.')
    with connection.cursor() as cursor:
        cursor.execute('SHOW transaction_isolation')
        if cursor.fetchone()[0] != 'read committed':
            raise ValidationError('Não foi possível verificar o isolamento para corrigir com segurança.')
        cursor.execute("SET LOCAL lock_timeout = '5s'")
        cursor.execute('LOCK TABLE estoque_fechamentorotarecebimento IN SHARE MODE')
    rental = Locacao.objects.select_for_update().get(pk=rental_id)
    payment = PagamentoLocacao.objects.select_for_update().get(pk=payment_id, locacao=rental)
    previous = CorrecaoPagamentoLocacao.objects.filter(operacao_uuid=operation_id).first()
    if previous:
        if previous.pagamento_id != payment.pk or previous.requisicao_hash != request_hash:
            raise ValidationError('Esta operação já foi utilizada com outros dados.')
        return previous
    if payment.operacao_recebimento_cliente_id:
        raise ValidationError('Recebimento unificado: corrija a operação completa. Não é permitida edição individual.')
    employee = Funcionario.operadores_do_caixa().select_for_update().filter(pk=responsavel_id).first()
    if not employee:
        raise ValidationError('Selecione um funcionário ativo e autorizado a operar o caixa.')
    target = PagamentoLocacao.conta_financeira_para_forma(forma_pagamento, criar=False)
    if not target or not payment.movimento_financeiro_id:
        raise ValidationError('Não foi possível verificar as contas financeiras envolvidas.')
    movement = MovimentoFinanceiro.objects.select_for_update().get(pk=payment.movimento_financeiro_id)
    accounts = {c.pk: c for c in ContaFinanceira.objects.select_for_update().filter(
        pk__in={target.pk, movement.conta_id}).order_by('pk')}
    if len(accounts) != len({target.pk, movement.conta_id}) or any(not c.ativo for c in accounts.values()):
        raise ValidationError('Conta financeira indisponível para correção.')
    target_checked = PagamentoLocacao.conta_financeira_para_forma(forma_pagamento, criar=False)
    if not target_checked or target_checked.pk != target.pk:
        raise ValidationError('A conta financeira mudou. Reabra o recebimento.')
    payment.locacao = rental
    payment.movimento_financeiro = movement
    validate_context(payment, timezone.localtime(data_hora, FUSO_DOCUMENTOS).date())
    before = snapshot(payment)
    if digest(before) != expected_version:
        raise ValidationError('Este recebimento foi alterado. Reabra o histórico antes de corrigir.')
    rental.atualizar_financeiro()
    if value > rental.saldo_devedor + payment.valor:
        raise ValidationError('O valor corrigido não pode superar o saldo disponível da locação.')
    last_correction = payment.correcoes.order_by('-pk').first()
    original_html = (last_correction.recibo_corrigido_html if last_correction else
                     freeze_receipt(payment, 'Versão anterior preservada. Este recebimento possui correção auditada.'))
    reversal = MovimentoFinanceiro.objects.create(
        conta_id=movement.conta_id, tipo='saida', valor=movement.valor, data=movement.data,
        descricao=f'Estorno da correção {operation_id} do pagamento #{payment.pk}',
        operador=actor.get_username(), origem='locacao_correcao_estorno',
    )
    payment.valor = value
    payment.data_hora = data_hora
    payment.forma_pagamento = forma_pagamento
    payment.responsavel = employee.nome
    payment.observacao = observacao.strip()
    payment.movimento_financeiro = None
    payment.save(update_fields=['valor', 'data_hora', 'forma_pagamento', 'responsavel', 'observacao', 'movimento_financeiro'])
    # Use the existing mapping and financial-entry creator. Only the correction
    # uses Belém explicitly; new receipts keep their current behavior.
    with timezone.override(FUSO_DOCUMENTOS):
        new_movement = payment.criar_movimento_financeiro(conta_verificada=accounts[target.pk])
        rental.atualizar_financeiro()
        after = snapshot(payment)
        corrected_html = freeze_receipt(payment, 'Recibo corrigido. Consulte o histórico interno para a justificativa e autoria.', corrected=True)
    correction = CorrecaoPagamentoLocacao.objects.create(
        pagamento=payment, operacao_uuid=operation_id, versao_anterior=expected_version,
        requisicao_hash=request_hash, antes=before, depois=after, motivo=motivo.strip(),
        usuario=actor, usuario_nome=actor.get_username(), movimento_original=movement,
        estorno=reversal, movimento_novo=new_movement,
        recibo_anterior_html=original_html, recibo_corrigido_html=corrected_html,
    )
    EventoLocacao.objects.create(
        locacao=rental, tipo='pagamento_corrigido', responsavel=actor.get_username(),
        descricao=f'Pagamento #{payment.pk} corrigido. Auditoria #{correction.pk}; operação {operation_id}. '
                  f'Antes: R$ {before["pagamento"]["valor"]}; depois: R$ {value:.2f}. Motivo: {motivo.strip()}',
    )
    return correction


def correction_receipt_url(correction):
    return reverse('locacoes:recibo_pagamento', args=[correction.pagamento_id]) + '?versao=' + str(correction.operacao_uuid)
