"""Bounded, batched SELECTs for the visible page and its nearest sale anchors."""
from collections import defaultdict

from django.db.models import Count, F, OuterRef, Q, Subquery

from estoque.models import (AjusteItemVendaQuitada, EdicaoCompra, EventoVenda,
    ItemCompra, ItemVenda, ItemVendaRemovido, MovimentacaoEstoqueManual)
from .models import MovimentoObservado
from .evidencia_saldos import conflito_eventos, ponte, snapshot


LIMITE_EVIDENCIAS = 500


def analisar(produto_id, pagina, referencia):
    linhas = list(pagina)
    for linha in linhas:
        if linha['fonte'] == 'compra':
            linha['saldo_aviso'] = 'Compra sem saldo original gravado ou sem duas referências independentes compatíveis; reconstrução não disponível.'
    venda_ids = [r['pk'] for r in linhas if r['fonte'] == 'venda']
    compra_ids = [r['pk'] for r in linhas if r['fonte'] == 'compra']
    vendas = ItemVenda.objects.filter(produto_id=produto_id)

    def vizinho(campo, depois=False):
        filtro = {'venda__criado_em__gt' if depois else 'venda__criado_em__lt': OuterRef(campo)}
        return vendas.filter(**filtro).order_by('venda__criado_em' if depois else '-venda__criado_em',
                                                'pk' if depois else '-pk').values('pk')[:1]

    atuais = list(vendas.filter(pk__in=venda_ids).annotate(
        anterior_id=Subquery(vizinho('venda__criado_em'))).values('pk', 'anterior_id'))
    recebidas = list(ItemCompra.objects.filter(pk__in=compra_ids).annotate(
        anterior_id=Subquery(vizinho('compra__estoque_entrada_realizada_em')),
        posterior_id=Subquery(vizinho('compra__estoque_entrada_realizada_em', True)),
    ).values('pk', 'anterior_id', 'posterior_id'))
    ids = set(venda_ids)
    ids.update(r[k] for r in atuais + recebidas for k in ('anterior_id', 'posterior_id') if r.get(k))
    registros = {r['pk']: r for r in vendas.filter(pk__in=ids).values('pk',
        documento=F('venda_id'), instante=F('venda__criado_em'), cancelada=F('venda__cancelada'),
        devolvido=F('venda__estoque_devolvido_cancelamento'), quantidade_doc=F('quantidade'),
        unidade_doc=F('unidade'), antes=F('estoque_antes'), depois=F('estoque_depois'),
        movimentado=F('estoque_movimentado'), base=F('estoque_unidade_snapshot'))}
    if not registros:
        return
    for r in registros.values():
        r.update(quantidade=r.pop('quantidade_doc'), unidade=r.pop('unidade_doc'))
    documentos = {r['documento'] for r in registros.values()}
    ambiguos = set(vendas.filter(venda__criado_em__in=[r['instante'] for r in registros.values()])
        .values('venda__criado_em').annotate(total=Count('pk')).filter(total__gt=1)
        .values_list('venda__criado_em', flat=True))
    minimo = min(r['instante'] for r in registros.values())
    maximo = max(r['instante'] for r in registros.values())
    excedeu = False

    def limitado(qs):
        nonlocal excedeu
        resultado = list(qs[:LIMITE_EVIDENCIAS + 1])
        excedeu |= len(resultado) > LIMITE_EVIDENCIAS
        return resultado[:LIMITE_EVIDENCIAS]

    eventos = defaultdict(list)
    for e in limitado(EventoVenda.objects.filter(venda_id__in=documentos).order_by('pk').values(
            'venda_id', 'tipo_evento', 'descricao')):
        eventos[e['venda_id']].append(e)
    outros_nomes = defaultdict(set)
    nomes_alvo = defaultdict(set)
    itens = limitado(ItemVenda.objects.filter(venda_id__in=documentos).order_by('pk').values(
        'venda_id', 'produto_id', nome=F('produto__nome')))
    quantidade_alvo = defaultdict(int)
    for r in itens:
        nome = str(r['nome'] or '').strip().casefold()
        if r['produto_id'] == produto_id:
            nomes_alvo[r['venda_id']].add(nome)
            quantidade_alvo[r['venda_id']] += 1
        elif nome:
            outros_nomes[r['venda_id']].add(nome)
    remocoes = limitado(ItemVendaRemovido.objects.filter(venda_id__in=documentos).order_by('pk').values(
        'venda_id', 'produto_id', 'produto_nome_snapshot'))
    removidas_alvo = set()
    for r in remocoes:
        if r['produto_id'] == produto_id:
            removidas_alvo.add(r['venda_id'])
        else:
            outros_nomes[r['venda_id']].add(r['produto_nome_snapshot'].strip().casefold())
    ajustes = set(limitado(AjusteItemVendaQuitada.objects.filter(produto_id=produto_id,
        venda_id__in=documentos).order_by('pk').values_list('venda_id', flat=True)))
    validados, motivos = {}, {}
    for pk, r in registros.items():
        conflito = conflito_eventos(eventos[r['documento']],
            outros_nomes[r['documento']] - nomes_alvo[r['documento']])
        if (excedeu or quantidade_alvo[r['documento']] != 1 or r['documento'] in removidas_alvo
                or r['documento'] in ajustes or r['instante'] >= referencia or r['instante'] in ambiguos):
            conflito = 'Evidência incompleta, ajuste ou referência temporal incompatível; sequência interrompida.'
        validado, motivo = snapshot(r, conflito)
        if validado:
            validados[pk] = dict(validado, instante=r['instante'], documento=r['documento'])
        motivos[pk] = motivo

    compras = limitado(ItemCompra.objects.filter(produto_id=produto_id,
        compra__estoque_entrada_realizada=True,
        compra__estoque_entrada_realizada_em__gte=minimo,
        compra__estoque_entrada_realizada_em__lte=maximo,
    ).order_by('pk').values('pk', documento=F('compra_id'), instante=F('compra__estoque_entrada_realizada_em'),
                           quantidade_doc=F('quantidade'), unidade_doc=F('unidade'), cancelada=F('compra__cancelada')))
    editadas = set(EdicaoCompra.objects.filter(compra_id__in=[r['documento'] for r in compras]).values_list('compra_id', flat=True))
    for r in compras:
        r.update(quantidade=r.pop('quantidade_doc'), unidade=r.pop('unidade_doc'), editada=r['documento'] in editadas)
    sem_data = ItemCompra.objects.filter(produto_id=produto_id, compra__estoque_entrada_realizada=True,
        compra__estoque_entrada_realizada_em__isnull=True).exists()
    # Cancellation returns belong to their effective date, even for sales older
    # than either anchor. No immutable return quantity/unit exists on Venda.
    devolucoes = vendas.filter(Q(venda__estoque_devolvido_cancelamento=True) |
        Q(venda__estoque_devolvido_cancelamento_em__isnull=False))
    sem_data |= devolucoes.filter(venda__estoque_devolvido_cancelamento_em__isnull=True).exists()
    retornos = limitado(devolucoes.filter(
        venda__estoque_devolvido_cancelamento_em__gte=minimo,
        venda__estoque_devolvido_cancelamento_em__lte=maximo).order_by(
        'venda__estoque_devolvido_cancelamento_em').values_list(
        'venda__estoque_devolvido_cancelamento_em', flat=True).distinct())
    obstaculos = []
    for model, campo in ((MovimentoObservado, 'registrado_em'), (MovimentacaoEstoqueManual, 'criado_em'),
                         (ItemVendaRemovido, 'criado_em'), (AjusteItemVendaQuitada, 'criado_em')):
        obstaculos.extend(limitado(model.objects.filter(produto_id=produto_id,
            **{campo + '__gte': minimo, campo + '__lte': maximo}).order_by(campo).values_list(campo, flat=True)))
    # A return/reversal may occur well after the removal record itself.
    for campo in ('estoque_devolvido_em', 'revertido_em'):
        obstaculos.extend(limitado(ItemVendaRemovido.objects.filter(produto_id=produto_id,
            **{campo + '__gte': minimo, campo + '__lte': maximo}).order_by(campo).values_list(campo, flat=True)))

    def intervalo(anterior, posterior):
        if not anterior or not posterior:
            return [], True
        a, b = anterior['instante'], posterior['instante']
        trecho = [c for c in compras if a < c['instante'] < b]
        inseguro = (excedeu or sem_data or any(a < t < b for t in obstaculos)
                    or any(a <= t <= b for t in retornos))
        return trecho, inseguro

    reconstrucoes = {}
    for r in recebidas:
        anterior, posterior = validados.get(r['anterior_id']), validados.get(r['posterior_id'])
        trecho, inseguro = intervalo(anterior, posterior)
        resultado = ponte(anterior, posterior, trecho, inseguro)
        if resultado and trecho[0]['pk'] == r['pk'] and posterior['instante'] < referencia:
            reconstrucoes[r['pk']] = resultado
    anteriores = {r['pk']: r['anterior_id'] for r in atuais}
    for linha in linhas:
        if linha['fonte'] == 'compra' and linha['pk'] in reconstrucoes:
            e = reconstrucoes[linha['pk']]
            linha.update(saldo=e['depois'], saldo_antes=e['antes'], saldo_unidade=e['base'],
                saldo_tipo='Reconstruído e corroborado', saldo_evidencia=e['origem'], saldo_aviso=e['limite'])
        elif linha['fonte'] == 'venda':
            e = validados.get(linha['pk'])
            if not e:
                linha['saldo_aviso'] = motivos.get(linha['pk'], 'Snapshot não disponível.')
                linha['saldo_continuidade'] = 'Sequência interrompida'
                linha['saldo_pontual'] = True
                continue
            anterior = validados.get(anteriores.get(linha['pk']))
            trecho, inseguro = intervalo(anterior, e)
            continua = bool(anterior and not inseguro and not trecho
                and anterior['base'] == e['base'] and anterior['depois'] == e['antes'])
            corroborada = ponte(anterior, e, trecho, inseguro)
            linha.update(saldo=e['depois'], saldo_antes=e['antes'], saldo_unidade=e['base'],
                saldo_tipo='Snapshot registrado',
                saldo_continuidade=('Compatível com anterior' if continua else 'Trecho corroborado' if corroborada
                                    else 'Referência pontual · sequência interrompida'),
                saldo_pontual=not (continua or corroborada),
                saldo_evidencia=f"Item de venda #{linha['pk']}: saldo antes, movimento e saldo depois gravados em {e['base']}.",
                saldo_aviso=('Compatível com o snapshot anterior; não certifica movimentos não registrados.' if continua
                    else 'Trecho corroborado por compra e snapshots independentes.' if corroborada
                    else 'Referência pontual; continuidade anterior não comprovada. Nenhum saldo foi propagado através da lacuna.'))
