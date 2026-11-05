"""Load the existing receiving form in edit mode; no alternate modal."""
from uuid import uuid4

from django.conf import settings
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from django.views.decorators.http import require_http_methods

from estoque.documentos_horarios import FUSO_DOCUMENTOS
from .forms import CorrecaoPagamentoLocacaoForm
from .models import PagamentoLocacao
from .payment_corrections import (correct_direct_payment, correction_receipt_url,
                                  require_actor, validate_context, version)

SALT = 'locacoes.correcao-pagamento-direto.v1'


@never_cache
@ensure_csrf_cookie
@csrf_protect
@require_http_methods(['GET', 'POST'])
def edit(request, pk, pagamento_pk):
    try:
        require_actor(request.user)
    except PermissionDenied as exc:
        return JsonResponse({'erro': str(exc)}, status=403 if request.user.is_authenticated else 401)
    payment = get_object_or_404(PagamentoLocacao.objects.select_related('locacao', 'movimento_financeiro'),
                               pk=pagamento_pk, locacao_id=pk)
    if payment.operacao_recebimento_cliente_id:
        return JsonResponse({'erro': 'Recebimento unificado: corrija a operação completa. Não é permitida edição individual.'}, status=403)
    with timezone.override(FUSO_DOCUMENTOS):
        if request.method == 'GET':
            try:
                validate_context(payment, timezone.localdate(payment.data_hora))
            except ValidationError as exc:
                return JsonResponse({'erro': '; '.join(exc.messages)}, status=409)
            form = CorrecaoPagamentoLocacaoForm(initial={
                'valor': payment.valor, 'data_hora': timezone.localtime(payment.data_hora).replace(tzinfo=None),
                'forma_pagamento': payment.forma_pagamento, 'observacao': payment.observacao,
            })
            employees = form.fields['responsavel'].queryset.filter(nome=payment.responsavel)
            if employees.count() == 1:
                form.initial['responsavel'] = employees.first().pk
            token = signing.dumps({'pagamento': payment.pk, 'locacao': pk, 'usuario': request.user.pk,
                                   'uuid': str(uuid4()), 'versao': version(payment),
                                   'data_original': payment.data_hora.isoformat()}, salt=SALT)
        else:
            token = request.POST.get('operacao_token', '')
            form = CorrecaoPagamentoLocacaoForm(request.POST)
            try:
                identity = signing.loads(token, salt=SALT)
                if (identity['usuario'] != request.user.pk or identity['locacao'] != pk
                        or identity['pagamento'] != payment.pk):
                    raise ValueError
                original_time = parse_datetime(identity['data_original'])
                if original_time is None or timezone.is_naive(original_time):
                    raise ValueError
            except (signing.BadSignature, KeyError, ValueError, TypeError):
                return JsonResponse({'erro': 'A identificação da correção é inválida. Reabra o histórico.'}, status=400)
            if form.is_valid():
                data = form.cleaned_data
                informed_time = data['data_hora']
                # Preserve seconds of the originally loaded timestamp if the
                # user did not change the existing minute-resolution widget.
                if request.POST.get('data_hora') == timezone.localtime(original_time).strftime('%Y-%m-%dT%H:%M'):
                    informed_time = original_time
                try:
                    correction = correct_direct_payment(
                        payment_id=payment.pk, rental_id=pk, actor=request.user,
                        operation_id=identity['uuid'], expected_version=identity['versao'],
                        valor=data['valor'], data_hora=informed_time, forma_pagamento=data['forma_pagamento'],
                        responsavel_id=data['responsavel'].pk, observacao=data['observacao'], motivo=data['motivo'],
                    )
                except ValidationError as exc:
                    form.add_error(None, '; '.join(exc.messages))
                except DatabaseError:
                    form.add_error(None, 'Não foi possível concluir com segurança. Tente novamente neste mesmo formulário.')
                else:
                    return JsonResponse({'ok': True, 'correcao': correction.pk, 'pagamento': payment.pk,
                                         'recibo_url': correction_receipt_url(correction)})
        return render(request, 'locacoes/includes/form_pagamento.html', {
            'form': form, 'locacao': payment.locacao, 'modo_consulta': True,
            'modo_correcao': True, 'pagamento_editado': payment, 'operacao_token': token,
            'csrf_cookie_name': settings.CSRF_COOKIE_NAME,
        }, status=422 if request.method == 'POST' else 200)
