from django.shortcuts import render, redirect
from .models import Produto

def home(request):
    # SALVAR ou ATUALIZAR
    if request.method == "POST":
        produto_id = request.POST.get("produto_id")

        nome = request.POST.get("nome")
        codigo = request.POST.get("codigo")
        categoria = request.POST.get("categoria")
        preco = request.POST.get("preco") or 0
        quantidade = request.POST.get("quantidade") or 0
        fornecedor = request.POST.get("fornecedor")

        if produto_id:
            produto = Produto.objects.get(id=produto_id)
        else:
            produto = Produto()

        produto.nome = nome
        produto.codigo = codigo
        produto.categoria = categoria
        produto.preco = preco
        produto.quantidade = quantidade
        produto.fornecedor = fornecedor
        produto.save()

        return redirect("home")

    # EXCLUIR
    excluir_id = request.GET.get("del")
    if excluir_id:
        Produto.objects.filter(id=excluir_id).delete()
        return redirect("home")

    # EDITAR
    editar_id = request.GET.get("edit")
    produto_edicao = None
    if editar_id:
        produto_edicao = Produto.objects.get(id=editar_id)

    # BUSCAR
    q = request.GET.get("q")
    if q:
        produtos = Produto.objects.filter(nome__icontains=q)
    else:
        produtos = Produto.objects.all().order_by("-criado_em")

    return render(
        request,
        "estoque/home.html",
        {
            "produtos": produtos,
            "produto_edicao": produto_edicao
        }
    )

