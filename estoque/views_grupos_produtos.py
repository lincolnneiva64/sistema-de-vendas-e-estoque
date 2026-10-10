from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.db.models import Count
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .grupos_produtos import adicionar_membros, criar_grupo, excluir_grupo, renomear_grupo, remover_membro, sugerir_nome
from .models import GrupoProdutoVinculado, MembroGrupoProduto, Produto
from .services.precos_vinculados import (diagnosticar_grupo, bloquear_catalogo,
    regularizar_grupo, adicionar_produtos_regularizados, recusa_operador_precos)


def dados_grupo(grupo, referencia_id=None):
    integrantes = list(grupo.produtos.order_by("nome", "pk"))
    produtos = [{"id": p.pk, "nome": p.nome} for p in integrantes]
    diagnostico=diagnosticar_grupo(integrantes, referencia_id, validar_precos=referencia_id is not None)
    diagnostico.update(regularizado=grupo.precos_regularizados,versao_grupo=grupo.versao_precos,
        propagacao_disponivel=True,
        aviso='Atualizacao seletiva. Ativar ou adicionar membros nao modifica seus precos nem sua adocao.')
    return {"id": grupo.pk, "nome": grupo.nome, "quantidade": len(produtos), "produtos": produtos,
            "precos_vinculados": diagnostico}


@require_GET
def grupos_lista(request):
    grupos = GrupoProdutoVinculado.objects.annotate(quantidade=Count("membros")).order_by("nome", "pk")
    return JsonResponse({"grupos": [
        {"id": g.pk, "nome": g.nome, "quantidade": g.quantidade,
         "url": reverse("estoque:grupos_produtos_detalhe", args=[g.pk])} for g in grupos
    ]})


@require_POST
def grupos_criar(request):
    try:
        ids = {int(valor) for valor in request.POST.getlist("produto_ids")}
        if request.POST.get("acao") == "sugerir":
            produtos = list(Produto.objects.filter(pk__in=ids, excluido=False).order_by("nome"))
            if len(ids) < 2 or len(produtos) != len(ids):
                raise ValidationError("Selecione pelo menos dois produtos disponíveis.")
            if MembroGrupoProduto.objects.filter(produto_id__in=ids).exists():
                raise ValidationError("Remova o vínculo atual dos produtos antes de criar outro grupo.")
            return JsonResponse({"nome": sugerir_nome([p.nome for p in produtos])})
        if request.POST.get('acao') == 'previa':
            produtos = list(Produto.objects.filter(pk__in=ids, excluido=False).order_by('pk'))
            if len(ids) < 2 or len(produtos) != len(ids) or MembroGrupoProduto.objects.filter(produto_id__in=ids).exists():
                raise ValidationError('Selecione pelo menos dois produtos disponíveis e sem vínculo.')
            referencia = int(request.POST['referencia']) if request.POST.get('referencia') else None
            return JsonResponse({'precos_vinculados': diagnosticar_grupo(produtos, validar_precos=False)})
        erro = recusa_operador_precos(request.user)
        if erro is not None:
            return erro
        if request.POST.get('confirmar') != '1':
            raise ValidationError('Confira os integrantes e confirme a ativação sem reajuste de preços.')
        with bloquear_catalogo():
            grupo = criar_grupo(ids, request.POST.get("nome"))
            regularizar_grupo(grupo.pk, request.POST.get('versao_observada'),
                confirmar=True, operador=request.user)
            grupo.refresh_from_db()
        return JsonResponse({**dados_grupo(grupo), "mensagem": f"{grupo.produtos.count()} produtos vinculados com sucesso."}, status=201)
    except (ValueError, TypeError):
        return JsonResponse({"erro": "Seleção de produtos inválida."}, status=400)
    except ValidationError as exc:
        return JsonResponse({"erro": " ".join(exc.messages)}, status=400)
    except IntegrityError:
        return JsonResponse({"erro": "Um produto já foi vinculado. Atualize a listagem."}, status=409)


@require_http_methods(["GET", "POST"])
def grupos_detalhe(request, grupo_id):
    grupo = get_object_or_404(GrupoProdutoVinculado, pk=grupo_id)
    if request.method == "GET":
        if request.GET.get("elegiveis") == "1":
            produtos = Produto.objects.filter(ativo=True, excluido=False, vinculo_grupo__isnull=True).order_by("nome", "pk")
            return JsonResponse({"produtos": list(produtos.values("id", "nome"))})
        try:
            referencia = int(request.GET['referencia']) if 'referencia' in request.GET else None
            with bloquear_catalogo():
                grupo.refresh_from_db()
                response = JsonResponse(dados_grupo(grupo, referencia))
            response['Cache-Control'] = 'private, no-store'
            return response
        except (ValueError, ValidationError):
            return JsonResponse({'erro': 'Referencia invalida para este grupo.'}, status=400)
    try:
        if request.POST.get('acao') == 'previa_adicao':
            ids = {int(v) for v in request.POST.getlist('produto_ids')}
            atuais = list(grupo.produtos.order_by('pk'))
            novos = list(Produto.objects.filter(pk__in=ids, excluido=False, ativo=True, vinculo_grupo__isnull=True).order_by('pk'))
            if not ids or len(novos) != len(ids):
                raise ValidationError('Selecione produtos ativos disponíveis e sem vínculo.')
            return JsonResponse({'precos_vinculados': diagnosticar_grupo([*atuais, *novos], validar_precos=False)})
        erro = recusa_operador_precos(request.user)
        if erro is not None:
            return erro
        if request.POST.get('acao') == 'regularizar':
            regularizar_grupo(grupo_id, request.POST.get('versao_observada'),
                confirmar=request.POST.get('confirmar') == '1', operador=request.user)
            mensagem = 'Grupo ativado sem alterar precos ou adocao dos integrantes.'
        elif request.POST.get("acao") == "renomear":
            renomear_grupo(grupo_id, request.POST.get("nome"))
            mensagem = "Grupo atualizado."
        elif request.POST.get("acao") == "adicionar":
            with bloquear_catalogo():
                grupo.refresh_from_db()
                ids = [int(v) for v in request.POST.getlist('produto_ids')]
                if MembroGrupoProduto.objects.filter(produto_id__in=ids).exclude(grupo_id=grupo_id).exists():
                    raise ValidationError('Um produto pertence a outro grupo. Remova o vínculo atual antes de continuar.')
                if not ids:
                    raise ValidationError('Selecione pelo menos um produto para adicionar.')
                if Produto.objects.filter(pk__in=set(ids), ativo=True, excluido=False).count() != len(set(ids)):
                    raise ValidationError('Selecione produtos ativos disponíveis.')
                evento = adicionar_produtos_regularizados(grupo_id, ids,
                    request.POST.get('versao_observada'), confirmar=request.POST.get('confirmar') == '1', operador=request.user)
                total = bool(evento)
            mensagem = "Produtos adicionados ao grupo." if total else "Os produtos selecionados já pertencem ao grupo."
        elif request.POST.get("acao") == "remover":
            remover_membro(grupo_id, int(request.POST.get("produto_id", "")))
            mensagem = "Produto removido do grupo."
        elif request.POST.get("acao") == "excluir":
            if request.POST.get("confirmar") != "1":
                raise ValidationError("Confirme a exclusão do grupo.")
            excluir_grupo(grupo_id)
            mensagem = "Grupo excluído. Os produtos foram preservados."
        else:
            raise ValidationError("Ação inválida.")
        grupo = GrupoProdutoVinculado.objects.filter(pk=grupo_id).first()
        if not grupo and request.POST.get("acao") == "remover":
            mensagem += " O grupo ficou vazio e foi excluído."
        return JsonResponse({"ok": True, "excluido": grupo is None, "mensagem": mensagem,
                             "grupo": dados_grupo(grupo) if grupo else None})
    except (ValueError, TypeError, MembroGrupoProduto.DoesNotExist, GrupoProdutoVinculado.DoesNotExist):
        return JsonResponse({"erro": "O vínculo não está mais disponível. Atualize a listagem."}, status=400)
    except ValidationError as exc:
        return JsonResponse({"erro": " ".join(exc.messages)}, status=400)
    except IntegrityError:
        return JsonResponse({"erro": "Um produto já foi vinculado. Atualize a seleção."}, status=409)
