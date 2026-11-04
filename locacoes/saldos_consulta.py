"""Presentation of current balances; amounts remain owned by Locacao."""
from decimal import Decimal

from django.core.paginator import Paginator
from django.db.models import Case, CharField, Count, Q, Sum, Value, When

from .models import Locacao


CATEGORIAS = (('andamento', 'Entregues a receber'), ('atrasadas', 'Em atraso'),
              ('reservas', 'Reservas futuras'), ('total', 'Total em aberto'))


def saldos_em_aberto():
    return Locacao.objects.exclude(status=Locacao.STATUS_CANCELADA).filter(saldo_devedor__gt=0).annotate(
        categoria_saldo=Case(
            When(status__in=[Locacao.STATUS_RESERVADA, Locacao.STATUS_SAIU_PARA_ENTREGA], then=Value('reservas')),
            When(status__in=[Locacao.STATUS_DEVOLVIDA, Locacao.STATUS_DEVOLVIDA_COM_AVARIA], then=Value('atrasadas')),
            default=Value('andamento'), output_field=CharField(),
        ))


def indicadores_saldos():
    queryset = saldos_em_aberto()
    expressions = {}
    for key, _ in CATEGORIAS:
        condition = Q() if key == 'total' else Q(categoria_saldo=key)
        expressions[key] = Sum('saldo_devedor', filter=condition, default=Decimal('0.00'))
        expressions[key + '_quantidade'] = Count('pk', filter=condition)
    values = queryset.aggregate(**expressions)
    return [{'chave': key, 'rotulo': label, 'valor': values[key], 'quantidade': values[key + '_quantidade']}
            for key, label in CATEGORIAS]


def contexto_saldos(categoria='total', pagina=1):
    if categoria is None:
        return {'indicadores_saldos': indicadores_saldos(), 'categoria_saldo': 'total',
                'categoria_saldo_rotulo': dict(CATEGORIAS)['total'], 'mostrar_lista_saldos': False}
    if categoria not in dict(CATEGORIAS):
        categoria = 'total'
    queryset = saldos_em_aberto()
    if categoria != 'total':
        queryset = queryset.filter(categoria_saldo=categoria)
    queryset = queryset.select_related('cliente').only(
        'id', 'cliente__nome', 'pessoa_avulsa_nome', 'status', 'total', 'total_pago', 'saldo_devedor',
        'criado_em', 'data_entrega', 'data_prevista_devolucao', 'data_vencimento_saldo',
    ).order_by('criado_em', 'pk')
    return {'indicadores_saldos': indicadores_saldos(), 'categoria_saldo': categoria,
            'categoria_saldo_rotulo': dict(CATEGORIAS)[categoria],
            'mostrar_lista_saldos': True,
            'pagina_saldos': Paginator(queryset, 20).get_page(pagina)}
