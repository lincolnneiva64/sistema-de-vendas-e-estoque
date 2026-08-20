from django import forms

from .models import Categoria, Produto


class ProdutoRevisaoForm(forms.ModelForm):
    """Form para edicao em lote de produtos na revisao de importacao."""

    revisado = forms.BooleanField(
        required=False,
        label="Revisado",
        help_text="Marque para indicar que este produto foi revisado.",
    )

    class Meta:
        model = Produto
        fields = ["nome", "codigo", "categoria", "revisado"]
        widgets = {
            "nome": forms.TextInput(attrs={
                "class": "form-control form-control-sm",
                "placeholder": "Nome do produto",
            }),
            "codigo": forms.TextInput(attrs={
                "class": "form-control form-control-sm mono",
                "placeholder": "Codigo",
            }),
            "categoria": forms.Select(attrs={
                "class": "form-select form-select-sm",
            }),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["categoria"].choices = categorias_revisao_choices()


class ProdutoRevisaoFiltrosForm(forms.Form):
    """Form para filtros na tela de revisao."""

    FILTRO_CHOICES = [
        ("pendentes", "Pendentes"),
        ("revisados", "Revisados"),
        ("todos", "Todos"),
    ]

    filtro = forms.ChoiceField(
        choices=FILTRO_CHOICES,
        initial="pendentes",
        required=False,
        widget=forms.RadioSelect(attrs={"class": "form-check-input"}),
    )

    busca = forms.CharField(
        max_length=200,
        required=False,
        widget=forms.TextInput(attrs={
            "class": "form-control",
            "placeholder": "Buscar por nome...",
        }),
    )

    page = forms.IntegerField(
        min_value=1,
        required=False,
        initial=1,
        widget=forms.HiddenInput(),
    )


def categorias_revisao_choices():
    nomes = set(Categoria.objects.filter(ativa=True).values_list("nome", flat=True))
    nomes.update(
        Produto.objects.filter(excluido=False)
        .exclude(categoria__isnull=True)
        .exclude(categoria="")
        .values_list("categoria", flat=True)
    )
    escolhas = [("", "---")]
    escolhas.extend((nome, nome) for nome in sorted(nomes, key=str.casefold))
    return escolhas
