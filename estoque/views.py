from django.db.models import Q
from django.shortcuts import render, redirect, get_object_or_404
from django.db.models import Case, When, Value, IntegerField, F
from .forms import ProdutoForm
from .models import Produto
from django.contrib import messages

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

        # CRIAR / EDITARgit
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
    produtos = Produto.objects.annotate(
    prioridade=Case(
        When(quantidade=0, then=Value(0)),
        When(quantidade__lte=F("estoque_minimo"), then=Value(1)),
        default=Value(2),
        output_field=IntegerField(),
    )
).order_by("prioridade", "-criado_em")
    if q:
        produtos = produtos.filter(
            Q(nome__icontains=q) |
            Q(codigo__icontains=q) |
            Q(categoria__icontains=q)
        )
    # ===== Cards Inteligentes =====

    total_produtos = produtos.count()

    valor_total = sum(p.preco * p.quantidade for p in produtos)

    zerado_count = produtos.filter(quantidade=0).count()

    limite_count = produtos.filter(
        quantidade=F("estoque_minimo"),
        quantidade__gt=0
    ).count()

    criticos_count = produtos.filter(
        quantidade__lt=F("estoque_minimo"),
        quantidade__gt=0
    ).count()

    normal_count = produtos.filter(
        quantidade__gt=F("estoque_minimo")
    ).count()



    return render(
        request,
        "estoque/home.html",
        {
            "produtos": produtos,
            "produto_edicao": produto_edicao,
            "form": form,
            "q": q,
            "total_produtos": total_produtos,
            "valor_total": valor_total,
            "zerado_count": zerado_count,
            "limite_count": limite_count,
            "criticos_count": criticos_count,
            "normal_count": normal_count,
        },
    )   


def cadastrar_produto(request):
    if request.method == "POST":
        form = ProdutoForm(request.POST)
        if form.is_valid():
            produto = form.save()
            messages.success(request, f'Produto "{produto.nome}" cadastrado com sucesso!')
            return redirect("estoque:home")
    else:
        form = ProdutoForm()

    return render(request, "estoque/cadastrar_produto.html", {"form": form})

def produto_detalhe(request, pk):
    produto = get_object_or_404(Produto, pk=pk)
    return render(request, "estoque/produto_detalhe.html", {"produto": produto})
def produto_editar(request, pk):
    produto = get_object_or_404(Produto, pk=pk)

    if request.method == "POST":
        form = ProdutoForm(request.POST, instance=produto)
        if form.is_valid():
            form.save()
            return redirect("estoque:home")
    else:
        form = ProdutoForm(instance=produto)

    return render(request, "estoque/cadastrar_produto.html", {"form": form})
