from django import forms

from .models import Produto
from .utils import normalize_product_name


class ProdutoForm(forms.ModelForm):
    class Meta:
        model = Produto
        fields = [
            "nome",
            "codigo",
            "categoria",
            "preco_compra",
            "preco_venda",
            "quantidade",
            "estoque_minimo",
            "fornecedor",
        ]

        widgets = {
            "nome": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "Ex.: Arroz 5kg",
                    "required": True,
                    "autofocus": True,
                }
            ),
            "codigo": forms.TextInput(
                attrs={
                    "class": "form-control mono",
                    "placeholder": "Ex.: 789...",
                }
            ),
            "categoria": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "Ex.: Grãos",
                }
            ),
            "preco_compra": forms.NumberInput(
                attrs={
                    "class": "form-control",
                    "step": "0.01",
                    "min": "0",
                    "placeholder": "Ex.: 10.00",
                }
            ),
            "preco_venda": forms.NumberInput(
                attrs={
                    "class": "form-control",
                    "step": "0.01",
                    "min": "0",
                    "placeholder": "Ex.: 19.90",
                }
            ),
            "quantidade": forms.NumberInput(
                attrs={
                    "class": "form-control",
                    "step": "1",
                    "min": "0",
                }
            ),
            "estoque_minimo": forms.NumberInput(
                attrs={
                    "class": "form-control",
                    "step": "1",
                    "min": "0",
                }
            ),
            "fornecedor": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "Ex.: Atacadista X",
                }
            ),
        }

    def clean_nome(self):
        nome = normalize_product_name(self.cleaned_data.get("nome", ""))

        if not nome:
            raise forms.ValidationError("Informe o nome do produto.")

        produtos = Produto.objects.exclude(
            pk=getattr(self.instance, "pk", None)
        ).only("nome")
        nome_key = nome.casefold()

        for produto in produtos:
            if normalize_product_name(produto.nome).casefold() == nome_key:
                raise forms.ValidationError("Já existe um produto com esse nome.")

        return nome