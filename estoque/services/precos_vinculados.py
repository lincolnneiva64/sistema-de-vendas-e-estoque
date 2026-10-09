"""Central price transactions, purchase provenance and read-only group previews.

Loading this module never normalizes existing product prices.
"""
import copy
import hashlib
import json
from contextlib import contextmanager
from functools import wraps
from uuid import UUID, uuid4
from decimal import Decimal

from django.core.exceptions import ValidationError, PermissionDenied
from django.http import JsonResponse
from django.db import transaction, models
from django.utils import timezone

PRECOS_VINCULADOS = ('preco_vista', 'preco_prazo',
                     'preco_vista_fracionado', 'preco_prazo_fracionado')
PRECOS_LEGADOS = ('preco_venda_1', 'preco_venda_2')


def exigir_operador_precos(operador):
    if operador is None or not operador.is_authenticated:
        raise PermissionDenied('Entre no sistema antes de alterar preços.')
    if not operador.has_perm('estoque.change_produto'):
        raise PermissionDenied('Operador sem permissão para alterar preços de produtos.')


def recusa_operador_precos(operador):
    try:
        exigir_operador_precos(operador)
    except PermissionDenied as exc:
        return JsonResponse({'erro': str(exc)}, status=403)
    return None


def proteger_precos_http(*, sempre=False):
    """Authorize price commands before purchase/financial writes begin."""
    def decorate(func):
        @wraps(func)
        def wrapped(request, *args, **kwargs):
            comando = any(k.startswith(('atualizar_preco_venda_', 'novo_preco_venda_produto_')) for k in request.POST)
            if request.method == 'POST' and (sempre or comando):
                erro = recusa_operador_precos(request.user)
                if erro is not None:
                    return erro
            return func(request, *args, **kwargs)
        return wrapped
    return decorate


def versao_cadastro_produto(produto):
    """Observed individual row; prevents stale editor cost/stock overwrites.

    This is an optimistic concurrency token, not an authorization credential.
    """
    dados = {field.attname: getattr(produto, field.attname) for field in produto._meta.concrete_fields}
    return hashlib.sha256(json.dumps(dados, sort_keys=True, default=str).encode()).hexdigest()


@contextmanager
def bloquear_catalogo(using='default'):
    # Deliberate conservative serialization. A dedicated singleton also covers
    # empty/new groups; it avoids phantom membership races and lock inversion.
    from estoque.models import ControlePrecosVinculados
    with transaction.atomic(using=using):
        ControlePrecosVinculados.objects.using(using).select_for_update().get_or_create(pk=1)
        yield


def serializar_grupos(func):
    @wraps(func)
    def wrapped(*args, **kwargs):
        if args and hasattr(args[0], 'method') and args[0].method != 'POST':
            return func(*args, **kwargs)
        using=kwargs.get('using') or (getattr(getattr(args[0], '_state', None), 'db', None) if args else None) or 'default'
        with bloquear_catalogo(using):
            return func(*args, **kwargs)
    return wrapped


def impedir_escrita_direta(produto_id, atualizacoes, using='default'):
    from estoque.models import MembroGrupoProduto, Produto
    vinculo=MembroGrupoProduto.objects.using(using).select_related('grupo').filter(produto_id=produto_id).first()
    if not vinculo:
        return
    produto=Produto.objects.using(using).get(pk=produto_id)
    if vinculo.grupo.precos_regularizados:
        if any(c in atualizacoes and getattr(produto, c) != atualizacoes[c] for c in PRECOS_LEGADOS):
            raise ValidationError('Grupo ativo: preços legados não podem ser alterados individualmente. Use os preços compartilhados no cadastro principal.')
        for campo in ('unidade_compra','unidade_venda_1','unidade_venda_2','fator_conversao','vende_fracionado'):
            if campo in ('unidade_venda_2','fator_conversao') and not produto.vende_fracionado and not atualizacoes.get('vende_fracionado'):
                continue
            if campo in atualizacoes:
                anterior=getattr(produto,campo);novo=atualizacoes[campo]
                if campo.startswith('unidade_'):anterior,novo=_unidade(anterior),_unidade(novo)
                if anterior!=novo:raise ValidationError('Desvincule o produto antes de alterar a apresentacao/conversao do grupo ativo.')
    campos=set(atualizacoes).intersection((*PRECOS_VINCULADOS, 'preco_venda'))
    if not produto.vende_fracionado:campos.difference_update(PRECOS_VINCULADOS[2:])
    if not campos:return
    atual=Produto.objects.using(using).values(*campos).get(pk=produto_id)
    if any(atual[campo] != atualizacoes[campo] for campo in campos):
        raise ValidationError('Produto vinculado: alteração isolada de preço bloqueada. Edite pelo cadastro do produto; se o grupo estiver pendente, regularize em Gerenciar grupos vinculados.')


def _integrantes(grupo, using):
    from estoque.models import Produto
    return list(Produto.objects.using(using).select_for_update().filter(vinculo_grupo__grupo=grupo).order_by('pk'))


def bloquear_produtos_precos(produto_ids, using='default'):
    """Call under catalog mutex, before taking any individual product lock.

    Include peers so stock-only sales (which do not need the catalog mutex) see
    the same ascending product order as purchases followed by price propagation.
    """
    from estoque.models import GrupoProdutoVinculado, MembroGrupoProduto, Produto
    ids=set(produto_ids)
    grupos=list(MembroGrupoProduto.objects.using(using).filter(produto_id__in=ids).values_list('grupo_id',flat=True))
    list(GrupoProdutoVinculado.objects.using(using).select_for_update().filter(pk__in=grupos).order_by('pk'))
    ids.update(MembroGrupoProduto.objects.using(using).filter(grupo_id__in=grupos).values_list('produto_id',flat=True))
    return {p.pk:p for p in Produto.objects.using(using).select_for_update().filter(pk__in=ids).order_by('pk')}


def _validar(produtos):
    from estoque.views import _custo_produto_para_unidade_venda
    for p in produtos:
        campos=PRECOS_VINCULADOS if p.vende_fracionado else PRECOS_VINCULADOS[:2]
        for campo in campos:
            valor=getattr(p,campo)
            if valor is None or not valor.is_finite() or valor<0 or valor>Decimal('99999999.99') or valor!=valor.quantize(Decimal('0.01')):
                raise ValidationError(f'Produto {p.pk}: preco monetario invalido.')
        if p.vende_fracionado:
            custo=_custo_produto_para_unidade_venda(p,p.unidade_venda_2)
            if custo>0 and any(getattr(p,campo)<custo for campo in PRECOS_VINCULADOS[2:]):
                raise ValidationError(f'Produto {p.pk}: preco fracionado abaixo do custo individual, conforme protecao de vendas.')
    diagnostico=diagnosticar_grupo(produtos)
    if diagnostico['bloqueios']:
        raise ValidationError([f"Produto {b['produto_id']}: {b['motivo']}" for b in diagnostico['bloqueios']])
    if diagnostico['divergentes'] or diagnostico['derivados_divergentes']:
        raise ValidationError('Grupo com precos divergentes; regularizacao necessaria.')


def _gravar(grupo, produtos, propostas, operador, origem, operation_id=None, evidencia_extra=None, reverte=None, evento_anterior=None, autoria_final=None, using='default'):
    exigir_operador_precos(operador)
    from estoque.models import Produto, AlteracaoPrecoVinculado
    evidencia={**(evidencia_extra or {}), 'integrantes':[p.pk for p in produtos], 'alteracoes':{},
               'operador_snapshot': {'id': operador.pk if operador else None,
                                    'nome': operador.get_username() if operador else 'sistema'}}
    evidencia['estados']={str(p.pk):{'nome':p.nome,
        'anteriores':{campo:_dinheiro(getattr(p,campo)) for campo in (*PRECOS_VINCULADOS,'preco_venda')},
        'novos':{campo:_dinheiro(getattr(propostas[p.pk],campo)) for campo in (*PRECOS_VINCULADOS,'preco_venda')}} for p in produtos}
    op_id=UUID(str(operation_id)) if operation_id else uuid4()
    for p in produtos:
        candidato=propostas[p.pk]
        novos={campo:getattr(candidato,campo) for campo in (*PRECOS_VINCULADOS,'preco_venda') if getattr(p,campo)!=getattr(candidato,campo)}
        autoria=dict(p.autoria_precos or {})
        alteracoes={}
        for campo, novo in novos.items():
            anterior_token=autoria.get(campo)
            novo_token=(autoria_final or {}).get(p.pk,{}).get(campo,str(op_id))
            alteracoes[campo]={'anterior':format(getattr(p,campo),'.2f'), 'novo':format(novo,'.2f'),
                              'autoria_anterior':anterior_token,'autoria_nova':novo_token}
            if novo_token is None:autoria.pop(campo,None)
            else:autoria[campo]=novo_token
        if novos:
            # Derived percentages remain individual, based on each own cost.
            for campo, percentual in [('preco_vista_fracionado','percentual_vista_fracionado'),('preco_prazo_fracionado','percentual_prazo_fracionado')]:
                if campo in novos and p.preco_compra_fracionado and p.preco_compra_fracionado>0:
                    novos[percentual]=((novos[campo]/p.preco_compra_fracionado-1)*100).quantize(Decimal('0.01'))
            models.QuerySet.update(Produto.objects.using(using).filter(pk=p.pk),**novos,autoria_precos=autoria,atualizado_em=timezone.now())
        evidencia['alteracoes'][str(p.pk)]=alteracoes
    anterior=grupo.ultima_alteracao_precos
    versao=grupo.versao_precos
    grupo.versao_precos+=1
    grupo.precos_regularizados=True
    grupo.ultima_alteracao_precos=evento_anterior if reverte else op_id
    grupo.save(update_fields=['versao_precos','precos_regularizados','ultima_alteracao_precos','atualizado_em'])
    return AlteracaoPrecoVinculado.objects.using(using).create(operation_id=op_id,
        grupo_id_snapshot=grupo.pk,grupo_nome_snapshot=grupo.nome,operador=operador,
        origem=origem,versao_antes=versao,versao_depois=grupo.versao_precos,
        evento_anterior=anterior,reverte=reverte,evidencia=evidencia)


@serializar_grupos
def regularizar_grupo(grupo_id, versao_observada, *, referencia_id=None, confirmar=False, operador=None, using='default'):
    from estoque.models import GrupoProdutoVinculado
    if confirmar is not True:
        raise ValidationError('Confirme a previa antes de regularizar.')
    grupo=GrupoProdutoVinculado.objects.using(using).select_for_update().get(pk=grupo_id)
    produtos=_integrantes(grupo,using)
    diagnostico=diagnosticar_grupo(produtos,referencia_id)
    if diagnostico['versao_observada']!=versao_observada:
        raise ValidationError('Previa desatualizada. Consulte novamente.')
    if referencia_id is None and (diagnostico['divergentes'] or diagnostico['derivados_divergentes']):
        raise ValidationError('Escolha explicitamente o produto de referencia.')
    referencia=next((p for p in produtos if p.pk==referencia_id),None)
    propostas={p.pk:copy.copy(p) for p in produtos}
    for p in produtos:
        if referencia:
            for campo in diagnostico['campos']:setattr(propostas[p.pk],campo,getattr(referencia,campo))
        propostas[p.pk].preco_venda=propostas[p.pk].preco_vista
    _validar(list(propostas.values()))
    if grupo.precos_regularizados and all(getattr(p,c)==getattr(propostas[p.pk],c) for p in produtos for c in (*PRECOS_VINCULADOS,'preco_venda')):
        return None
    return _gravar(grupo,produtos,propostas,operador,'regularizacao',using=using)


@serializar_grupos
def alterar_precos_grupo(produto_id, atualizacoes, *, versao_esperada, operador=None, origem='edicao', evidencia_extra=None, using='default'):
    from estoque.models import GrupoProdutoVinculado,MembroGrupoProduto
    if not set(atualizacoes).issubset(PRECOS_VINCULADOS):
        raise ValidationError('Somente os quatro precos de venda podem ser compartilhados.')
    vinculo=MembroGrupoProduto.objects.using(using).get(produto_id=produto_id)
    grupo=GrupoProdutoVinculado.objects.using(using).select_for_update().get(pk=vinculo.grupo_id)
    if not grupo.precos_regularizados:
        raise ValidationError('Grupo ainda nao regularizado. Nenhum preco foi alterado.')
    if grupo.versao_precos!=versao_esperada:
        raise ValidationError('Versao de precos desatualizada. Reabra a revisao.')
    produtos=_integrantes(grupo,using);fonte=next(p for p in produtos if p.pk==produto_id)
    if diagnosticar_grupo(produtos)['divergentes'] or diagnosticar_grupo(produtos)['derivados_divergentes']:
        raise ValidationError('Grupo divergente; regularizacao necessaria.')
    valores={campo:Decimal(str(value)) for campo,value in atualizacoes.items()}
    alterados={campo:value for campo,value in valores.items() if getattr(fonte,campo)!=value}
    if not alterados:return None
    if not fonte.vende_fracionado and set(alterados).intersection(PRECOS_VINCULADOS[2:]):
        raise ValidationError('Grupo sem fracionamento.')
    if any(not value.is_finite() or value<0 or value>Decimal('99999999.99') or value!=value.quantize(Decimal('0.01')) for value in alterados.values()):
        raise ValidationError('Preco monetario invalido.')
    propostas={p.pk:copy.copy(p) for p in produtos}
    for candidato in propostas.values():
        for campo,value in alterados.items():setattr(candidato,campo,value)
        candidato.preco_venda=candidato.preco_vista
    _validar(list(propostas.values()))
    return _gravar(grupo,produtos,propostas,operador,origem,evidencia_extra=evidencia_extra,using=using)


@serializar_grupos
def restaurar_evento_grupo(operation_id, *, operador=None, using='default'):
    from estoque.models import AlteracaoPrecoVinculado,GrupoProdutoVinculado,Produto
    evento=AlteracaoPrecoVinculado.objects.using(using).get(operation_id=operation_id)
    if AlteracaoPrecoVinculado.objects.using(using).filter(reverte=evento.operation_id).exists():return None
    grupo=GrupoProdutoVinculado.objects.using(using).select_for_update().filter(pk=evento.grupo_id_snapshot).first()
    if not grupo or not grupo.precos_regularizados or grupo.ultima_alteracao_precos!=evento.operation_id:
        raise ValidationError('Restauracao bloqueada: grupo alterado posteriormente ou ausente. Revisao manual necessaria.')
    produtos=_integrantes(grupo,using)
    if [p.pk for p in produtos]!=evento.evidencia.get('integrantes'):
        raise ValidationError('Integrantes mudaram. Restauracao exige revisao manual.')
    propostas={p.pk:copy.copy(p) for p in produtos}
    for p in produtos:
        for campo,registro in evento.evidencia['alteracoes'].get(str(p.pk),{}).items():
            if campo not in (*PRECOS_VINCULADOS,'preco_venda') or getattr(p,campo)!=Decimal(registro['novo']) or (p.autoria_precos or {}).get(campo)!=registro['autoria_nova']:
                raise ValidationError('Evidencia divergente. Restauracao exige revisao manual.')
            setattr(propostas[p.pk],campo,Decimal(registro['anterior']))
    _validar(list(propostas.values()))
    autorias={p.pk:{campo:registro['autoria_anterior'] for campo,registro in
        evento.evidencia['alteracoes'].get(str(p.pk),{}).items()} for p in produtos}
    return _gravar(grupo,produtos,propostas,operador,'restauracao_compra',reverte=evento.operation_id,
        evento_anterior=evento.evento_anterior,autoria_final=autorias,using=using)


@serializar_grupos
def adicionar_produtos_regularizados(grupo_id, produto_ids, versao_observada, *, confirmar=False, operador=None, using='default'):
    from estoque.models import GrupoProdutoVinculado, MembroGrupoProduto, Produto
    if confirmar is not True:raise ValidationError('Confirme a previa dos novos integrantes.')
    grupo=GrupoProdutoVinculado.objects.using(using).select_for_update().get(pk=grupo_id)
    if not grupo.precos_regularizados:raise ValidationError('Regularize o grupo antes de adicionar com precos compartilhados.')
    bloquear_produtos_precos([*grupo.produtos.values_list('pk',flat=True),*produto_ids],using)
    atuais=_integrantes(grupo,using);ids=set(produto_ids)-{p.pk for p in atuais}
    if not ids:return None
    novos=list(Produto.objects.using(using).select_for_update().filter(pk__in=ids,excluido=False).order_by('pk'))
    if len(novos)!=len(ids) or MembroGrupoProduto.objects.using(using).filter(produto_id__in=ids).exists():
        raise ValidationError('Produto indisponivel ou pertencente a outro grupo.')
    todos=sorted([*atuais,*novos],key=lambda p:p.pk)
    if diagnosticar_grupo(todos)['versao_observada']!=versao_observada:raise ValidationError('Previa de entrada desatualizada.')
    propostas={p.pk:copy.copy(p) for p in todos}
    _validar(atuais)
    for p in novos:
        for campo in (PRECOS_VINCULADOS if atuais[0].vende_fracionado else PRECOS_VINCULADOS[:2]):
            setattr(propostas[p.pk],campo,getattr(atuais[0],campo))
        propostas[p.pk].preco_venda=propostas[p.pk].preco_vista
    _validar(list(propostas.values()))
    MembroGrupoProduto.objects.using(using).bulk_create([MembroGrupoProduto(grupo=grupo,produto=p) for p in novos])
    return _gravar(grupo,todos,propostas,operador,'entrada_integrantes',using=using)


@serializar_grupos
def salvar_formulario_produto(form, *, operador=None):
    """Save the ordinary editor and group command in one transaction.

    The browser's group identity/version must match even for cost-only edits:
    otherwise old submitted sale prices could silently undo a newer operation.
    """
    from estoque.models import MembroGrupoProduto, Produto
    produto = form.save(commit=False)
    if produto.pk:
        bloqueados = bloquear_produtos_precos([produto.pk])
        atual = bloqueados[produto.pk]
        vinculo = MembroGrupoProduto.objects.select_related('grupo').filter(produto_id=produto.pk).first()
        observado = form.cleaned_data.get('grupo_precos_id')
        if observado != (vinculo.grupo_id if vinculo else None):
            raise ValidationError('O vínculo mudou. Reabra o cadastro antes de salvar.')
        if vinculo and vinculo.grupo.precos_regularizados:
            exigir_operador_precos(operador)
            if form.cleaned_data.get('versao_precos_grupo') != vinculo.grupo.versao_precos:
                raise ValidationError('Preços alterados por outra operação. Reabra o cadastro; nada foi gravado.')
            if form.cleaned_data.get('versao_cadastro_precos') != versao_cadastro_produto(atual):
                raise ValidationError('Cadastro, custo ou estoque alterado por outra operação. Reabra o cadastro; nada foi gravado.')
            custos = [c for c in ('preco_compra', 'preco_compra_fracionado') if getattr(produto, c) != getattr(atual, c)]
            if custos:
                # Validate the proposed editor values, then write only this
                # member's costs. Propagation validates against final costs;
                # a later rejection rolls back this write too.
                produto.save(update_fields=custos)
            campos = PRECOS_VINCULADOS if atual.vende_fracionado else PRECOS_VINCULADOS[:2]
            alterar_precos_grupo(produto.pk, {c: getattr(produto, c) for c in campos},
                versao_esperada=vinculo.grupo.versao_precos, operador=operador, origem='edicao_produto')
            atual.refresh_from_db()
            produto.autoria_precos = atual.autoria_precos
            produto.preco_venda = produto.preco_vista
    produto.cadastro_incompleto = False
    produto.save()
    if produto.pk and MembroGrupoProduto.objects.filter(produto_id=produto.pk, grupo__precos_regularizados=True).exists():
        vinculo = MembroGrupoProduto.objects.get(produto_id=produto.pk)
        _validar(list(Produto.objects.filter(vinculo_grupo__grupo_id=vinculo.grupo_id).order_by('pk')))
    form.save_m2m()
    form.salvar_fornecedores(produto)
    return produto


def _unidade(value):
    return str(value or '').strip().upper()


def _dinheiro(value):
    return None if value is None else format(value, '.2f')


def diagnosticar_grupo(produtos, referencia_id=None):
    """One observed set, including inactive members; no implicit reference."""
    produtos = sorted(produtos, key=lambda p: p.pk)
    signatures = []
    atuais = []
    bloqueios = []
    for p in produtos:
        principal = _unidade(p.unidade_venda_1 or p.unidade_compra)
        secundaria = _unidade(p.unidade_venda_2) if p.vende_fracionado else ''
        fator = Decimal(p.fator_conversao or 0) if p.vende_fracionado else Decimal(0)
        signatures.append((principal, secundaria, str(fator.normalize()), p.vende_fracionado))
        if p.unidade_compra and _unidade(p.unidade_compra) != principal:
            bloqueios.append({'produto_id': p.pk, 'motivo': 'Unidade de compra e unidade principal divergentes.'})
        if not principal or p.vende_fracionado and (not secundaria or secundaria == principal or not fator.is_finite() or fator <= 0):
            bloqueios.append({'produto_id': p.pk, 'motivo': 'Unidade ou conversao incompleta/invalida.'})
        if p.excluido:
            bloqueios.append({'produto_id': p.pk, 'motivo': 'Produto excluido ainda vinculado.'})
        atuais.append({'id': p.pk, 'nome': p.nome, 'ativo': p.ativo,
                       'unidade_principal': principal, 'unidade_fracionada': secundaria,
                       'vende_fracionado': p.vende_fracionado, 'fator': format(fator, '.2f'),
                       'descricao_conversao': p.descricao_conversao or '',
                       'precos': {campo: _dinheiro(getattr(p, campo)) for campo in PRECOS_VINCULADOS},
                       'preco_venda': _dinheiro(p.preco_venda),
                       'custo': _dinheiro(p.preco_compra),
                       'custo_fracionado': _dinheiro(p.preco_compra_fracionado),
                       'autoria_precos': p.autoria_precos or {},
                       'permitir_prejuizo': p.permitir_prejuizo, 'motivo_prejuizo': p.motivo_prejuizo or ''})
    if not produtos:
        bloqueios.append({'produto_id': None, 'motivo': 'Grupo sem integrantes.'})
    if signatures and any(signature != signatures[0] for signature in signatures):
        bloqueios.append({'produto_id': None, 'motivo': 'Integrantes com unidades, fator ou fracionamento diferentes.'})
    campos = PRECOS_VINCULADOS if produtos and produtos[0].vende_fracionado else PRECOS_VINCULADOS[:2]
    divergentes = [campo for campo in campos if len({getattr(p, campo) for p in produtos}) > 1]
    derivados = [p.pk for p in produtos if p.preco_venda != p.preco_vista]
    referencia = None
    if referencia_id is not None:
        referencia = next((p for p in produtos if p.pk == referencia_id), None)
        if referencia is None:
            raise ValidationError('Produto de referencia nao pertence ao grupo.')
    propostas = []
    if produtos:
        for p in produtos:
            candidato = copy.copy(p)
            novos = {campo: getattr(referencia or p, campo) for campo in campos}
            for campo, value in novos.items():
                setattr(candidato, campo, value)
            candidato.preco_venda = candidato.preco_vista
            try:
                if candidato.preco_compra is None:
                    raise ValidationError('Custo individual ausente; conferir cadastro.')
                if any(value is None or not value.is_finite() or value < 0 for value in novos.values()):
                    raise ValidationError('Preco ausente ou invalido.')
                # Reuse the existing main-price/loss protection, no new margin.
                candidato.clean()
                if candidato.vende_fracionado:
                    from estoque.views import _custo_produto_para_unidade_venda
                    custo = _custo_produto_para_unidade_venda(candidato, candidato.unidade_venda_2)
                    if custo > 0 and any(getattr(candidato, c) < custo for c in PRECOS_VINCULADOS[2:]):
                        raise ValidationError('Preço fracionado abaixo do custo individual, conforme proteção de vendas.')
            except ValidationError as exc:
                bloqueios.append({'produto_id': p.pk, 'motivo': ' '.join(exc.messages)})
            if referencia is None:
                continue
            propostas.append({'produto_id': p.pk, 'nome': p.nome,
                              'anteriores': {campo: _dinheiro(getattr(p, campo)) for campo in campos},
                              'novos': {campo: _dinheiro(value) for campo, value in novos.items()},
                              'preco_venda_anterior': _dinheiro(p.preco_venda),
                              'preco_venda_novo': _dinheiro(referencia.preco_vista)})
    version = hashlib.sha256(json.dumps(atuais, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
    return {'status': 'bloqueado' if bloqueios else 'referencia_necessaria' if divergentes or derivados else 'precos_iguais',
            'campos': list(campos), 'divergentes': divergentes,
            'derivados_divergentes': derivados, 'bloqueios': bloqueios,
            'produtos': atuais, 'referencia_id': referencia_id, 'previa': propostas,
            'versao_observada': version, 'propagacao_disponivel': True,
            'apresentacao_fisica_exige_conferencia': True,
            'aviso': 'Confira a prévia. Nenhum preço é alterado antes da confirmação explícita.'}
