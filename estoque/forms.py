from django import forms

from .models import Categoria, Produto, Unidade
from .utils import normalize_category_name, normalize_product_name


class ProdutoForm(forms.ModelForm):
    factor_conversao = forms.DecimalField(
    required=False,
    widget=forms.NumberInput(attrs={
        "class": "form-control",
        "step": "0.01",
        "min": "0",
        "placeholder": "",
    })
)

    preco_compra_fracionado = forms.DecimalField(
        required=False,
        widget=forms.NumberInput(attrs={
            "class": "form-control",
            "step": "0.01",
            "min": "0",
            "placeholder": "",
        })
    )

    unidade_venda_2 = forms.ChoiceField(
        required=False,
        widget=forms.Select(attrs={
            "class": "form-select",
        })
    )
    fator_conversao = forms.DecimalField(required=False)
    percentual_vista_fracionado = forms.DecimalField(required=False)
    preco_vista_fracionado = forms.DecimalField(required=False)
    percentual_prazo_fracionado = forms.DecimalField(required=False)
    preco_prazo_fracionado = forms.DecimalField(required=False)

    class Meta:
        model = Produto
        fields = [
            "nome",
            "codigo",
            "categoria",
            "preco_compra",
            
            "unidade_compra",
            "fator_conversao",
            "preco_compra_fracionado",
            "unidade_venda_1",
            "preco_vista",
            "unidade_venda_2",
            "preco_prazo",
            "vende_fracionado",
            "descricao_conversao",
            "quantidade",
            "estoque_minimo",
            "fornecedor",
            "percentual_vista_fracionado",
            "preco_vista_fracionado",
            "percentual_prazo_fracionado",
            "preco_prazo_fracionado",
            
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
            "categoria": forms.Select(
                attrs={
                    "class": "form-select",
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
),            "preco_venda_2": forms.NumberInput(
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

        unidades_ativas = list(Unidade.objects.filter(ativa=True).order_by("sigla"))
        opcoes_unidade = [("", "Selecione")] + [
            (unidade.sigla, unidade.sigla)
            for unidade in unidades_ativas
        ]
        siglas_disponiveis = {unidade.sigla for unidade in unidades_ativas}

        for valor_atual in [
            getattr(self.instance, "unidade_compra", None),
            getattr(self.instance, "unidade_venda_1", None),
            getattr(self.instance, "unidade_venda_2", None),
        ]:
            if valor_atual and valor_atual not in siglas_disponiveis:
                opcoes_unidade.append((valor_atual, valor_atual))
                siglas_disponiveis.add(valor_atual)

        for field_name in ["unidade_compra", "unidade_venda_1", "unidade_venda_2"]:
            if field_name in self.fields:
                self.fields[field_name].choices = opcoes_unidade
                self.fields[field_name].widget.choices = opcoes_unidade

        categorias_ativas = list(Categoria.objects.filter(ativa=True).order_by("nome"))
        opcoes_categoria = [("", "Selecione")] + [
            (categoria.nome, categoria.nome)
            for categoria in categorias_ativas
        ]
        categorias_disponiveis = {categoria.nome for categoria in categorias_ativas}
        categoria_atual = getattr(self.instance, "categoria", None)

        if categoria_atual and categoria_atual not in categorias_disponiveis:
            opcoes_categoria.append((categoria_atual, categoria_atual))

        self.fields["categoria"].choices = opcoes_categoria
        self.fields["categoria"].widget.choices = opcoes_categoria
        self.fields["categoria"].required = False

        self.fields["nome"].widget.attrs.update({"list": "lista-produtos"})
        self.fields["fator_conversao"].required = False
        self.fields["preco_compra_fracionado"].required = False
        self.fields["unidade_venda_2"].required = False
        self.fields["percentual_vista_fracionado"].required = False
        self.fields["preco_vista_fracionado"].required = False
        self.fields["percentual_prazo_fracionado"].required = False
        self.fields["preco_prazo_fracionado"].required = False

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

    def clean(self):
        cleaned_data = super().clean()
        vende_fracionado_valor = cleaned_data.get("vende_fracionado")
        vende_fracionado = str(vende_fracionado_valor).strip().lower() in (
            "true",
            "1",
            "sim",
        )

        if not vende_fracionado:
            cleaned_data["fator_conversao"] = 0
            cleaned_data["preco_compra_fracionado"] = 0
            cleaned_data["unidade_venda_2"] = ""
            cleaned_data["percentual_vista_fracionado"] = 0
            cleaned_data["preco_vista_fracionado"] = 0
            cleaned_data["percentual_prazo_fracionado"] = 0
            cleaned_data["preco_prazo_fracionado"] = 0

            self.instance.fator_conversao = 0
            self.instance.preco_compra_fracionado = 0
            self.instance.unidade_venda_2 = ""
            self.instance.percentual_vista_fracionado = 0
            self.instance.preco_vista_fracionado = 0
            self.instance.percentual_prazo_fracionado = 0
            self.instance.preco_prazo_fracionado = 0

            self._errors.pop("fator_conversao", None)
            self._errors.pop("preco_compra_fracionado", None)
            self._errors.pop("unidade_venda_2", None)
            self._errors.pop("percentual_vista_fracionado", None)
            self._errors.pop("preco_vista_fracionado", None)
            self._errors.pop("percentual_prazo_fracionado", None)
            self._errors.pop("preco_prazo_fracionado", None)

        return cleaned_data


class UnidadeForm(forms.ModelForm):
    class Meta:
        model = Unidade
        fields = ["nome", "sigla", "descricao", "ativa"]
        widgets = {
            "nome": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "Ex.: Unidade",
                "autocomplete": "off",
            }),
            "sigla": forms.TextInput(attrs={
                "class": "form-control text-uppercase",
                "placeholder": "Ex.: UN",
                "autocomplete": "off",
                "maxlength": "20",
            }),
            "descricao": forms.Textarea(attrs={
                "class": "form-control",
                "placeholder": "Observação opcional sobre a unidade",
                "rows": 4,
            }),
            "ativa": forms.CheckboxInput(attrs={
                "class": "form-check-input",
            }),
        }

    def clean_nome(self):
        nome = " ".join((self.cleaned_data.get("nome") or "").strip().split())
        if not nome:
            raise forms.ValidationError("Informe o nome da unidade.")

        unidade_existente = Unidade.objects.filter(nome__iexact=nome)
        if self.instance.pk:
            unidade_existente = unidade_existente.exclude(pk=self.instance.pk)

        if unidade_existente.exists():
            raise forms.ValidationError("Já existe uma unidade com esse nome.")

        return nome.title()

    def clean_sigla(self):
        sigla = " ".join((self.cleaned_data.get("sigla") or "").strip().upper().split())
        if not sigla:
            raise forms.ValidationError("Informe a sigla da unidade.")

        unidade_existente = Unidade.objects.filter(sigla__iexact=sigla)
        if self.instance.pk:
            unidade_existente = unidade_existente.exclude(pk=self.instance.pk)

        if unidade_existente.exists():
            raise forms.ValidationError("Já existe uma unidade com essa sigla.")

        return sigla

    def clean_descricao(self):
        descricao = self.cleaned_data.get("descricao") or ""
        return " ".join(descricao.strip().split()) or None


class CategoriaForm(forms.ModelForm):
    class Meta:
        model = Categoria
        fields = ["nome", "descricao", "ativa"]
        widgets = {
            "nome": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "Ex.: Mercearia",
                "autocomplete": "off",
            }),
            "descricao": forms.Textarea(attrs={
                "class": "form-control",
                "placeholder": "Observação opcional sobre a categoria",
                "rows": 4,
            }),
            "ativa": forms.CheckboxInput(attrs={
                "class": "form-check-input",
            }),
        }

    def clean_nome(self):
        nome = " ".join((self.cleaned_data.get("nome") or "").strip().split())
        if not nome:
            raise forms.ValidationError("Informe o nome da categoria.")

        nome_normalizado = normalize_category_name(nome)
        categoria_existente = Categoria.objects.filter(nome__iexact=nome_normalizado)
        if self.instance.pk:
            categoria_existente = categoria_existente.exclude(pk=self.instance.pk)

        if categoria_existente.exists():
            raise forms.ValidationError("Já existe uma categoria com esse nome.")

        return nome_normalizado

    def clean_descricao(self):
        descricao = self.cleaned_data.get("descricao") or ""
        return " ".join(descricao.strip().split()) or None
