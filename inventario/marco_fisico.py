"""Physical reference in the existing confirmation/proposal/application journal."""
from decimal import Decimal
from types import SimpleNamespace

from django.db.models import Q

from .models import (AplicacaoAjuste, DetalheMovimentoEstoque, EventoAjuste,
                     EventoInventario, PropostaAjuste, SinalizacaoFisica)

REGRA = 'marco_fisico_v1'


def vigente(rodada, lote=None):
    """A later applied OK supersedes an older one, including zero corrections."""
    if lote is not None:
        return any(p.rodada_id != rodada.pk and p.rodada.item.produto_id == rodada.item.produto_id
            and (p.confirmacao.ok_em >= rodada.ok_em or p.confirmacao.sequencia_ok > rodada.sequencia_ok)
            for p in lote.marcos_aplicados)
    return PropostaAjuste.objects.filter(
        rodada__item__produto_id=rodada.item.produto_id, estado='aplicado').exclude(
        rodada=rodada).filter(Q(confirmacao__ok_em__gte=rodada.ok_em) |
                             Q(confirmacao__sequencia_ok__gt=rodada.sequencia_ok)).exists()


def validar_contagem(rodada, produto, lote=None):
    from .conversao import cadastro_compativel, converter
    from .conversao_fisica import exigir_precisao_ajuste, cadastro_contagem
    from .selecao import itens_incluidos
    confs = lote.confirmacoes[rodada.pk] if lote is not None else None
    conf = (confs[-1] if confs else None) if lote is not None else rodada.confirmacoes.order_by('-versao_entrada').first()
    campos = ('ok_em', 'esperado', 'contado_base', 'contado', 'sequencia_ok', 'embalagens', 'avulsos')
    if (not rodada.ok_em or rodada.encerrada_em or not conf or
            any(getattr(conf, c) != getattr(rodada, c) for c in campos)):
        raise ValueError('OK sem confirmação original compatível; solicite recontagem.')
    if vigente(rodada, lote):
        raise ValueError('Existe marco físico mais recente aplicado para este produto.')
    if ((rodada.item_id not in lote.incluidos or lote.maior_numero[rodada.item_id] > rodada.numero)
            if lote is not None else (not itens_incluidos().filter(pk=rodada.item_id).exists()
                or rodada.item.rodadas.filter(numero__gt=rodada.numero).exists())):
        raise ValueError('Contagem retirada ou substituída por recontagem.')
    if not produto.ativo or produto.excluido or not cadastro_compativel(produto, rodada.item.cadastro):
        raise ValueError('Produto ou unidade alterados desde a contagem.')
    exigir_precisao_ajuste(rodada)
    valores = converter(cadastro_contagem(rodada.item), rodada.embalagens, rodada.avulsos)
    if valores[2:] != (rodada.contado, rodada.contado_base):
        raise ValueError('Contagem incompatível com as quantidades físicas registradas.')
    # Earlier signals remain audit evidence; only unresolved issues at/after this
    # OK affect the new reference. The responsible person still authorizes it.
    if (any(item == rodada.item_id and criado >= rodada.ok_em for item, criado in lote.sinais_marco)
            if lote is not None else SinalizacaoFisica.objects.filter(rodada__item_id=rodada.item_id,
                criado_em__gte=rodada.ok_em, resolvida_em__isnull=True).exists()):
        raise ValueError('Sinalização física posterior ao OK sem resolução.')
    return conf


def carregar(lote):
    """Request-local batches, including all products in the authorized lot."""
    if hasattr(lote, 'marcos_aplicados'):
        return
    produtos = [r.item.produto_id for r in lote.rodadas.values()]
    lote.marcos_aplicados = list(PropostaAjuste.objects.filter(estado='aplicado',
        rodada__item__produto_id__in=produtos).select_related('confirmacao', 'rodada__item'))
    lote.detalhes_marco = {d.movimento_id: d for d in DetalheMovimentoEstoque.objects.filter(
        movimento_id__in=[m.pk for ms in lote.movimentos.values() for m in ms])}
    lote.aplicacoes_marco = {f'Movimentação manual #{a.decisao.movimentacao_id}': a
        for a in AplicacaoAjuste.objects.filter(proposta__rodada_id__in=lote.rodadas,
            proposta__estado='aplicado').select_related('proposta__confirmacao', 'decisao__movimentacao')}
    lote.sinais_marco = list(SinalizacaoFisica.objects.filter(
        rodada__item_id__in=[r.item_id for r in lote.rodadas.values()],
        resolvida_em__isnull=True).values_list('rodada__item_id', 'criado_em'))


def comprovar(rodada, produto, movimentos, *, travar=False, lote=None):
    """Only effective writes after the chosen OK; never sum documents twice."""
    from .services import sequencia, verificar_observador
    from .conciliacao_documental import conciliar
    from .saldo_documental import quantidade_historica, unidade
    if lote is not None:
        lote.conferir(rodada, travar)
        carregar(lote)
        if lote.erro_observador:
            raise ValueError(lote.erro_observador)
    else:
        verificar_observador()
    validar_contagem(rodada, produto, lote)
    movimentos = [m for m in movimentos if m.sequencia > rodada.sequencia_ok]
    detalhes = lote.detalhes_marco if lote is not None else {
        d.movimento_id: d for d in DetalheMovimentoEstoque.objects.filter(movimento_id__in=[m.pk for m in movimentos])}
    saldo, posicao, documentos = rodada.esperado, rodada.sequencia_ok, []
    for m in movimentos:
        if (m.sequencia != posicao + 1 or m.antes != saldo or m.depois is None
                or m.registrado_em < rodada.ok_em):
            raise ValueError(f'Movimento #{m.pk}: sequência posterior ao OK incompleta.')
        d = detalhes.get(m.pk)
        aplicacao = (lote.aplicacoes_marco.get(m.referencia) if lote is not None else AplicacaoAjuste.objects.filter(decisao__movimentacao__produto_id=m.produto_id,
            decisao__movimentacao__estoque_antes=m.antes,
            decisao__movimentacao__estoque_depois=m.depois,
            proposta__rodada=rodada, proposta__estado='aplicado').select_related(
                'decisao__movimentacao', 'proposta__confirmacao').first()) if m.origem == 'ajuste_manual' else None
        if aplicacao and m.referencia == f'Movimentação manual #{aplicacao.decisao.movimentacao_id}':
            # The correction establishes this count; it is not another physical
            # receipt to add to that same count's projection.
            from .ajustes import assinatura
            e = aplicacao.proposta.evidencia
            if (assinatura(e) != aplicacao.proposta.assinatura
                    or aplicacao.evidencia['preparacao'] != e
                    or aplicacao.antes + aplicacao.diferenca != aplicacao.depois
                    or m.sequencia != e['sequencia'] + 1):
                raise ValueError('Auditoria da aplicação incompatível.')
            prova = dict(observador=m.pk, sequencia=m.sequencia, efeito='0',
                origem=m.origem, referencia=m.referencia, unidade=aplicacao.unidade,
                antes=str(m.antes), depois=str(m.depois), produto=m.produto_id)
        elif d:
            # Immutable attribution identifies the operand and frozen conversion.
            # A manual adjustment's operand is an absolute target, not a delta.
            valor = quantidade_historica(d, d.quantidade_entrada, d.unidade_entrada)
            delta = m.depois - m.antes
            if (unidade(d.unidade_base) != unidade(rodada.item.cadastro['base'])
                    or valor is None or d.acao in ('regularizacao', 'regularizacao_conferencia', 'cadastro')):
                raise ValueError(f'Movimento #{m.pk}: efeito físico posterior não comprovado.')
            esperado = (m.depois if d.acao == 'ajuste' else
                        -delta if d.acao == 'saida' else delta if d.acao in ('entrada', 'devolucao') else None)
            if (esperado is None or valor.quantize(Decimal('.001')) != esperado
                    or (d.acao != 'ajuste' and valor <= 0)):
                raise ValueError(f'Movimento #{m.pk}: quantidade posterior incompatível com a atribuição.')
            prova = dict(observador=m.pk, sequencia=m.sequencia, produto=m.produto_id,
                origem=m.origem, referencia=m.referencia, unidade=d.unidade_base,
                antes=str(m.antes), depois=str(m.depois), efeito=str(delta),
                registrado_em=m.registrado_em.isoformat(), atribuicao=d.pk,
                conversao=d.conversao, fator=str(d.fator),
                quantidade_entrada=str(d.quantidade_entrada), unidade_entrada=d.unidade_entrada)
        else:
            prova = conciliar(rodada, [m], travar=travar, _lote=lote)[0]
            prova['efeito'] = str(m.depois - m.antes)
        documentos.append(prova)
        saldo, posicao = m.depois, m.sequencia
    atual = lote.sequencias.get(produto.pk, 0) if lote is not None else sequencia(produto.pk)
    if posicao != atual or saldo != produto.quantidade:
        raise ValueError('Saldo ou sequência posterior ao OK incompletos.')
    return posicao, documentos


def resultante(contado, documentos):
    return contado + sum((Decimal(d['efeito']) for d in documentos), Decimal(0))


def recibos(propostas):
    """Normalize zero-effect receipts without inventing a stock movement."""
    propostas = list(propostas)
    aplicacoes = {a.proposta_id: a for a in AplicacaoAjuste.objects.filter(
        proposta__in=propostas).select_related('proposta', 'decisao')}
    eventos = {e.proposta_id: e for e in EventoAjuste.objects.filter(
        proposta__in=propostas, acao='marco_sem_ajuste').select_related('proposta', 'usuario')}
    resultado = []
    for p in propostas:
        if p.pk in aplicacoes:
            resultado.append(aplicacoes[p.pk])
        elif p.pk in eventos:
            e = eventos[p.pk]
            from .ajustes import assinatura
            dados = e.dados
            if (p.estado != 'aplicado' or p.evidencia.get('regra') != REGRA
                    or assinatura(p.evidencia) != p.assinatura
                    or assinatura(dados.get('preparacao')) != p.assinatura
                    or Decimal(p.evidencia['diferenca']) != 0
                    or Decimal(p.evidencia['atual']) != Decimal(p.evidencia['resultante'])):
                raise ValueError('Auditoria do marco sem ajuste incompatível.')
            resultado.append(SimpleNamespace(pk=e.pk, proposta=p, proposta_id=p.pk,
                usuario_id=e.usuario_id, usuario=e.usuario, aplicado_em=e.criado_em,
                antes=Decimal(p.evidencia['atual']), depois=Decimal(p.evidencia['resultante']),
                diferenca=Decimal(0), unidade=p.evidencia['cadastro']['base'],
                evidencia=dados, sem_ajuste=True))
    return resultado


def registrar_sem_ajuste(usuario, proposta, rodada, atual):
    """Called under the same product lock and atomic application transaction."""
    from estoque.models import Produto
    from django.utils import timezone
    proposta.estado = 'aplicado'
    proposta.save(update_fields=['estado'])
    EventoAjuste.objects.create(proposta=proposta, usuario=usuario,
        acao='marco_sem_ajuste', dados=dict(preparacao=proposta.evidencia,
            autorizado_por=proposta.autorizado_por_id, justificativa=proposta.justificativa))
    EventoInventario.objects.create(inventario_id=rodada.item.inventario_id,
        rodada=rodada, usuario=usuario, acao='marco_fisico_aplicado',
        dados=dict(proposta=str(proposta.pk), confirmacao=proposta.confirmacao_id,
                   regra=REGRA, ajuste='0', saldo=str(atual['resultante'])))
    Produto.objects.filter(pk=rodada.item.produto_id).update(estoque_conferido=True,
        estoque_conferido_em=timezone.now(), estoque_conferido_por=usuario)
    return recibos([proposta])[0]
