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
            "estoque_minimo",
            "fornecedor",
        ]
        widgets = {
            "nome": forms.TextInput(attrs={"class": "form-control", "placeholder": "Ex.: Arroz 5kg"}),
            "codigo": forms.TextInput(attrs={"class": "form-control", "placeholder": "Ex.: 789..."}),
            "categoria": forms.TextInput(attrs={"class": "form-control", "placeholder": "Ex.: Grãos"}),
            "preco": forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": "0"}),
            "quantidade": forms.NumberInput(attrs={"class": "form-control", "step": "1", "min": "0"}),
            "estoque_minimo": forms.NumberInput(attrs={"class": "form-control", "step": "1", "min": "0"}),
            "fornecedor": forms.TextInput(attrs={"class": "form-control", "placeholder": "Ex.: Atacadista X"}),
        }
