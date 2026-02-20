from django.shortcuts import get_object_or_404, redirect, render

from .forms import ProdutoForm
from .models import Produto


def home(request):
    if request.method == "POST":
        if request.POST.get("excluir_id"):
            Produto.objects.filter(id=request.POST.get("excluir_id")).delete()
            return redirect("home")

        form = ProdutoForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect("home")
    else:
        form = ProdutoForm()

    q = request.GET.get("q")
    if q:
        produtos = Produto.objects.filter(nome__icontains=q).order_by("-criado_em")
    else:
        produtos = Produto.objects.all().order_by("-criado_em")

    return render(
        request,
        "estoque/home.html",
        {
            "produtos": produtos,
            "form": form,
        },
    )


def produto_detalhe(request, pk):
    produto = get_object_or_404(Produto, pk=pk)
    return render(request, "estoque/produto_detalhe.html", {"produto": produto})


def produto_editar(request, pk):
    produto = get_object_or_404(Produto, pk=pk)

    if request.method == "POST":
        form = ProdutoForm(request.POST, instance=produto)
        if form.is_valid():
            form.save()
            return redirect("produto_detalhe", pk=produto.pk)
    else:
        form = ProdutoForm(instance=produto)

    return render(
        request,
        "estoque/produto_form.html",
        {
            "form": form,
            "produto": produto,
        },
    )
