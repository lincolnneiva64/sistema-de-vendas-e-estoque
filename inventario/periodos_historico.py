"""Read-only date selection; never creates an opening balance or a conversion."""
from datetime import datetime, time, timedelta

from django.db.models import Min
from django.utils import timezone
from django.utils.dateparse import parse_date

from estoque.documentos_horarios import FUSO_DOCUMENTOS
from estoque.models import ItemVenda, ItemVendaRemovido, MovimentacaoEstoqueManual
from .models import MovimentoObservado


MODOS = ('ultima', 'tres_compras', 'personalizado', 'completo', 'compra')


def selecionar(params, referencia, selecionada, documentos, produto_id, limite=None):
    limite = limite if limite is not None else referencia
    modo = params.get('periodo') or ('compra' if params.get('compra', '').isdigit() else 'ultima')
    if modo not in MODOS:
        modo = 'ultima'
    fim = limite
    inicio = selecionada.compra.estoque_entrada_realizada_em if selecionada else referencia-timedelta(days=30)
    aviso = '' if selecionada else ('Sem compra recebida datada: últimos 30 dias antes da conferência e registros posteriores até o momento da consulta.'
        if limite != referencia else 'Sem compra recebida datada: últimos 30 dias até a referência da conferência.')
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
            inicio = timezone.make_aware(datetime.combine(primeiro, time.min), FUSO_DOCUMENTOS)
            fim = min(timezone.make_aware(datetime.combine(ultimo+timedelta(days=1), time.min), FUSO_DOCUMENTOS), limite)
            if inicio >= fim:
                raise ValueError
            aviso = ('Datas inclusivas no horário de Belém (PA); o fim é limitado ao momento da consulta.'
                     if limite != referencia else 'Datas inclusivas no horário de Belém (PA); o fim é limitado à referência da conferência.')
        except (ValueError, OverflowError, TypeError):
            inicio = fim = referencia
            erro = ('Informe um período válido, com início anterior ao momento da consulta e fim igual ou posterior ao início.'
                    if limite != referencia else 'Informe um período válido, com início anterior à referência da conferência e fim igual ou posterior ao início.')
    elif modo == 'completo':
        candidatos = []
        fontes = ((documentos, 'compra__estoque_entrada_realizada_em'),
            (ItemVenda.objects.filter(produto_id=produto_id), 'venda__criado_em'),
            (ItemVenda.objects.filter(produto_id=produto_id), 'venda__estoque_devolvido_cancelamento_em'),
            (ItemVendaRemovido.objects.filter(produto_id=produto_id), 'criado_em'),
            (MovimentoObservado.objects.filter(produto_id=produto_id), 'registrado_em'),
            (MovimentacaoEstoqueManual.objects.filter(produto_id=produto_id), 'criado_em'))
        for qs, campo in fontes:
            valor = qs.filter(**{campo+'__lte': limite}).aggregate(inicio=Min(campo))['inicio']
            if valor:
                candidatos.append(valor)
        dia = ItemVenda.objects.filter(produto_id=produto_id,
            venda__data_venda__lte=timezone.localtime(limite, FUSO_DOCUMENTOS).date()).aggregate(
                inicio=Min('venda__data_venda'))['inicio']
        if dia:
            candidatos.append(timezone.make_aware(datetime.combine(dia, time.min), FUSO_DOCUMENTOS))
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
