"""Read-only date selection; never creates an opening balance or a conversion."""
from datetime import datetime, time, timedelta

from django.db.models import Min
from django.utils import timezone
from django.utils.dateparse import parse_date

from estoque.models import ItemVenda, ItemVendaRemovido, MovimentacaoEstoqueManual
from .models import MovimentoObservado


MODOS = ('ultima', 'tres_compras', 'personalizado', 'completo', 'compra')


def selecionar(params, referencia, selecionada, documentos, produto_id):
    modo = params.get('periodo') or ('compra' if params.get('compra', '').isdigit() else 'ultima')
    if modo not in MODOS:
        modo = 'ultima'
    fim = referencia
    inicio = selecionada.compra.estoque_entrada_realizada_em if selecionada else referencia-timedelta(days=30)
    aviso = '' if selecionada else 'Sem compra recebida datada: últimos 30 dias até a referência da conferência.'
    erro = ''
    if modo == 'tres_compras':
        ultimas = list(documentos[:3])
        if ultimas:
            inicio = ultimas[-1].compra.estoque_entrada_realizada_em
            aviso = f'Contexto ampliado: {len(ultimas)} compra(s) recebida(s) disponível(is).'
    elif modo == 'personalizado':
        try:
            primeiro, ultimo = (parse_date(params.get(c, '')) for c in ('data_inicial', 'data_final'))
            if not primeiro or not ultimo or primeiro > ultimo:
                raise ValueError
            inicio = timezone.make_aware(datetime.combine(primeiro, time.min))
            fim = min(timezone.make_aware(datetime.combine(ultimo+timedelta(days=1), time.min)), referencia)
            if inicio >= fim:
                raise ValueError
            aviso = 'Datas inclusivas no fuso local; o fim é limitado à referência da conferência.'
        except (ValueError, OverflowError, TypeError):
            inicio = fim = referencia
            erro = 'Informe um período válido, com início anterior à referência da conferência e fim igual ou posterior ao início.'
    elif modo == 'completo':
        candidatos = []
        fontes = ((documentos, 'compra__estoque_entrada_realizada_em'),
            (ItemVenda.objects.filter(produto_id=produto_id), 'venda__criado_em'),
            (ItemVenda.objects.filter(produto_id=produto_id), 'venda__estoque_devolvido_cancelamento_em'),
            (ItemVendaRemovido.objects.filter(produto_id=produto_id), 'criado_em'),
            (MovimentoObservado.objects.filter(produto_id=produto_id), 'registrado_em'),
            (MovimentacaoEstoqueManual.objects.filter(produto_id=produto_id), 'criado_em'))
        for qs, campo in fontes:
            valor = qs.filter(**{campo+'__lte': referencia}).aggregate(inicio=Min(campo))['inicio']
            if valor:
                candidatos.append(valor)
        dia = ItemVenda.objects.filter(produto_id=produto_id,
            venda__data_venda__lte=timezone.localtime(referencia).date()).aggregate(
                inicio=Min('venda__data_venda'))['inicio']
        if dia:
            candidatos.append(timezone.make_aware(datetime.combine(dia, time.min)))
        if candidatos:
            inicio = min(candidatos)
        aviso = 'Desde o primeiro registro datado disponível; não comprova cobertura integral nem saldo inicial.'
    elif modo == 'compra' and selecionada and params.get('intervalo') == 'entre_compras':
        proxima = documentos.filter(compra__estoque_entrada_realizada_em__gt=inicio).order_by(
            'compra__estoque_entrada_realizada_em', 'compra_id').first()
        if proxima:
            fim = proxima.compra.estoque_entrada_realizada_em
    return dict(periodo=modo, inicio=inicio, fim=fim, aviso_periodo=aviso, erro_periodo=erro,
        data_inicial=params.get('data_inicial', ''), data_final=params.get('data_final', ''))
