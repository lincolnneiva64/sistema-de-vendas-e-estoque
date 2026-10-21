"""Conservative adjustment proposals. All stock writes use one guarded entry point."""
import hashlib
import json
from decimal import Decimal
from uuid import uuid4

from django.conf import settings
from django.core import signing
from django.db import connection, transaction
from django.db.models import OuterRef, Subquery

from estoque.models import Produto, MovimentacaoEstoqueManual
from estoque.services.estoque_manual import conferir_ou_ajustar_estoque
from . import services
from .selecao import itens_incluidos, rodadas_incluidas
from .models import (Inventario, ItemInventario, RodadaContagem, DecisaoAjuste,
                     PropostaAjuste, AplicacaoAjuste, EventoAjuste, MovimentoObservado,
                     ConfirmacaoContagem)

SALT = 'inventario-ajustes-v1'


def assinatura(dados):
    return hashlib.sha256(json.dumps(dados, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def token(dados):
    return signing.dumps(dados, salt=SALT, compress=True)


def ler_token(valor):
    if not isinstance(valor, str) or not valor or len(valor) > 100000:
        raise ValueError('Prévia ausente ou inválida. Prepare novamente os ajustes.')
    try:
        return signing.loads(valor, salt=SALT, max_age=1200)
    except signing.BadSignature:
        raise ValueError('Prévia inválida ou expirada. Prepare novamente os ajustes.')


def avaliar(rodada, produto=None, travar_documentos=False, *, _lote=None):
    """Read-only preview; equality of balances alone never establishes continuity."""
    produto = produto or rodada.item.produto
    item = rodada.item
    if _lote is not None:
        _lote.conferir(rodada, travar_documentos)
    resultado = dict(rodada=rodada, produto=produto, unidade=item.cadastro.get('base', ''),
                     esperado=rodada.esperado, contado=rodada.contado_base if rodada.ok_em else None,
                     atual=produto.quantidade, diferenca=None, resultante=None,
                     estado='Pendente de revisão', bloqueio='', evidencia=None)
    if rodada.ok_em and rodada.contado_base is not None and rodada.esperado is not None:
        resultado['diferenca'] = rodada.contado_base - rodada.esperado
    movimentos = (list(_lote.movimentos[item.produto_id]) if _lote is not None
        else list(services.movimentos_relevantes(rodada))) if rodada.ok_em else []
    resultado['movimentos'] = movimentos
    resultado['posteriores'] = [m for m in movimentos if m.sequencia > (rodada.sequencia_ok or 0)]
    try:
        # All conferences use the same evidence checks for preparation/approval.
        # Actual execution remains separately guarded in aplicar().
        if (item.pk in _lote.decisoes if _lote is not None else DecisaoAjuste.objects.filter(item=item).exists()):
            resultado['estado'] = 'Aplicado'
            return resultado
        if (not rodada.ok_em or rodada.esperado is None or rodada.contado_base is None
                or rodada.sequencia_ok is None or rodada.encerrada_em
                or (_lote.maior_numero.get(item.pk, 0) > rodada.numero if _lote is not None
                    else item.rodadas.filter(numero__gt=rodada.numero).exists())):
            raise ValueError('Confirmação física ausente, alterada ou substituída por recontagem.')
        confs = _lote.confirmacoes[rodada.pk] if _lote is not None else None
        confirmacao = (confs[-1] if confs else None) if _lote is not None else rodada.confirmacoes.order_by('-versao_entrada').first()
        if not confirmacao or any(getattr(confirmacao, campo) != getattr(rodada, campo)
                for campo in ('ok_em', 'esperado', 'contado_base', 'contado', 'sequencia_ok', 'embalagens', 'avulsos')):
            raise ValueError('OK sem confirmação original compatível; solicite recontagem.')
        from .conciliacao_documental import conciliar
        documentos = conciliar(rodada, movimentos, travar=travar_documentos, _lote=_lote)
        resultado['documentos'] = documentos
        sequencia = services.validar_cadeia(rodada, produto,
            documentos={d['observador'] for d in documentos}, _lote=_lote)
        diferenca = (rodada.contado_base - rodada.esperado).quantize(Decimal('.001'))
        resultante = produto.quantidade + diferenca
        resultado.update(diferenca=diferenca, resultante=resultante)
        if diferenca == 0:
            resultado.update(estado='Sem ajuste', bloqueio='Sem diferença a aplicar.')
            return resultado
        if not Decimal(0) <= resultante < Decimal('1000000000'):
            raise ValueError('Saldo proposto fora dos limites do estoque.')
        dados = dict(inventario=item.inventario_id, item=item.pk, produto=produto.pk,
            rodada=rodada.pk, versao=rodada.versao, confirmacao=confirmacao.pk,
            ok_em=rodada.ok_em.isoformat(), esperado=str(rodada.esperado), contado=str(rodada.contado_base),
            cadastro=item.cadastro, sequencia=sequencia, atual=str(produto.quantidade),
            documentos=documentos,
            diferenca=str(diferenca), resultante=str(resultante),
            sinais=list(_lote.sinais[item.pk]) if _lote is not None else list(item.rodadas.values_list('sinalizacoes__pk', 'sinalizacoes__evidencia').order_by('numero', 'sinalizacoes__pk')))
        resultado.update(evidencia=dados, assinatura=assinatura(dados))
        resultado['token'] = token(dict(tipo='preparacao', rodada=rodada.pk, assinatura=resultado['assinatura']))
        proposta = (_lote.propostas.get((rodada.pk, resultado['assinatura'])) if _lote is not None
                    else PropostaAjuste.objects.filter(rodada=rodada, assinatura=resultado['assinatura']).first())
        resultado['proposta'] = proposta
        if proposta:
            resultado['estado'] = proposta.get_estado_display()
    except ValueError as exc:
        resultado.update(estado='Bloqueado', bloqueio=str(exc))
    return resultado


def bloquear(inventario_id, rodada_ids):
    """Consistent lock order, including product locks held through commit."""
    Inventario.objects.select_for_update().get(pk=inventario_id)
    verificar_limite(inventario_id)
    referencias = list(rodadas_incluidas(inventario_id).filter(pk__in=rodada_ids).values('pk', 'item_id', 'item__produto_id'))
    if len(referencias) != len(set(rodada_ids)):
        raise ValueError('Proposta fora da conferência.')
    list(ItemInventario.objects.select_for_update().filter(pk__in=[r['item_id'] for r in referencias]).order_by('pk'))
    rodadas = list(RodadaContagem.objects.select_for_update(of=('self',)).select_related('item__produto', 'item__inventario')
        .filter(pk__in=rodada_ids).order_by('pk'))
    produtos = {p.pk: p for p in Produto.objects.select_for_update()
                .filter(pk__in=[r['item__produto_id'] for r in referencias]).order_by('pk')}
    return [(r, produtos[r.item.produto_id]) for r in rodadas]


def verificar_limite(inventario_id):
    if itens_incluidos(inventario_id).count() > 500:
        raise ValueError('Mais de 500 produtos: aprovação e aplicação bloqueadas. Divida a revisão em conferências menores.')


@transaction.atomic
def decidir(usuario, inventario_id, rodada_id, previa, justificativa, aprovar=True, confirmacao=False):
    services.permitir(usuario, 'aprovar_ajuste')
    if not confirmacao or not str(justificativa or '').strip() or len(str(justificativa)) > 4000:
        raise ValueError('Confirme a revisão física e informe evidências e justificativa.')
    dados = ler_token(previa)
    if dados.get('tipo') != 'preparacao' or dados.get('rodada') != rodada_id:
        raise ValueError('Prévia não pertence a esta rodada.')
    rodada, produto = bloquear(inventario_id, [rodada_id])[0]
    avaliacao = avaliar(rodada, produto, travar_documentos=True)
    if avaliacao['bloqueio'] or avaliacao.get('assinatura') != dados.get('assinatura'):
        raise ValueError(avaliacao['bloqueio'] or 'Dados alterados desde a preparação. Prepare novamente.')
    return _registrar_decisao(usuario, rodada, avaliacao, justificativa, aprovar)


def _registrar_decisao(usuario, rodada, avaliacao, justificativa, aprovar=True, *, _novas=None):
    """Persist evidence already fully validated under this transaction's locks.

    Only decidir()/atualizar() call this internal helper. Both enforce permission,
    signed preview and current evidence before reaching it. No cross-request cache.
    """
    existente = avaliacao.get('proposta')
    if existente:
        if existente.estado == 'aprovado' and not aprovar:
            existente.estado = 'rejeitado'
            existente.save(update_fields=['estado'])
            EventoAjuste.objects.create(proposta=existente, usuario=usuario, acao='rejeitado',
                                       dados=dict(justificativa=justificativa.strip()))
            return existente
        if existente.estado != ('aprovado' if aprovar else 'rejeitado'):
            raise ValueError('Decisão já registrada. Uma proposta rejeitada exige nova confirmação física.')
        return existente
    proposta = PropostaAjuste(rodada=rodada,
        confirmacao_id=avaliacao['evidencia']['confirmacao'], assinatura=avaliacao['assinatura'],
        evidencia=avaliacao['evidencia'], justificativa=justificativa.strip(),
        estado='aprovado' if aprovar else 'rejeitado', autorizado_por=usuario)
    if _novas is not None:
        _novas.append(proposta)
    else:
        proposta.save(force_insert=True)
        EventoAjuste.objects.create(proposta=proposta, usuario=usuario, acao=proposta.estado,
            dados=dict(evidencia=proposta.evidencia, justificativa=proposta.justificativa))
    return proposta


def previa_aplicacao(inventario_id, propostas):
    return token(dict(tipo='aplicacao', inventario=inventario_id,
                      propostas=sorted(str(p.pk) for p in propostas)))


def verificar_execucao():
    if not getattr(settings, 'INVENTARIO_AJUSTES_HABILITADOS', False):
        raise ValueError('Aplicação real desabilitada no servidor. Aprovar não altera estoque.')
    if connection.vendor != 'postgresql':
        raise ValueError('Aplicação exige PostgreSQL e bloqueios transacionais.')
    with connection.cursor() as cursor:
        cursor.execute("""SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal
            AND tgenabled IN ('O','A') AND (tgname, tgrelid) IN (
            ('inventario_aplicacao_imutavel', 'inventario_aplicacaoajuste'::regclass),
            ('inventario_evento_ajuste_imutavel', 'inventario_eventoajuste'::regclass),
            ('inventario_proposta_congelada', 'inventario_propostaajuste'::regclass))""")
        if cursor.fetchone()[0] != 3:
            raise ValueError('Proteções da auditoria indisponíveis. Aplicação bloqueada.')


@transaction.atomic
def aplicar(usuario, inventario_id, previa, confirmacao=False):
    services.permitir(usuario, 'aprovar_ajuste')
    verificar_execucao()  # Before any stock write, including retries.
    if not confirmacao:
        raise ValueError('Confirmação expressa de aplicação obrigatória.')
    dados = ler_token(previa)
    if dados.get('tipo') != 'aplicacao' or dados.get('inventario') != inventario_id or not dados.get('propostas'):
        raise ValueError('Prévia de aplicação inválida.')
    Inventario.objects.select_for_update().get(pk=inventario_id)
    verificar_limite(inventario_id)
    propostas = list(PropostaAjuste.objects.filter(pk__in=dados['propostas'],
        rodada__item__inventario_id=inventario_id).order_by('rodada_id'))
    if len(propostas) != len(dados['propostas']):
        raise ValueError('Proposta indisponível ou fora da conferência.')
    if all(p.estado == 'aplicado' for p in propostas):
        return list(AplicacaoAjuste.objects.filter(proposta__in=propostas).order_by('proposta__rodada_id'))
    if any(p.estado != 'aprovado' for p in propostas):
        raise ValueError('Lote alterado ou parcialmente executado anteriormente. Consulte o resultado e prepare nova prévia.')
    travadas = bloquear(inventario_id, [p.rodada_id for p in propostas])
    por_rodada = {p.rodada_id: p for p in propostas}
    avaliacoes = []
    from .contexto_ajustes import carregar
    with carregar([r for r, _ in travadas], travar=True) as lote:
        for rodada, produto in travadas:
            proposta = por_rodada[rodada.pk]
            atual = avaliar(rodada, produto, travar_documentos=True, _lote=lote)
            _validar_proposta(proposta, atual)
            avaliacoes.append((proposta, rodada, produto, atual))
    return _gravar_aplicacoes(usuario, inventario_id, avaliacoes)


def _validar_proposta(proposta, atual):
    if atual['bloqueio'] or atual.get('assinatura') != proposta.assinatura or assinatura(proposta.evidencia) != proposta.assinatura:
        raise ValueError(atual['bloqueio'] or 'Estoque, OK ou evidência alterados após aprovação. Nenhum ajuste do lote aplicado.')


def _gravar_aplicacoes(usuario, inventario_id, avaliacoes):
    """The existing stock/audit writer, shared by both authorized entry points.

    Callers hold inventory, round, item, product and documentary locks and have
    validated the complete lot in this same atomic transaction before writing.
    """
    if len(avaliacoes) > 1:
        return _gravar_lote(usuario, inventario_id, avaliacoes)
    lote = uuid4()
    resultados = []
    for proposta, rodada, produto, atual in avaliacoes:
        # The product remains locked; no other transaction can change it between
        # this read and the stock service's UPDATE. Always current + difference.
        resultado = conferir_ou_ajustar_estoque(produto.pk, MovimentacaoEstoqueManual.TIPO_AJUSTE,
            novo_estoque=atual['resultante'], motivo=proposta.justificativa,
            observacao=f'Conferência {inventario_id}; proposta {proposta.pk}; lote {lote}', usuario=usuario)
        if not MovimentoObservado.objects.filter(produto=produto,
                sequencia=atual['evidencia']['sequencia'] + 1,
                antes=resultado['estoque_antes'], depois=resultado['estoque_depois'],
                origem='ajuste_manual', referencia=f"Movimentação manual #{resultado['historico'].pk}").exists():
            raise ValueError('Movimento do observador não confirmado. Operação integralmente revertida.')
        decisao = DecisaoAjuste.objects.create(item=rodada.item, rodada=rodada,
            movimentacao=resultado['historico'], diferenca=atual['diferenca'],
            justificativa=proposta.justificativa, aprovado_por=usuario)
        registro = AplicacaoAjuste.objects.create(proposta=proposta, decisao=decisao, usuario=usuario,
            lote=lote, antes=resultado['estoque_antes'], diferenca=resultado['diferenca'],
            depois=resultado['estoque_depois'], unidade=atual['unidade'],
            evidencia=dict(preparacao=proposta.evidencia, autorizado_por=proposta.autorizado_por_id,
                           justificativa=proposta.justificativa))
        proposta.estado = 'aplicado'
        proposta.save(update_fields=['estado'])
        EventoAjuste.objects.create(proposta=proposta, usuario=usuario, acao='aplicado',
            dados=dict(operacao=str(registro.pk), lote=str(lote), antes=str(registro.antes),
                       diferenca=str(registro.diferenca), depois=str(registro.depois)))
        services.evento(rodada.item.inventario, 'ajuste_aplicado', usuario, rodada=rodada,
            decisao=decisao.pk, operacao=str(registro.pk), lote=str(lote), diferenca=str(registro.diferenca))
        resultados.append(registro)
    return resultados


def _gravar_lote(usuario, inventario_id, avaliacoes):
    from estoque.services.estoque_manual_lote import gravar
    from .models import EventoInventario
    lote = uuid4()
    movimentos = gravar(usuario, [dict(produto=p, antes=a['atual'], depois=a['resultante'],
        sequencia=a['evidencia']['sequencia'], motivo=pr.justificativa,
        observacao=f'Conferência {inventario_id}; proposta {pr.pk}; lote {lote}')
        for pr, r, p, a in avaliacoes])
    decisoes = [DecisaoAjuste(item=r.item, rodada=r, movimentacao=m['historico'],
        diferenca=a['diferenca'], justificativa=pr.justificativa, aprovado_por=usuario)
        for (pr, r, p, a), m in zip(avaliacoes, movimentos)]
    DecisaoAjuste.objects.bulk_create(decisoes)
    registros = [AplicacaoAjuste(proposta=pr, decisao=d, usuario=usuario, lote=lote,
        antes=m['estoque_antes'], diferenca=m['diferenca'], depois=m['estoque_depois'],
        unidade=a['unidade'], evidencia=dict(preparacao=pr.evidencia,
        autorizado_por=pr.autorizado_por_id, justificativa=pr.justificativa))
        for (pr, r, p, a), m, d in zip(avaliacoes, movimentos, decisoes)]
    AplicacaoAjuste.objects.bulk_create(registros)
    ids = [pr.pk for pr, r, p, a in avaliacoes]
    if PropostaAjuste.objects.filter(pk__in=ids, estado='aprovado').update(estado='aplicado') != len(ids):
        raise ValueError('Transição incompleta das propostas.')
    for proposta, _, _, _ in avaliacoes:
        proposta.estado = 'aplicado'
    EventoAjuste.objects.bulk_create([EventoAjuste(proposta=pr, usuario=usuario, acao='aplicado',
        dados=dict(operacao=str(reg.pk), lote=str(lote), antes=str(reg.antes),
        diferenca=str(reg.diferenca), depois=str(reg.depois)))
        for (pr, r, p, a), reg in zip(avaliacoes, registros)])
    EventoInventario.objects.bulk_create([EventoInventario(inventario_id=inventario_id,
        acao='ajuste_aplicado', usuario=usuario, rodada=r, dados=dict(decisao=reg.decisao_id,
        operacao=str(reg.pk), lote=str(lote), diferenca=str(reg.diferenca)))
        for (pr, r, p, a), reg in zip(avaliacoes, registros)])
    return registros


def previa_atualizacao(inventario_id, linhas):
    return token(dict(tipo='atualizacao', inventario=inventario_id,
        rodadas=[dict(rodada=l['rodada'].pk, assinatura=l['assinatura']) for l in linhas]))


@transaction.atomic
def atualizar(usuario, inventario_id, previa, confirmacao=False, observacao=''):
    """One explicit authorization; approval and application commit together."""
    services.permitir(usuario, 'aprovar_ajuste')
    verificar_execucao()
    if not confirmacao:
        raise ValueError('Confirmação expressa de atualização obrigatória.')
    observacao = str(observacao or '').strip()
    if len(observacao) > 4000:
        raise ValueError('Observação deve ter até 4000 caracteres.')
    dados = ler_token(previa)
    entradas = dados.get('rodadas')
    if (dados.get('tipo') != 'atualizacao' or dados.get('inventario') != inventario_id
            or not isinstance(entradas, list) or not 1 <= len(entradas) <= 500
            or any(not isinstance(e, dict) or not isinstance(e.get('rodada'), int)
                   or not isinstance(e.get('assinatura'), str) for e in entradas)
            or len({e['rodada'] for e in entradas}) != len(entradas)):
        raise ValueError('Prévia de atualização inválida.')
    travadas = bloquear(inventario_id, [e['rodada'] for e in entradas])
    propostas = {p.rodada_id: p for p in PropostaAjuste.objects.filter(
        rodada_id__in=[r.pk for r, _ in travadas], estado='aplicado')}
    if propostas:
        if len(propostas) == len(entradas) and all(
                propostas[e['rodada']].assinatura == e['assinatura'] for e in entradas):
            return list(AplicacaoAjuste.objects.filter(proposta__in=propostas.values()).order_by('proposta__rodada_id'))
        raise ValueError('Seleção parcialmente atualizada. Confira os resultados e confirme uma nova seleção.')
    assinaturas = {e['rodada']: e['assinatura'] for e in entradas}
    avaliacoes = []
    from .contexto_ajustes import carregar
    with carregar([r for r, _ in travadas], travar=True) as lote:
        for rodada, produto in travadas:
            atual = avaliar(rodada, produto, travar_documentos=True, _lote=lote)
            if (atual['bloqueio'] or atual['estado'] not in ('Pendente de revisão', 'Aprovado')
                    or atual.get('assinatura') != assinaturas[rodada.pk]):
                raise ValueError(atual['bloqueio'] or 'Dados alterados. Confira os saldos e confirme novamente.')
            avaliacoes.append((rodada, produto, atual))
    # Every product and document is checked before the first write. The same
    # locks remain held through approval, stock movement, audit and commit.
    preparadas, novas, eventos = [], [], []
    for rodada, produto, atual in avaliacoes:
        proposta = _registrar_decisao(usuario, rodada, atual,
            observacao or 'Atualização de estoque confirmada pelo gestor.', _novas=novas)
        _validar_proposta(proposta, atual)
        eventos.append(EventoAjuste(proposta=proposta, usuario=usuario, acao='confirmado_gestor',
            dados=dict(confirmacao_fisica=proposta.confirmacao_id, observacao=observacao,
                       assinatura=assinaturas[rodada.pk], fluxo='confirmacao_unica')))
        preparadas.append((proposta, rodada, produto, atual))
    PropostaAjuste.objects.bulk_create(novas)
    EventoAjuste.objects.bulk_create([EventoAjuste(proposta=p, usuario=usuario, acao='aprovado',
        dados=dict(evidencia=p.evidencia, justificativa=p.justificativa)) for p in novas] + eventos)
    # Preserve the execution/audit-trigger check that aplicar() performed at
    # this point in the composed flow. It is one cheap check for the whole lot.
    verificar_execucao()
    return _gravar_aplicacoes(usuario, inventario_id, preparadas)


def projetar_resultado(linhas_revisao):
    """Light read model, not an authorization or a documentary validation.

    Reuse the rounds already read by the detail page. Fetch only recorded
    application balances, never the potentially large JSON evidence.
    """
    aplicadas = {a['decisao__item_id']: a for a in AplicacaoAjuste.objects.filter(
        decisao__item_id__in=[l['item'].pk for l in linhas_revisao],
        proposta__estado='aplicado').values(
            'decisao__item_id', 'decisao__rodada_id', 'proposta__rodada_id',
            'antes', 'diferenca', 'depois', 'unidade')}
    campos_ok = ('ok_em', 'esperado', 'contado_base', 'contado', 'sequencia_ok', 'embalagens', 'avulsos')
    ultima = ConfirmacaoContagem.objects.filter(rodada_id=OuterRef('rodada_id')).order_by(
        '-versao_entrada').values('pk')[:1]
    confirmacoes = {c['rodada_id']: c for c in ConfirmacaoContagem.objects.filter(
        rodada_id__in=[l['rodada'].pk for l in linhas_revisao], pk=Subquery(ultima)
    ).values('rodada_id', *campos_ok)}
    linhas = []
    for revisao in linhas_revisao:
        rodada, item = revisao['rodada'], revisao['item']
        linha = dict(rodada=rodada, atual=item.produto.quantidade,
            unidade=item.cadastro.get('base', ''), estado='Requer prévia',
            bloqueio='', saldo_aplicado=None, unidade_aplicada='', preliminar=True)
        if revisao['decisao']:
            linha['estado'] = 'Aplicado'
            aplicacao = aplicadas.get(item.pk)
            if (aplicacao and aplicacao['decisao__rodada_id'] == rodada.pk
                    and aplicacao['proposta__rodada_id'] == rodada.pk
                    and aplicacao['unidade'].strip()
                    and aplicacao['antes'] + aplicacao['diferenca'] == aplicacao['depois']):
                linha.update(saldo_aplicado=aplicacao['depois'],
                             unidade_aplicada=aplicacao['unidade'])
        elif (not rodada.ok_em or rodada.esperado is None or rodada.contado_base is None
                or rodada.sequencia_ok is None or rodada.encerrada_em):
            linha.update(estado='Bloqueado', bloqueio='Confirmação física ausente ou rodada encerrada.')
        elif any(s.resolvida_em is None for s in revisao['sinalizacoes']):
            linha.update(estado='Bloqueado', bloqueio='Sinalização física pendente de resolução.')
        elif (rodada.pk not in confirmacoes or any(
                confirmacoes[rodada.pk][campo] != getattr(rodada, campo) for campo in campos_ok)):
            linha.update(estado='Bloqueado', bloqueio='OK sem confirmação original compatível; solicite recontagem.')
        elif rodada.contado_base == rodada.esperado:
            linha.update(estado='Sem diferença', bloqueio='Sem diferença registrada para atualizar.')
        linhas.append(linha)
    resumo = dict(aptos=0, candidatos=sum(l['estado'] == 'Requer prévia' for l in linhas),
        bloqueados=sum(l['estado'] == 'Bloqueado' for l in linhas),
        aplicados=sum(l['estado'] == 'Aplicado' for l in linhas), total=len(linhas))
    erro = ''
    if len(linhas) > 500:
        resumo['candidatos'] = 0
        erro = 'Mais de 500 produtos: divida a revisão em conferências menores. Aplicação bloqueada.'
    return dict(linhas=linhas, aptas=[], resumo=resumo, erro=erro, preliminar=True)


def preparar_resultado(inventario):
    """Projeção compartilhada, somente leitura, da atualização e sua auditoria."""
    ultima = RodadaContagem.objects.filter(item_id=OuterRef('item_id')).order_by('-numero').values('pk')[:1]
    rodadas = rodadas_incluidas(inventario).filter(pk=Subquery(ultima))
    if rodadas.count() > 500:
        return dict(linhas=[], aptas=[], resumo=dict(aptos=0),
            erro='Mais de 500 produtos: divida a revisão em conferências menores. Aplicação bloqueada.')
    from .contexto_ajustes import carregar
    atuais = list(rodadas.select_related('item__produto').order_by('item__cadastro__nome', 'pk'))
    with carregar(atuais) as lote:
        linhas = [avaliar(r, _lote=lote) for r in atuais]
    # Presentation only: an applied balance is historical, never a fresh forecast.
    aplicadas = {a.proposta.rodada_id: a for a in AplicacaoAjuste.objects.filter(
        proposta__rodada_id__in=[l['rodada'].pk for l in linhas if l['estado'] == 'Aplicado'],
        proposta__estado='aplicado').select_related('proposta', 'decisao')}
    for linha in linhas:
        linha.update(saldo_aplicado=None, unidade_aplicada='')
        aplicacao = aplicadas.get(linha['rodada'].pk)
        if (linha['estado'] == 'Aplicado' and aplicacao
                and aplicacao.decisao.rodada_id == linha['rodada'].pk
                and aplicacao.decisao.item_id == linha['rodada'].item_id
                and aplicacao.unidade.strip()
                and aplicacao.antes + aplicacao.diferenca == aplicacao.depois):
            linha.update(saldo_aplicado=aplicacao.depois, unidade_aplicada=aplicacao.unidade)
    aptas = [l for l in linhas if not l['bloqueio'] and l['estado'] in ('Pendente de revisão', 'Aprovado')]
    resumo = dict(aptos=len(aptas), bloqueados=sum(l['estado'] == 'Bloqueado' for l in linhas),
        aplicados=sum(l['estado'] == 'Aplicado' for l in linhas),
        pendentes=sum(l['estado'] == 'Pendente de revisão' for l in linhas), total=len(linhas))
    return dict(linhas=linhas, aptas=aptas, resumo=resumo, erro='')
