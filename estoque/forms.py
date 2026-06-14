from decimal import Decimal, InvalidOperation

from django import forms

from django.utils import timezone

from .models import Categoria, Cliente, Fornecedor, FornecedorContato, Funcionario, PixRecebido, Produto, Unidade
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

        if "unidade_venda_1" in self.fields:
            self.fields["unidade_venda_1"].required = False
            self.fields["unidade_venda_1"].widget = forms.HiddenInput()

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

        unidade_compra_valor = cleaned_data.get("unidade_compra") or ""
        unidade_compra_valor = str(unidade_compra_valor).strip()
        if unidade_compra_valor:
            cleaned_data["unidade_venda_1"] = unidade_compra_valor
            self.instance.unidade_venda_1 = unidade_compra_valor
        elif self.instance and self.instance.pk:
            cleaned_data["unidade_venda_1"] = getattr(self.instance, "unidade_venda_1", "")

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


class ClienteForm(forms.ModelForm):
    limite_credito = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={
            "class": "form-control clientes-input-moeda",
            "inputmode": "decimal",
            "autocomplete": "off",
            "placeholder": "",
        }),
    )

    class Meta:
        model = Cliente
        fields = [
            "nome",
            "apelido_nome_conhecido",
            "cpf_cnpj",
            "whatsapp",
            "telefone_alternativo",
            "email",
            "cep",
            "logradouro",
            "numero",
            "complemento",
            "bairro",
            "cidade",
            "uf",
            "referencia",
            "vende_a_prazo",
            "prazo_padrao_dias",
            "limite_credito",
            "limite_aberto",
            "status_credito",
            "observacao_financeira",
            "tipo_chave_pix",
            "chave_pix",
            "permite_contato_whatsapp",
            "nome_contato_whatsapp",
            "observacao_contato",
            "observacoes",
            "ativo",
        ]
        widgets = {
            "nome": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "Nome do cliente",
                "autocomplete": "off",
                "autofocus": True,
            }),
            "apelido_nome_conhecido": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "Apelido ou nome conhecido",
                "autocomplete": "off",
            }),
            "cpf_cnpj": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "CPF ou CNPJ",
                "autocomplete": "off",
            }),
            "whatsapp": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "(00) 00000-0000",
                "autocomplete": "off",
            }),
            "telefone_alternativo": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "Telefone alternativo",
                "autocomplete": "off",
            }),
            "email": forms.EmailInput(attrs={
                "class": "form-control",
                "placeholder": "email@exemplo.com",
                "autocomplete": "off",
            }),
            "cep": forms.TextInput(attrs={"class": "form-control", "placeholder": "CEP"}),
            "logradouro": forms.TextInput(attrs={"class": "form-control", "placeholder": "Rua, avenida, sitio"}),
            "numero": forms.TextInput(attrs={"class": "form-control", "placeholder": "Numero"}),
            "complemento": forms.TextInput(attrs={"class": "form-control", "placeholder": "Complemento"}),
            "bairro": forms.TextInput(attrs={"class": "form-control", "placeholder": "Bairro"}),
            "cidade": forms.TextInput(attrs={"class": "form-control", "placeholder": "Cidade"}),
            "uf": forms.TextInput(attrs={
                "class": "form-control text-uppercase",
                "placeholder": "UF",
                "maxlength": "2",
            }),
            "referencia": forms.TextInput(attrs={"class": "form-control", "placeholder": "Referencia"}),
            "vende_a_prazo": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "prazo_padrao_dias": forms.NumberInput(attrs={
                "class": "form-control",
                "min": "0",
                "step": "1",
                "placeholder": "",
            }),
            "limite_aberto": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "status_credito": forms.Select(attrs={"class": "form-select"}),
            "observacao_financeira": forms.Textarea(attrs={
                "class": "form-control",
                "rows": 3,
                "placeholder": "Observacoes financeiras",
            }),
            "tipo_chave_pix": forms.Select(attrs={"class": "form-select"}),
            "chave_pix": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "Chave Pix",
                "autocomplete": "off",
            }),
            "permite_contato_whatsapp": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "nome_contato_whatsapp": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "Nome do contato",
                "autocomplete": "off",
            }),
            "observacao_contato": forms.Textarea(attrs={
                "class": "form-control",
                "rows": 3,
                "placeholder": "Observacao de contato",
            }),
            "observacoes": forms.Textarea(attrs={
                "class": "form-control",
                "rows": 3,
                "placeholder": "Observacoes gerais",
            }),
            "ativo": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["prazo_padrao_dias"].required = False
        self.fields["limite_credito"].required = False

        if not self.is_bound and not self.instance.pk:
            self.fields["prazo_padrao_dias"].initial = None
            self.fields["limite_credito"].initial = None
            self.initial.pop("prazo_padrao_dias", None)
            self.initial.pop("limite_credito", None)

    def clean_nome(self):
        nome = " ".join((self.cleaned_data.get("nome") or "").strip().split())
        if not nome:
            raise forms.ValidationError("Informe o nome do cliente.")
        return nome

    def clean_prazo_padrao_dias(self):
        prazo = self.cleaned_data.get("prazo_padrao_dias") or 0
        if prazo < 0:
            raise forms.ValidationError("O prazo padrao nao pode ser negativo.")
        return prazo

    def clean_limite_credito(self):
        limite = self.cleaned_data.get("limite_credito")
        if limite in (None, ""):
            return 0
        if isinstance(limite, str):
            limite = limite.strip().replace("R$", "").replace(" ", "")
            if "," in limite:
                limite = limite.replace(".", "").replace(",", ".")
            elif "." in limite and len(limite.rsplit(".", 1)[-1]) == 3:
                limite = limite.replace(".", "")
            try:
                limite = Decimal(limite)
            except (InvalidOperation, ValueError):
                raise forms.ValidationError("Informe um limite de credito valido.")
        if limite < 0:
            raise forms.ValidationError("O limite de credito nao pode ser negativo.")
        return limite

    def clean_uf(self):
        uf = " ".join((self.cleaned_data.get("uf") or "").strip().upper().split())
        return uf or None


class FuncionarioForm(forms.ModelForm):
    class Meta:
        model = Funcionario
        fields = [
            "nome",
            "telefone_whatsapp",
            "pode_receber_checklist",
            "pode_operar_sistema",
            "observacoes",
            "ativo",
        ]
        widgets = {
            "nome": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "Nome do funcionario",
                "autocomplete": "off",
                "autofocus": True,
            }),
            "telefone_whatsapp": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "(00) 00000-0000",
                "autocomplete": "off",
            }),
            "pode_receber_checklist": forms.CheckboxInput(attrs={
                "class": "form-check-input",
            }),
            "pode_operar_sistema": forms.CheckboxInput(attrs={
                "class": "form-check-input",
            }),
            "observacoes": forms.Textarea(attrs={
                "class": "form-control",
                "rows": 3,
                "placeholder": "Observacoes internas",
            }),
            "ativo": forms.CheckboxInput(attrs={
                "class": "form-check-input",
            }),
        }

    def clean_nome(self):
        nome = " ".join((self.cleaned_data.get("nome") or "").strip().split())
        if not nome:
            raise forms.ValidationError("Informe o nome do funcionario.")
        return nome

    def clean_telefone_whatsapp(self):
        telefone = " ".join((self.cleaned_data.get("telefone_whatsapp") or "").strip().split())
        return telefone or None

    def clean_observacoes(self):
        observacoes = self.cleaned_data.get("observacoes") or ""
        return observacoes.strip() or None

    def clean(self):
        cleaned_data = super().clean()
        telefone = Funcionario.normalizar_whatsapp(cleaned_data.get("telefone_whatsapp"))
        pode_receber_checklist = cleaned_data.get("pode_receber_checklist")
        pode_operar_sistema = cleaned_data.get("pode_operar_sistema")
        ativo = cleaned_data.get("ativo")

        if pode_receber_checklist and not telefone:
            self.add_error(
                "telefone_whatsapp",
                "Informe o WhatsApp para permitir envio de checklist.",
            )

        if pode_receber_checklist and ativo is False:
            cleaned_data["pode_receber_checklist"] = False
        if pode_operar_sistema and ativo is False:
            cleaned_data["pode_operar_sistema"] = False

        return cleaned_data


class PixRecebidoForm(forms.ModelForm):
    data_pagamento = forms.DateTimeField(
        input_formats=["%Y-%m-%dT%H:%M"],
        widget=forms.DateTimeInput(
            attrs={
                "class": "form-control",
                "type": "datetime-local",
            },
            format="%Y-%m-%dT%H:%M",
        ),
    )

    class Meta:
        model = PixRecebido
        fields = [
            "cliente",
            "nome_pagador",
            "valor",
            "data_pagamento",
            "observacao",
            "comprovante",
            "status",
        ]
        widgets = {
            "cliente": forms.Select(attrs={"class": "form-select"}),
            "nome_pagador": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "Nome de quem fez o Pix",
                "autocomplete": "off",
            }),
            "valor": forms.NumberInput(attrs={
                "class": "form-control",
                "step": "0.01",
                "min": "0.01",
                "placeholder": "0,00",
            }),
            "observacao": forms.Textarea(attrs={
                "class": "form-control",
                "rows": 3,
                "placeholder": "Observacao interna opcional",
            }),
            "comprovante": forms.ClearableFileInput(attrs={"class": "form-control"}),
            "status": forms.Select(attrs={"class": "form-select"}),
        }
        labels = {
            "cliente": "Cliente",
            "nome_pagador": "Nome do pagador",
            "valor": "Valor do Pix",
            "data_pagamento": "Data/hora do pagamento",
            "observacao": "Observacao",
            "comprovante": "Comprovante/anexo",
            "status": "Status",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["cliente"].required = False
        self.fields["cliente"].queryset = Cliente.objects.filter(ativo=True).order_by("nome")
        self.fields["cliente"].empty_label = "Sem cliente identificado"
        self.fields["nome_pagador"].required = False
        self.fields["comprovante"].required = False
        self.fields["observacao"].required = False
        if not self.is_bound and not self.instance.pk:
            self.initial["data_pagamento"] = timezone.localtime(timezone.now()).strftime("%Y-%m-%dT%H:%M")
            self.initial["status"] = PixRecebido.STATUS_PENDENTE

    def clean_nome_pagador(self):
        nome = self.cleaned_data.get("nome_pagador") or ""
        return " ".join(nome.strip().split())

    def clean_valor(self):
        valor = self.cleaned_data.get("valor")
        if valor is None or valor <= 0:
            raise forms.ValidationError("Informe o valor do Pix antes de salvar.")
        return valor

    def clean_cliente(self):
        cliente = self.cleaned_data.get("cliente")
        return cliente

    def clean_observacao(self):
        observacao = self.cleaned_data.get("observacao") or ""
        return observacao.strip()


class PixRecebidoCorrecaoForm(forms.Form):
    cliente = forms.ModelChoiceField(
        queryset=Cliente.objects.none(),
        required=False,
        empty_label="Sem cliente confirmado",
        label="Cliente confirmado",
        widget=forms.Select(attrs={"class": "pix-correction-input"}),
    )
    nome_pagador = forms.CharField(
        required=False,
        max_length=160,
        label="Pagador",
        widget=forms.TextInput(attrs={
            "class": "pix-correction-input",
            "autocomplete": "off",
            "placeholder": "Nome lido ou corrigido do pagador",
        }),
    )
    valor = forms.CharField(
        required=False,
        label="Valor",
        widget=forms.TextInput(attrs={
            "class": "pix-correction-input",
            "autocomplete": "off",
            "inputmode": "decimal",
            "placeholder": "Valor do Pix atual",
        }),
    )
    data_pagamento = forms.DateTimeField(
        required=False,
        input_formats=["%Y-%m-%dT%H:%M"],
        label="Data do pagamento",
        widget=forms.DateTimeInput(
            attrs={"class": "pix-correction-input", "type": "datetime-local", "autocomplete": "off"},
            format="%Y-%m-%dT%H:%M",
        ),
    )
    instituicao_pix = forms.CharField(
        required=False,
        max_length=80,
        label="Banco/instituicao",
        widget=forms.TextInput(attrs={
            "class": "pix-correction-input",
            "autocomplete": "off",
            "placeholder": "Ex.: Nubank",
        }),
    )

    def __init__(self, *args, pix=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.pix = pix
        self.fields["cliente"].queryset = Cliente.objects.filter(ativo=True).order_by("nome")
        if pix and not self.is_bound:
            self.initial.update({
                "cliente": pix.cliente_id,
                "nome_pagador": pix.nome_pagador,
                "valor": str(pix.valor or "").replace(".", ","),
                "data_pagamento": timezone.localtime(pix.data_pagamento).strftime("%Y-%m-%dT%H:%M")
                if pix.data_pagamento
                else "",
                "instituicao_pix": pix.instituicao_pix,
            })

    def clean_nome_pagador(self):
        nome = self.cleaned_data.get("nome_pagador") or ""
        return " ".join(nome.strip().split())[:160]

    def clean_valor(self):
        valor = self.cleaned_data.get("valor")
        if valor in (None, ""):
            return self.pix.valor if self.pix else Decimal("0.00")

        texto = str(valor).strip().replace("R$", "").replace(" ", "")
        if "," in texto and "." in texto:
            texto = texto.replace(".", "").replace(",", ".")
        else:
            texto = texto.replace(",", ".")

        try:
            valor_decimal = Decimal(texto).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError):
            raise forms.ValidationError("Informe um valor valido, como 650,00.")

        if valor_decimal < Decimal("0.00"):
            raise forms.ValidationError("Informe um valor de Pix valido.")
        return valor_decimal

class FornecedorForm(forms.ModelForm):
    class Meta:
        model = Fornecedor
        fields = [
            "nome",
            "nome_fantasia",
            "telefone_whatsapp",
            "cidade",
            "bairro",
            "prazos_pagamento_padrao",
            "observacao",
            "ativo",
        ]
        widgets = {
            "nome": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "Nome do fornecedor",
                "autocomplete": "off",
                "onkeydown": "return fornecedorEnterAvanca(event);",
                "autofocus": True,
            }),
            "nome_fantasia": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "Nome fantasia / apelido",
                "autocomplete": "off",
                "onkeydown": "return fornecedorEnterAvanca(event);",
            }),
            "telefone_whatsapp": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "(00) 00000-0000",
                "autocomplete": "off",
                "onkeydown": "return fornecedorEnterAvanca(event);",
            }),
            "cidade": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "Cidade",
                "autocomplete": "off",
                "onkeydown": "return fornecedorEnterAvanca(event);",
            }),
            "bairro": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "Bairro",
                "autocomplete": "off",
                "onkeydown": "return fornecedorEnterAvanca(event);",
            }),
            "prazos_pagamento_padrao": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "Ex.: 7, 14, 21",
                "autocomplete": "off",
                "onkeydown": "return fornecedorEnterAvanca(event);",
            }),
            "observacao": forms.Textarea(attrs={
                "class": "form-control",
                "placeholder": "Observacoes sobre o fornecedor",
                "onkeydown": "return fornecedorEnterAvanca(event);",
                "rows": 3,
            }),
            "ativo": forms.CheckboxInput(attrs={
                "class": "form-check-input",
            }),
        }

    @staticmethod
    def _nome_proprio(valor):
        valor = (valor or "").strip()
        if not valor:
            return valor
        return " ".join(parte[:1].upper() + parte[1:].lower() for parte in valor.split())

    def clean_nome(self):
        nome = self._nome_proprio(self.cleaned_data.get("nome"))
        if not nome:
            return nome

        duplicados = Fornecedor.objects.filter(nome__iexact=nome)
        if self.instance and self.instance.pk:
            duplicados = duplicados.exclude(pk=self.instance.pk)

        if duplicados.exists():
            raise forms.ValidationError("Ja existe um fornecedor cadastrado com esse nome.")

        return nome

    def clean_nome_fantasia(self):
        return self._nome_proprio(self.cleaned_data.get("nome_fantasia"))

    def clean_cidade(self):
        return self._nome_proprio(self.cleaned_data.get("cidade"))

    def clean_bairro(self):
        return self._nome_proprio(self.cleaned_data.get("bairro"))



    def clean_prazos_pagamento_padrao(self):
        valor = (self.cleaned_data.get("prazos_pagamento_padrao") or "").strip()
        if not valor:
            return None

        partes = [parte.strip() for parte in valor.replace(";", ",").split(",") if parte.strip()]
        dias = []
        for parte in partes:
            if not parte.isdigit():
                raise forms.ValidationError("Informe apenas dias separados por virgula. Exemplo: 7, 14, 21")
            dia = int(parte)
            if dia <= 0:
                raise forms.ValidationError("Os dias de prazo devem ser maiores que zero.")
            dias.append(str(dia))

        return ", ".join(dias)


    def clean_dia_vencimento_cartao(self):
        dia = self.cleaned_data.get("dia_vencimento_cartao")
        if dia in (None, ""):
            return None
        if dia < 1 or dia > 31:
            raise forms.ValidationError("Informe um dia entre 1 e 31.")
        return dia



class FornecedorContatoForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance.pk:
            self.fields["ativo"].initial = True

    class Meta:
        model = FornecedorContato
        fields = [
            "nome",
            "cargo",
            "telefone_whatsapp",
            "principal",
            "ativo",
            "observacao",
        ]
        widgets = {
            "nome": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "Nome do responsavel",
                "autocomplete": "off",
            }),
            "cargo": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "Vendedor, financeiro, entrega...",
                "autocomplete": "off",
            }),
            "telefone_whatsapp": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "(00) 00000-0000",
                "autocomplete": "off",
            }),
            "principal": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "ativo": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "observacao": forms.Textarea(attrs={
                "class": "form-control",
                "placeholder": "Observacao do contato",
                "rows": 2,
            }),
        }

    @staticmethod
    def _limpar_texto(valor):
        valor = (valor or "").strip()
        return " ".join(valor.split()) or None

    def clean_nome(self):
        return self._limpar_texto(self.cleaned_data.get("nome"))

    def clean_cargo(self):
        return self._limpar_texto(self.cleaned_data.get("cargo"))

    def clean_telefone_whatsapp(self):
        return self._limpar_texto(self.cleaned_data.get("telefone_whatsapp"))


FornecedorContatoFormSet = forms.inlineformset_factory(
    Fornecedor,
    FornecedorContato,
    form=FornecedorContatoForm,
    extra=3,
    can_delete=True,
)

