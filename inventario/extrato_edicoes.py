"""Read-only history projection. Current notes never create additional stock flow.

Only exact observer references identify a sale. Audit text corroborates quantities,
never event identity or timestamps. No current conversion factors are consulted.
"""
import re
from collections import defaultdict
from decimal import Decimal

from django.urls import reverse
from estoque.models import EventoVenda, ItemVenda, ItemVendaRemovido, Produto
from .evidencia_saldos import BENIGNOS, conflito_eventos, unidade
from .models import MovimentoObservado

LIMITE = 2000
EDICOES = {'quantidade_item_alterada', 'item_adicionado_na_nota', 'item_removido_da_nota'}
QUANTIDADE = re.compile(
    r'^Quantidade alterada na nota: (.+)\. De (\d+(?:\.\d+)?) (\S+) '
    r'para (\d+(?:\.\d+)?) (\S+)\.(?: |$)')


def etapas_comprovadas(itens, eventos, movimentos, removida, nome_unico, outros_nomes):
    """Require a complete, unambiguous same-unit documentary/observer sequence.

    In particular, current quantity + a return is NOT evidence of the original
    quantity. That quantity must be explicitly recorded in the first audit edit.
    Raw observer balances are retained, never reconstructed by accumulating rows.
    """
    if len(itens) != 1 or removida or not nome_unico:
        return None
    item = itens[0]
    base = unidade(item.estoque_unidade_snapshot)
    if (item.venda.cancelada or item.venda.estoque_devolvido_cancelamento or not base
            or unidade(item.unidade) != base):
        return None
    edicoes = []
    atribuida = False
    for evento in eventos:
        if evento.tipo_evento in BENIGNOS | {'nota_impressa'}:
            continue
        if (evento.tipo_evento == 'alteracao_separacao_atendida' and atribuida
                and evento.descricao.strip() in {
                    'Alteracoes apos separacao atendidas.',
                    'Ajustes da separacao aplicados pela edicao da venda.'}):
            continue
        match = QUANTIDADE.match(evento.descricao) if evento.tipo_evento == 'quantidade_item_alterada' else None
        if not match or match[1] != item.produto.nome:
            if not conflito_eventos([dict(tipo_evento=evento.tipo_evento,
                    descricao=evento.descricao, criado_em=evento.criado_em)], outros_nomes):
                atribuida = True
                continue
            return None
        if unidade(match[3]) != base or unidade(match[5]) != base:
            return None
        edicoes.append((Decimal(match[2]), Decimal(match[4])))
        atribuida = True
    if (not edicoes or len(movimentos) != len(edicoes) + 1
            or edicoes[0][0] <= 0 or movimentos[-1].depois != item.estoque_depois):
        return None
    quantidades = [edicoes[0][0]]
    atual = quantidades[0]
    for antes, depois in edicoes:
        if antes != atual or depois <= 0:
            return None
        quantidades.append(depois - antes)
        atual = depois
    if atual != item.quantidade or item.estoque_movimentado != atual:
        return None
    if item.estoque_antes is None or item.estoque_depois is None or item.estoque_antes - atual != item.estoque_depois:
        return None
    provas = {}
    for indice, (m, quantidade) in enumerate(zip(movimentos, quantidades)):
        if (m.antes is None or m.depois is None or min(m.antes, m.depois) < 0
                or quantidade == 0 or m.antes - m.depois != quantidade
                or m.origem != ('venda' if quantidade > 0 else 'devolucao_venda')
                or (indice and m.registrado_em < movimentos[indice - 1].registrado_em)):
            return None
        provas[m.pk] = (quantidade, base, indice == 0)
    return provas


def apresentar_edicoes(pagina, produto_id):
    ids = {r['documento'] for r in pagina if r['fonte'] in {'venda', 'remocao', 'cancelamento'}}
    for r in pagina:
        if r['fonte'] == 'digital':
            match = re.fullmatch(r'Venda #(\d+)', r['referencia'])
            if match and r['origem'] in {'venda', 'devolucao_venda'}:
                ids.add(int(match[1]))
    if not ids:
        return
    referencias = {f'Venda #{i}': i for i in ids}
    # Bounded bulk reads, independent of page size. Overflow disables proof,
    # never hides a movement (SQL pagination already preserved every observer).
    itens = list(ItemVenda.objects.filter(produto_id=produto_id, venda_id__in=ids)
        .select_related('venda', 'produto').order_by('pk')[:LIMITE + 1])
    eventos = list(EventoVenda.objects.filter(venda_id__in=ids).order_by('criado_em', 'pk')[:LIMITE + 1])
    movimentos = list(MovimentoObservado.objects.filter(produto_id=produto_id,
        referencia__in=referencias, origem__in=('venda', 'devolucao_venda'))
        .order_by('sequencia', 'pk')[:LIMITE + 1])
    outros_itens = list(ItemVenda.objects.filter(venda_id__in=ids).order_by('pk')
        .values('venda_id', 'produto_id', 'produto__nome')[:LIMITE + 1])
    remocoes = list(ItemVendaRemovido.objects.filter(venda_id__in=ids).order_by('pk')
        .values('venda_id', 'produto_id', 'produto_nome_snapshot')[:LIMITE + 1])
    removidas = {i['venda_id'] for i in remocoes if i['produto_id'] == produto_id}
    outros_nomes = defaultdict(set)
    nomes_alvo = {i.produto.nome.strip().casefold() for i in itens}
    for i in outros_itens + remocoes:
        nome = i.get('produto__nome') or i.get('produto_nome_snapshot') or ''
        if i['produto_id'] != produto_id and nome.strip().casefold() not in nomes_alvo:
            outros_nomes[i['venda_id']].add(nome.strip().casefold())
    por_item, por_evento, por_movimento = defaultdict(list), defaultdict(list), defaultdict(list)
    for item in itens:
        por_item[item.venda_id].append(item)
    for evento in eventos:
        por_evento[evento.venda_id].append(evento)
    for movimento in movimentos:
        por_movimento[referencias[movimento.referencia]].append(movimento)
    truncado = any(len(lista) > LIMITE for lista in (itens, eventos, movimentos, outros_itens, remocoes))
    nome_unico = bool(itens) and Produto.objects.filter(nome=itens[0].produto.nome).count() == 1
    provas, editadas = {}, set()
    for venda_id in ids:
        docs, obs, evs = por_item[venda_id], por_movimento[venda_id], por_evento[venda_id]
        nomes = {i.produto.nome for i in docs}
        edicao_alvo = any(e.tipo_evento in EDICOES and any(
            e.descricao.startswith(prefixo + nome + sufixo)
            for nome in nomes for prefixo, sufixo in (
                ('Quantidade alterada na nota: ', '. De '),
                ('Item adicionado na nota: ', ', quantidade '),
                ('Item removido da nota: ', ', quantidade '))) for e in evs)
        # Any edit, duplicate SKU, removal, return or mismatched observer makes
        # a current note informational. No guesses about the original quantity.
        diferente = bool(obs) and (len(docs) != 1 or len(obs) != 1 or
            docs[0].estoque_antes != obs[0].antes or docs[0].estoque_depois != obs[0].depois)
        if (truncado or diferente or venda_id in removidas or len(docs) > 1
                or edicao_alvo
                or any(i.venda.cancelada or i.venda.estoque_devolvido_cancelamento for i in docs)):
            editadas.add(venda_id)
        if not truncado:
            provas.update(etapas_comprovadas(docs, evs, obs, venda_id in removidas,
                nome_unico, outros_nomes[venda_id]) or {})
    for r in pagina:
        if r['fonte'] == 'venda' and r['documento'] in editadas:
            docs = por_item[r['documento']]
            alteracoes = []
            for evento in por_evento[r['documento']]:
                match = QUANTIDADE.match(evento.descricao)
                if (evento.tipo_evento == 'quantidade_item_alterada' and match
                        and any(match[1] == i.produto.nome for i in docs)):
                    alteracoes.append(dict(instante=evento.criado_em, antes=Decimal(match[2]),
                        unidade_antes=match[3], depois=Decimal(match[4]), unidade_depois=match[5]))
            r.update(saida=None, entrada=None, saldo=None, saldo_contagem=None,
                documento_atual=True, registros_tecnicos=[],
                titulo=r['titulo'] + ' — Nota atual', edicoes_documentais=alteracoes[:50],
                edicoes_truncadas=truncado or len(alteracoes) > 50,
                fonte_texto='Nota atual — informação documental',
                nota='Quantidade atual da nota; não é outra baixa. Consulte os movimentos registrados nos respectivos horários.',
                saldo_aviso='O estado atual da nota não comprova o saldo na data da saída original.')
        elif r['fonte'] == 'digital':
            match = re.fullmatch(r'Venda #(\d+)', r['referencia'])
            if not match or int(match[1]) not in ids or r['origem'] not in {'venda', 'devolucao_venda'}:
                continue
            venda_id = int(match[1])
            titulo = 'Saída registrada' if r['origem'] == 'venda' else 'Devolução registrada'
            prova = provas.get(r['pk'])
            if prova:
                quantidade, base, original = prova
                titulo = 'Saída original' if original else 'Acréscimo por edição' if quantidade > 0 else 'Devolução por edição'
                r.update(saida=quantidade if quantidade > 0 else None,
                    entrada=-quantidade if quantidade < 0 else None, unidade=base,
                    saldo=r['depois'], saldo_antes=r['antes'], saldo_unidade=base,
                    saldo_tipo='Registro digital corroborado', saldo_pontual=True,
                    saldo_continuidade='Referência pontual; continuidade não certificada',
                    saldo_evidencia='Referência explícita à venda, sequência e quantidades documentadas na edição, na mesma unidade do snapshot. Sem conversão.',
                    saldo_aviso='Saldo deste registro; não recalculado pelas linhas do extrato.', unidade_comprovada=True)
            r.update(titulo=f'Venda #{venda_id} — {titulo}',
                url=reverse('estoque:venda_detalhe', args=[venda_id]))
