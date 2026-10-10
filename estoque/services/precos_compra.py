"""Snapshots exatos de precos alterados por compras; sem inferencia de legado."""
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from django.db import models, transaction
from django.utils import timezone

from estoque.models import Compra, ItemCompra, Produto, PRECOS_PRODUTO_CONTROLADOS
from .precos_vinculados import serializar_grupos, alterar_precos_grupo, restaurar_evento_grupo, bloquear_produtos_precos, PRECOS_VINCULADOS
from django.core.exceptions import ValidationError, ObjectDoesNotExist


def _valor_json(valor):
    return None if valor is None else str(valor)


@serializar_grupos
def aplicar_precos_compra(item, atualizacoes, operador=None, versao_esperada=None, selecionados=None, confirmar=False, assinatura_esperada=None):
    using = item._state.db or "default"
    with transaction.atomic(using=using):
        compra=Compra.objects.using(using).select_for_update().get(pk=item.compra_id)
        item = ItemCompra.objects.using(using).select_for_update().get(pk=item.pk)
        bloquear_produtos_precos([item.produto_id],using)
        produto = Produto.objects.using(using).select_for_update().get(pk=item.produto_id)
        vinculo=getattr(produto,'vinculo_grupo',None)
        compartilhados={campo:value for campo,value in atualizacoes.items() if campo in PRECOS_VINCULADOS}
        if vinculo:
            compartilhados={campo:value for campo,value in compartilhados.items() if getattr(produto,campo)!=value}
        if vinculo and compartilhados:
            # Purchase callers must provide a version from the operator's review,
            # never silently use the latest version after a competing update.
            if versao_esperada is None:
                raise ValueError('Revisao de produto vinculado exige versao do grupo. Reabra a revisao; nenhuma alteracao foi salva.')
            from estoque.models import MembroGrupoProduto
            obrigatorios=set(MembroGrupoProduto.objects.using(using).filter(
                grupo_id=vinculo.grupo_id,produto_id__in=compra.itens.values_list('produto_id',flat=True)
            ).values_list('produto_id',flat=True))
            if selecionados is None or not obrigatorios.issubset({int(pk) for pk in selecionados}):
                raise ValueError('Confirme todos os integrantes que participaram da mesma compra.')
            custos={campo:value for campo,value in atualizacoes.items() if campo in ('preco_compra','preco_compra_fracionado')}
            if custos:
                # Validate shared sale prices against the final individual cost,
                # with both writes under this same outer transaction.
                aplicar_precos_compra(item,custos,operador=operador)
                produto.refresh_from_db();item.refresh_from_db()
            try:
                evento=alterar_precos_grupo(produto.pk,compartilhados,versao_esperada=versao_esperada,
                    operador=operador,origem='compra',selecionados=selecionados,confirmar=confirmar,assinatura_esperada=assinatura_esperada,evidencia_extra={'compra_id':item.compra_id,'item_id':item.pk,'produto_id':item.produto_id},using=using)
            except ValidationError as exc:
                raise ValueError(' '.join(exc.messages)) from exc
            if evento:
                item.alteracoes_precos_vinculados=[*item.alteracoes_precos_vinculados,str(evento.operation_id)]
                item.save(update_fields=['alteracoes_precos_vinculados'])
                compra.alteracoes_precos_vinculados=[*compra.alteracoes_precos_vinculados,str(evento.operation_id)]
                compra.save(update_fields=['alteracoes_precos_vinculados'])
            produto.refresh_from_db()
            atualizacoes={campo:value for campo,value in atualizacoes.items() if campo not in (*PRECOS_VINCULADOS,'preco_venda',*custos)}
        elif vinculo:
            atualizacoes={campo:value for campo,value in atualizacoes.items() if campo not in (*PRECOS_VINCULADOS,'preco_venda')}
        snapshot = dict(item.alteracoes_precos or {})
        autoria = dict(produto.autoria_precos or {})
        alterados = {}
        for campo, novo in atualizacoes.items():
            if campo not in PRECOS_PRODUTO_CONTROLADOS:
                raise ValueError("Campo de preco nao permitido.")
            anterior = getattr(produto, campo)
            if anterior == novo:
                continue
            token = uuid4().hex
            registro = snapshot.get(campo)
            # Uma revisao da mesma compra pode ampliar seu efeito anterior.
            if registro and autoria.get(campo) == registro.get("alteracao_id") and _valor_json(anterior) == registro.get("novo"):
                valor_anterior = registro["anterior"]
            else:
                valor_anterior = _valor_json(anterior)
            snapshot[campo] = {"anterior": valor_anterior, "novo": _valor_json(novo), "alteracao_id": token}
            autoria[campo] = token
            alterados[campo] = novo
        if alterados:
            # Bypass restrito: valores e autoria sao gravados juntos, sob bloqueio.
            models.QuerySet.update(
                Produto.objects.using(using).filter(pk=produto.pk),
                **alterados, autoria_precos=autoria, atualizado_em=timezone.now(),
            )
            item.alteracoes_precos = snapshot
            item.save(update_fields=["alteracoes_precos"])


@serializar_grupos
def restaurar_precos_compra(item, produto, operador=None, restaurar_grupo=True):
    """Usado na exclusao, com transacao e bloqueio do Produto ja adquiridos."""
    # save(update_fields=estoque) pode normalizar precos apenas em memoria.
    # A comparacao deve usar os valores efetivamente persistidos.
    vinculado=hasattr(produto,'vinculo_grupo')
    if vinculado and any(campo in (*PRECOS_VINCULADOS,'preco_venda') for campo in item.alteracoes_precos):
        raise ValueError('Snapshot legado de produto vinculado: restauracao coletiva nao comprovada. Revisao manual necessaria.')
    produto.refresh_from_db(fields=[*PRECOS_PRODUTO_CONTROLADOS, "autoria_precos"])
    autoria = dict(produto.autoria_precos or {})
    restaurados = {}
    for campo, registro in (item.alteracoes_precos or {}).items():
        if campo not in PRECOS_PRODUTO_CONTROLADOS or not isinstance(registro, dict):
            continue
        token = registro.get("alteracao_id")
        if not token or autoria.get(campo) != token or "anterior" not in registro or "novo" not in registro:
            continue
        try:
            novo = None if registro["novo"] is None else Decimal(registro["novo"])
            anterior = None if registro["anterior"] is None else Decimal(registro["anterior"])
            if (novo is not None and not novo.is_finite()) or (anterior is not None and not anterior.is_finite()):
                continue
        except (InvalidOperation, TypeError, ValueError):
            continue
        if getattr(produto, campo) != novo:
            continue
        restaurados[campo] = anterior
        autoria.pop(campo, None)
    if restaurados:
        models.QuerySet.update(
            Produto.objects.using(produto._state.db).filter(pk=produto.pk),
            **restaurados, autoria_precos=autoria, atualizado_em=timezone.now(),
        )
        for campo, valor in restaurados.items():
            setattr(produto, campo, valor)
        produto.autoria_precos = autoria
    try:
        for operation_id in reversed(item.alteracoes_precos_vinculados if restaurar_grupo else []):
            from estoque.models import AlteracaoPrecoVinculado
            evidencia=AlteracaoPrecoVinculado.objects.using(produto._state.db or 'default').get(operation_id=operation_id).evidencia
            if evidencia.get('compra_id')!=item.compra_id or evidencia.get('produto_id')!=item.produto_id:
                raise ValidationError('Evidencia nao pertence ao item/compra. Revisao manual necessaria.')
            restaurar_evento_grupo(operation_id,operador=operador,using=produto._state.db or 'default')
    except (ValidationError, ObjectDoesNotExist, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ValueError('Restauracao exige revisao manual: '+str(exc)) from exc


@serializar_grupos
def restaurar_precos_da_compra(compra, operador=None):
    from estoque.models import AlteracaoPrecoVinculado
    try:
        for operation_id in reversed(compra.alteracoes_precos_vinculados):
            evento=AlteracaoPrecoVinculado.objects.get(operation_id=operation_id)
            if evento.evidencia.get('compra_id')!=compra.pk:
                raise ValidationError('Evidencia nao pertence a compra.')
            restaurar_evento_grupo(operation_id,operador=operador)
    except (ValidationError,ObjectDoesNotExist,ValueError,KeyError,TypeError,AttributeError) as exc:
        raise ValueError('Restauracao exige revisao manual: '+str(exc)) from exc
