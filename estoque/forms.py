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
            "preco_vista",
            "preco_prazo",
            "unidade_compra",
            "fator_conversao",
            "preco_compra_fracionado",
            "unidade_venda_1",
            "preco_venda_1",
            "unidade_venda_2",
            "preco_venda_2",
            "vende_fracionado",
            "descricao_conversao",
            "quantidade",
            "estoque_minimo",
            "fornecedor",
        ]

        widgets = {
            "preco_compra": forms.NumberInput(attrs={"class": "form-control", "placeholder": ""}),
"preco_venda": forms.NumberInput(attrs={"class": "form-control", "placeholder": ""}),
            "nome": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "",
                    "required": True,
                    "autofocus": True,
                }
            ),
            "codigo": forms.TextInput(
                attrs={
                    "class": "form-control mono",
                    "placeholder": "",
                }
            ),
            "categoria": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "",
                }
            ),
            "preco_compra": forms.NumberInput(
                attrs={
                    "class": "form-control",
                    "step": "0.01",
                    "min": "0",
                    "placeholder": "",
                }
            ),
            "preco_vista": forms.NumberInput(
    attrs={
        "class": "form-control",
        "step": "0.01",
        "min": "0",
        "placeholder": "",
    }
),
"preco_prazo": forms.NumberInput(
    attrs={
        "class": "form-control",
        "step": "0.01",
        "min": "0",
        "placeholder": "",
    }
),
            "unidade_compra": forms.Select(
                attrs={
                    "class": "form-select",
                },
                choices=[
                    ("", "Selecione"),
                    ("UN", "UN"),
                    ("KG", "KG"),
                    ("CX", "CX"),
                    ("FD", "FD"),
                    ("PCT", "PCT"),
                    ("LT", "LT"),
                ],
            ),
            "fator_conversao": forms.NumberInput(
                attrs={
                    "class": "form-control",
                    "step": "0.01",
                    "min": "0",
                    "placeholder": "",
                }
            ),
            "preco_compra_fracionado": forms.NumberInput(
    attrs={
        "class": "form-control",
        "step": "0.01",
        "min": "0",
        "placeholder": "",
    }
),
            "unidade_venda_1": forms.Select(
                attrs={
                    "class": "form-select",
                },
                choices=[
                    ("", "Selecione"),
                    ("UN", "UN"),
                    ("KG", "KG"),
                    ("CX", "CX"),
                    ("FD", "FD"),
                    ("PCT", "PCT"),
                    ("LT", "LT"),
                ],
            ),
            "preco_venda_1": forms.NumberInput(
                attrs={
                    "class": "form-control",
                    "step": "0.01",
                    "min": "0",
                    "placeholder": "",
                }
            ),
            "unidade_venda_2": forms.Select(
                attrs={
                    "class": "form-select",
                },
                choices=[
                    ("", "Selecione"),
                    ("UN", "UN"),
                    ("KG", "KG"),
                    ("CX", "CX"),
                    ("FD", "FD"),
                    ("PCT", "PCT"),
                    ("LT", "LT"),
                ],
            ),
            "preco_venda_2": forms.NumberInput(
                attrs={
                    "class": "form-control",
                    "step": "0.01",
                    "min": "0",
                    "placeholder": "",
                }
            ),
            "vende_fracionado": forms.Select(
                attrs={
                    "class": "form-select",
                },
                choices=[
                    (False, "Não"),
                    (True, "Sim"),
                ],
            ),
            "descricao_conversao": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "Ex.: 1 fardo = 12 unidades",
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
                    "placeholder": "",
                }
            ),
        }
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if not self.instance or not self.instance.pk:
            self.fields["preco_compra"].initial = None
            self.fields["preco_vista"].initial = None
            self.fields["preco_prazo"].initial = None
            self.initial["preco_compra"] = ""
            self.initial["preco_vista"] = ""
            self.initial["preco_prazo"] = ""
            self.initial["unidade_compra"] = ""
            self.initial["fator_conversao"] = ""
            self.initial["unidade_venda_1"] = ""
            self.initial["preco_venda_1"] = ""
            self.initial["unidade_venda_2"] = ""
            self.initial["preco_venda_2"] = ""
            self.initial["vende_fracionado"] = False
            self.initial["descricao_conversao"] = ""
            
            self.initial["nome"] = ""
            self.initial["codigo"] = ""
            self.initial["categoria"] = ""
            self.initial["quantidade"] = ""
            self.initial["estoque_minimo"] = ""
            self.initial["fornecedor"] = ""
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