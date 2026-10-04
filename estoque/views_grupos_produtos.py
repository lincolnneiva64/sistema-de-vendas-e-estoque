from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.db.models import Count
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .grupos_produtos import adicionar_membros, criar_grupo, excluir_grupo, renomear_grupo, remover_membro, sugerir_nome
from .models import GrupoProdutoVinculado, MembroGrupoProduto, Produto


def dados_grupo(grupo):
    produtos = list(grupo.produtos.order_by("nome", "pk").values("id", "nome"))
    return {"id": grupo.pk, "nome": grupo.nome, "quantidade": len(produtos), "produtos": produtos}


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
        grupo = criar_grupo(ids, request.POST.get("nome"))
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
        return JsonResponse(dados_grupo(grupo))
    try:
        if request.POST.get("acao") == "renomear":
            renomear_grupo(grupo_id, request.POST.get("nome"))
            mensagem = "Grupo atualizado."
        elif request.POST.get("acao") == "adicionar":
            total = adicionar_membros(grupo_id, [int(v) for v in request.POST.getlist("produto_ids")])
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
