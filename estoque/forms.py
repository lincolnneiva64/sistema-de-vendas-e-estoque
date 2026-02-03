from django import forms

from .models import Produto


class ProdutoForm(forms.ModelForm):
    class Meta:
        model = Produto
        fields = [
            "nome",
            "codigo",
            "categoria",
            "preco",
            "quantidade",
            "fornecedor",
        ]
        widgets = {
            "nome": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "Ex.: Arroz 5kg",
                    "required": True,
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
            "preco": forms.NumberInput(
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
                    "placeholder": "Ex.: 20",
                }
            ),
            "fornecedor": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "Ex.: Atacadista X",
                }
            ),
        }
