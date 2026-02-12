from django.db.models import Q
from django.shortcuts import render, redirect, get_object_or_404

from .forms import ProdutoForm
from .models import Produto


def home(request):
    produto_edicao = None

    # POST: criar/editar/excluir
    if request.method == "POST":
        acao = request.POST.get("acao")

        # EXCLUIR
        if acao == "excluir":
            excluir_id = request.POST.get("excluir_id")
            if excluir_id:
                Produto.objects.filter(id=excluir_id).delete()
            return redirect("estoque:home")

        # CRIAR / EDITAR
        produto_id = request.POST.get("produto_id")
        if produto_id:
            produto_edicao = get_object_or_404(Produto, id=produto_id)
            form = ProdutoForm(request.POST, instance=produto_edicao)
        else:
            form = ProdutoForm(request.POST)

        if form.is_valid():
            form.save()
            return redirect("estoque:home")

    # GET: carregar formulário de edição (se tiver ?edit=ID) ou formulário vazio
    editar_id = request.GET.get("edit")
    if editar_id:
        produto_edicao = get_object_or_404(Produto, id=editar_id)
        form = ProdutoForm(instance=produto_edicao)
    else:
        form = ProdutoForm()

    # Busca
    q = request.GET.get("q", "").strip()
    produtos = Produto.objects.all().order_by("-criado_em")
    if q:
        produtos = produtos.filter(
            Q(nome__icontains=q) |
            Q(codigo__icontains=q) |
            Q(categoria__icontains=q)
        )

    return render(
        request,
        "estoque/home.html",
        {
            "produtos": produtos,
            "produto_edicao": produto_edicao,
            "form": form,
            "q": q,
        },
    )
def cadastrar_produto(request):
    return redirect("estoque:home")
