from django import forms

from estoque.models import Cliente

from .models import (
    ConferenciaEntregaLocacao,
    ConferenciaRecolhimentoLocacao,
    ConfiguracaoLocacao,
    FaixaPrecoLocacao,
    ItemLocacao,
    Locacao,
    MovimentoEstoqueLocacao,
    PagamentoLocacao,
)


class ConfiguracaoLocacaoForm(forms.ModelForm):
    class Meta:
        model = ConfiguracaoLocacao
        fields = [
            "total_mesas",
            "total_cadeiras",
            "preco_mesa_avulsa_diaria",
            "preco_cadeira_avulsa_diaria",
            "valor_reposicao_cadeira",
            "valor_reposicao_mesa",
        ]
        widgets = {
            "total_mesas": forms.NumberInput(attrs={"class": "form-control", "min": "0"}),
            "total_cadeiras": forms.NumberInput(attrs={"class": "form-control", "min": "0"}),
            "preco_mesa_avulsa_diaria": forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "0.01"}),
            "preco_cadeira_avulsa_diaria": forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "0.01"}),
            "valor_reposicao_cadeira": forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "0.01"}),
            "valor_reposicao_mesa": forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "0.01"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for campo in ["total_mesas", "total_cadeiras"]:
            if self.instance.pk and getattr(self.instance, campo) is not None:
                self.fields[campo].disabled = True
                self.fields[campo].help_text = "Saldo bloqueado. Use Registrar movimentacao."


class FaixaPrecoLocacaoForm(forms.ModelForm):
    class Meta:
        model = FaixaPrecoLocacao
        fields = ["preco_jogo_diaria"]
        widgets = {
            "preco_jogo_diaria": forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "0.01"}),
        }


class MovimentoEstoqueLocacaoForm(forms.Form):
    item = forms.ChoiceField(
        choices=MovimentoEstoqueLocacao.ITEM_CHOICES,
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    tipo = forms.ChoiceField(
        choices=MovimentoEstoqueLocacao.TIPO_CHOICES,
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    quantidade = forms.IntegerField(
        required=False,
        min_value=1,
        widget=forms.NumberInput(attrs={"class": "form-control", "min": "1", "step": "1"}),
    )
    saldo_contado = forms.IntegerField(
        required=False,
        min_value=0,
        widget=forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1"}),
    )
    responsavel = forms.CharField(
        max_length=120,
        widget=forms.TextInput(attrs={"class": "form-control", "autocomplete": "off"}),
    )
    observacao = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 3}),
    )

    def clean(self):
        cleaned_data = super().clean()
        tipo = cleaned_data.get("tipo")
        quantidade = cleaned_data.get("quantidade")
        saldo_contado = cleaned_data.get("saldo_contado")
        observacao = (cleaned_data.get("observacao") or "").strip()

        if tipo == MovimentoEstoqueLocacao.TIPO_AJUSTE_INVENTARIO:
            if saldo_contado is None:
                self.add_error("saldo_contado", "Informe o saldo contado no inventario.")
            if not observacao:
                self.add_error("observacao", "Informe o motivo do ajuste de inventario.")
            cleaned_data["quantidade"] = 0
        elif quantidade is None:
            self.add_error("quantidade", "Informe a quantidade movimentada.")

        cleaned_data["observacao"] = observacao
        return cleaned_data


class LocacaoForm(forms.Form):
    tipo_pessoa = forms.ChoiceField(
        choices=Locacao.TIPO_PESSOA_CHOICES,
        initial=Locacao.TIPO_PESSOA_CLIENTE,
        widget=forms.Select(
            attrs={
                "class": "form-select",
                "autofocus": True,
            }
        ),
    )
    cliente = forms.ModelChoiceField(
        queryset=Cliente.objects.filter(ativo=True).order_by("nome"),
        required=False,
        widget=forms.Select(
            attrs={
                "class": "form-select",
            }
        ),
    )
    pessoa_avulsa_nome = forms.CharField(
        required=False,
        max_length=160,
        widget=forms.TextInput(attrs={"class": "form-control", "autocomplete": "off"}),
    )
    pessoa_avulsa_telefone = forms.CharField(
        required=False,
        max_length=40,
        widget=forms.TextInput(attrs={"class": "form-control", "autocomplete": "off"}),
    )
    endereco_entrega = forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 2,
                "data-enter-next": "id_faixa_preco",
            }
        ),
    )
    data_entrega = forms.DateField(widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}))
    horario_entrega = forms.TimeField(widget=forms.TimeInput(attrs={"class": "form-control", "type": "time"}))
    data_evento = forms.DateField(widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}))
    horario_evento = forms.TimeField(widget=forms.TimeInput(attrs={"class": "form-control", "type": "time"}))
    data_prevista_devolucao = forms.DateField(
        widget=forms.DateInput(attrs={"class": "form-control", "type": "date", "data-enter-next": "id_jogos"})
    )
    data_vencimento_saldo = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}),
    )
    faixa_preco = forms.ModelChoiceField(
        queryset=FaixaPrecoLocacao.objects.filter(ativa=True).order_by("ordem", "id"),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    observacao = forms.CharField(required=False, widget=forms.Textarea(attrs={"class": "form-control", "rows": 3}))
    sinal_valor = forms.DecimalField(
        required=False,
        min_value=0,
        decimal_places=2,
        max_digits=12,
        widget=forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "0.01"}),
    )
    sinal_forma_pagamento = forms.ChoiceField(
        required=False,
        choices=[("", "Sem sinal"), *PagamentoLocacao.FORMA_CHOICES],
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    sinal_observacao = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 2}),
    )

    def clean(self):
        cleaned_data = super().clean()
        tipo_pessoa = cleaned_data.get("tipo_pessoa")
        cliente = cleaned_data.get("cliente")
        pessoa_avulsa_nome = (cleaned_data.get("pessoa_avulsa_nome") or "").strip()
        pessoa_avulsa_telefone = (cleaned_data.get("pessoa_avulsa_telefone") or "").strip()
        data_entrega = cleaned_data.get("data_entrega")
        data_prevista_devolucao = cleaned_data.get("data_prevista_devolucao")
        data_vencimento_saldo = cleaned_data.get("data_vencimento_saldo")
        data_evento = cleaned_data.get("data_evento")
        sinal_valor = cleaned_data.get("sinal_valor")
        sinal_forma = cleaned_data.get("sinal_forma_pagamento")

        if tipo_pessoa == Locacao.TIPO_PESSOA_CLIENTE and not cliente:
            self.add_error("cliente", "Selecione o cliente cadastrado.")
        if tipo_pessoa == Locacao.TIPO_PESSOA_AVULSA:
            if not pessoa_avulsa_nome:
                self.add_error("pessoa_avulsa_nome", "Informe o nome da pessoa avulsa.")
            if not pessoa_avulsa_telefone:
                self.add_error("pessoa_avulsa_telefone", "Informe o telefone da pessoa avulsa.")
            cleaned_data["cliente"] = None

        if data_entrega and data_prevista_devolucao and data_prevista_devolucao < data_entrega:
            self.add_error("data_prevista_devolucao", "A devolucao prevista nao pode ser anterior a entrega.")
        if data_entrega and data_evento and data_evento < data_entrega:
            self.add_error("data_evento", "A data do evento nao pode ser anterior a entrega.")
        if not data_vencimento_saldo and data_entrega:
            cleaned_data["data_vencimento_saldo"] = data_entrega
        if sinal_valor and sinal_valor > 0 and not sinal_forma:
            self.add_error("sinal_forma_pagamento", "Informe a forma de pagamento do sinal.")

        cleaned_data["pessoa_avulsa_nome"] = pessoa_avulsa_nome
        cleaned_data["pessoa_avulsa_telefone"] = pessoa_avulsa_telefone
        endereco_entrega = (
            cleaned_data.get("endereco_entrega") or ""
        ).strip()

        if not endereco_entrega:
            self.add_error(
                "endereco_entrega",
                "Informe o endereco da entrega.",
            )

        cleaned_data["endereco_entrega"] = endereco_entrega
        cleaned_data["observacao"] = (cleaned_data.get("observacao") or "").strip()
        cleaned_data["sinal_observacao"] = (cleaned_data.get("sinal_observacao") or "").strip()
        return cleaned_data


class ItensLocacaoReservaForm(forms.Form):
    jogos = forms.IntegerField(
        required=False,
        min_value=0,
        initial=0,
        widget=forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1"}),
    )
    preco_jogo_diaria = forms.DecimalField(
        required=False,
        min_value=0,
        decimal_places=2,
        max_digits=10,
        widget=forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "0.01"}),
    )
    mesas_avulsas = forms.IntegerField(
        required=False,
        min_value=0,
        initial=0,
        widget=forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1"}),
    )
    preco_mesa_avulsa_diaria = forms.DecimalField(
        required=False,
        min_value=0,
        decimal_places=2,
        max_digits=10,
        widget=forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "0.01"}),
    )
    cadeiras_avulsas = forms.IntegerField(
        required=False,
        min_value=0,
        initial=0,
        widget=forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1"}),
    )
    preco_cadeira_avulsa_diaria = forms.DecimalField(
        required=False,
        min_value=0,
        decimal_places=2,
        max_digits=10,
        widget=forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "0.01"}),
    )

    def __init__(self, *args, faixa_preco=None, configuracao=None, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            if faixa_preco:
                self.fields["preco_jogo_diaria"].initial = faixa_preco.preco_jogo_diaria
            if configuracao:
                self.fields["preco_mesa_avulsa_diaria"].initial = configuracao.preco_mesa_avulsa_diaria
                self.fields["preco_cadeira_avulsa_diaria"].initial = configuracao.preco_cadeira_avulsa_diaria

    def itens(self, faixa_preco, configuracao):
        dados = self.cleaned_data
        referencia = {
            "preco_jogo_diaria": faixa_preco.preco_jogo_diaria,
            "preco_mesa_avulsa_diaria": configuracao.preco_mesa_avulsa_diaria,
            "preco_cadeira_avulsa_diaria": configuracao.preco_cadeira_avulsa_diaria,
        }
        itens = []
        mapa = [
            ("jogos", "preco_jogo_diaria", ItemLocacao.TIPO_JOGO),
            ("mesas_avulsas", "preco_mesa_avulsa_diaria", ItemLocacao.TIPO_MESA_AVULSA),
            ("cadeiras_avulsas", "preco_cadeira_avulsa_diaria", ItemLocacao.TIPO_CADEIRA_AVULSA),
        ]
        for campo_qtd, campo_preco, tipo in mapa:
            quantidade = int(dados.get(campo_qtd) or 0)
            if quantidade <= 0:
                continue
            preco = dados.get(campo_preco)
            if preco is None:
                preco = referencia[campo_preco]
            itens.append({
                "tipo": tipo,
                "quantidade": quantidade,
                "preco_diaria": preco,
                "ajuste_manual": preco != referencia[campo_preco],
            })
        return itens


class CancelarLocacaoForm(forms.Form):
    motivo = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 3}),
    )


class PagamentoLocacaoForm(forms.Form):
    valor = forms.DecimalField(
        min_value=0,
        decimal_places=2,
        max_digits=12,
        widget=forms.NumberInput(attrs={"class": "form-control", "min": "0.01", "step": "0.01"}),
    )
    data_hora = forms.DateTimeField(
        required=False,
        widget=forms.DateTimeInput(attrs={"class": "form-control", "type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
        input_formats=["%Y-%m-%dT%H:%M"],
    )
    forma_pagamento = forms.ChoiceField(
        choices=PagamentoLocacao.FORMA_CHOICES,
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    responsavel = forms.CharField(
        required=False,
        max_length=120,
        widget=forms.TextInput(attrs={"class": "form-control", "autocomplete": "off"}),
    )
    observacao = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 2}),
    )

    def clean_valor(self):
        valor = self.cleaned_data["valor"]
        if valor <= 0:
            raise forms.ValidationError("Informe um valor maior que zero.")
        return valor


class VencimentoSaldoLocacaoForm(forms.Form):
    data_vencimento_saldo = forms.DateField(
        widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}),
    )
    responsavel = forms.CharField(
        required=False,
        max_length=120,
        widget=forms.TextInput(attrs={"class": "form-control", "autocomplete": "off"}),
    )
    observacao = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 2}),
    )


class ReciboStatusForm(forms.Form):
    responsavel = forms.CharField(
        required=False,
        max_length=120,
        widget=forms.TextInput(attrs={"class": "form-control", "autocomplete": "off"}),
    )
    observacao = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 2}),
    )


class TermoLocacaoForm(forms.Form):
    responsavel = forms.CharField(
        required=False,
        max_length=120,
        widget=forms.TextInput(attrs={"class": "form-control", "autocomplete": "off"}),
    )


class AcaoOperacionalLocacaoForm(forms.Form):
    responsavel = forms.CharField(
        required=False,
        max_length=120,
        widget=forms.TextInput(attrs={"class": "form-control", "autocomplete": "off"}),
    )
    observacao = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 2}),
    )


class ConferenciaEntregaLocacaoForm(forms.Form):
    entregue_mesas = forms.IntegerField(
        min_value=0,
        initial=0,
        label="Mesas entregues agora",
        widget=forms.NumberInput(
            attrs={
                "class": "form-control",
                "min": "0",
                "step": "1",
            }
        ),
    )
    entregue_cadeiras = forms.IntegerField(
        min_value=0,
        initial=0,
        label="Cadeiras entregues agora",
        widget=forms.NumberInput(
            attrs={
                "class": "form-control",
                "min": "0",
                "step": "1",
            }
        ),
    )
    recebedor_nome = forms.CharField(
        max_length=160,
        label="Quem recebeu",
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "autocomplete": "off",
            }
        ),
    )
    recebedor_relacao = forms.ChoiceField(
        choices=ConferenciaEntregaLocacao.RELACAO_CHOICES,
        label="Relacao com o cliente",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    recebedor_relacao_outro = forms.CharField(
        required=False,
        max_length=120,
        label="Qual e a relacao",
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "autocomplete": "off",
            }
        ),
    )
    estado_material = forms.ChoiceField(
        choices=ConferenciaEntregaLocacao.ESTADO_CHOICES,
        label="Estado do material",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    justificativa_parcial = forms.CharField(
        required=False,
        label="Justificativa da entrega parcial",
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 3,
            }
        ),
    )
    previsao_conclusao = forms.DateTimeField(
        required=False,
        label="Previsao para completar",
        input_formats=["%Y-%m-%dT%H:%M"],
        widget=forms.DateTimeInput(
            format="%Y-%m-%dT%H:%M",
            attrs={
                "class": "form-control",
                "type": "datetime-local",
            },
        ),
    )
    observacao = forms.CharField(
        required=False,
        label="Observacao",
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 3,
            }
        ),
    )
    responsavel = forms.CharField(
        max_length=120,
        label="Funcionario responsavel",
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "autocomplete": "off",
            }
        ),
    )

    def __init__(self, *args, locacao, **kwargs):
        super().__init__(*args, **kwargs)
        self.locacao = locacao
        self._dados_calculados = None

        previsto = Locacao.necessidades_itens(
            [
                {
                    "tipo": item.tipo,
                    "quantidade": item.quantidade,
                }
                for item in locacao.itens.all()
            ]
        )
        acumulado_mesas = sum(
            conferencia.entregue_mesas
            for conferencia in locacao.conferencias_entrega.all()
        )
        acumulado_cadeiras = sum(
            conferencia.entregue_cadeiras
            for conferencia in locacao.conferencias_entrega.all()
        )

        self.previsto_mesas = previsto["mesas"]
        self.previsto_cadeiras = previsto["cadeiras"]
        self.acumulado_mesas = acumulado_mesas
        self.acumulado_cadeiras = acumulado_cadeiras
        self.pendente_mesas = max(
            self.previsto_mesas - self.acumulado_mesas,
            0,
        )
        self.pendente_cadeiras = max(
            self.previsto_cadeiras - self.acumulado_cadeiras,
            0,
        )

        if not self.is_bound:
            self.fields["entregue_mesas"].initial = self.pendente_mesas
            self.fields["entregue_cadeiras"].initial = (
                self.pendente_cadeiras
            )

    def clean(self):
        cleaned_data = super().clean()

        entregue_mesas = cleaned_data.get("entregue_mesas")
        entregue_cadeiras = cleaned_data.get("entregue_cadeiras")

        entregue_mesas = (
            int(entregue_mesas)
            if entregue_mesas is not None
            else 0
        )
        entregue_cadeiras = (
            int(entregue_cadeiras)
            if entregue_cadeiras is not None
            else 0
        )

        if entregue_mesas == 0 and entregue_cadeiras == 0:
            self.add_error(
                "entregue_mesas",
                "Informe pelo menos uma mesa ou cadeira entregue.",
            )

        if entregue_mesas > self.pendente_mesas:
            self.add_error(
                "entregue_mesas",
                (
                    "A quantidade entregue esta maior que a prevista "
                    "no termo. Corrija a contagem e traga o excedente "
                    "de volta ou regularize o material adicional em "
                    "um ajuste da locacao."
                ),
            )

        if entregue_cadeiras > self.pendente_cadeiras:
            self.add_error(
                "entregue_cadeiras",
                (
                    "A quantidade entregue esta maior que a prevista "
                    "no termo. Corrija a contagem e traga o excedente "
                    "de volta ou regularize o material adicional em "
                    "um ajuste da locacao."
                ),
            )

        novo_acumulado_mesas = (
            self.acumulado_mesas + entregue_mesas
        )
        novo_acumulado_cadeiras = (
            self.acumulado_cadeiras + entregue_cadeiras
        )
        novo_pendente_mesas = max(
            self.previsto_mesas - novo_acumulado_mesas,
            0,
        )
        novo_pendente_cadeiras = max(
            self.previsto_cadeiras - novo_acumulado_cadeiras,
            0,
        )

        entrega_parcial = (
            novo_pendente_mesas > 0
            or novo_pendente_cadeiras > 0
        )

        justificativa = (
            cleaned_data.get("justificativa_parcial") or ""
        ).strip()
        previsao = cleaned_data.get("previsao_conclusao")
        relacao = cleaned_data.get("recebedor_relacao")
        relacao_outro = (
            cleaned_data.get("recebedor_relacao_outro") or ""
        ).strip()
        estado = cleaned_data.get("estado_material")
        observacao = (
            cleaned_data.get("observacao") or ""
        ).strip()

        if entrega_parcial:
            if not justificativa:
                self.add_error(
                    "justificativa_parcial",
                    (
                        "Informe a justificativa da entrega "
                        "parcial."
                    ),
                )
            if not previsao:
                self.add_error(
                    "previsao_conclusao",
                    (
                        "Informe a previsao para completar "
                        "a entrega."
                    ),
                )

        if (
            relacao
            == ConferenciaEntregaLocacao.RELACAO_OUTRO
            and not relacao_outro
        ):
            self.add_error(
                "recebedor_relacao_outro",
                "Informe a relacao da pessoa que recebeu.",
            )

        if (
            estado
            == ConferenciaEntregaLocacao.ESTADO_RESSALVA
            and not observacao
        ):
            self.add_error(
                "observacao",
                (
                    "Explique a ressalva sobre o estado "
                    "do material."
                ),
            )

        cleaned_data["justificativa_parcial"] = justificativa
        cleaned_data["recebedor_relacao_outro"] = relacao_outro
        cleaned_data["observacao"] = observacao

        self._dados_calculados = {
            "previsto_mesas": self.previsto_mesas,
            "previsto_cadeiras": self.previsto_cadeiras,
            "acumulado_anterior_mesas": self.acumulado_mesas,
            "acumulado_anterior_cadeiras": (
                self.acumulado_cadeiras
            ),
            "acumulado_mesas": novo_acumulado_mesas,
            "acumulado_cadeiras": novo_acumulado_cadeiras,
            "pendente_mesas": novo_pendente_mesas,
            "pendente_cadeiras": novo_pendente_cadeiras,
            "situacao": (
                ConferenciaEntregaLocacao.SITUACAO_PARCIAL
                if entrega_parcial
                else ConferenciaEntregaLocacao.SITUACAO_COMPLETA
            ),
        }

        return cleaned_data

    def dados_conferencia(self):
        if not self.is_valid():
            raise ValueError(
                "O formulario precisa ser valido antes do calculo."
            )
        return dict(self._dados_calculados)


class NaoPossivelOperacionalLocacaoForm(forms.Form):
    responsavel = forms.CharField(
        required=False,
        max_length=120,
        widget=forms.TextInput(attrs={"class": "form-control", "autocomplete": "off"}),
    )
    observacao = forms.CharField(
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 2}),
    )

    def clean_observacao(self):
        observacao = (self.cleaned_data.get("observacao") or "").strip()
        if not observacao:
            raise forms.ValidationError("Informe o motivo/observacao.")
        return observacao


class ConferenciaRecolhimentoLocacaoForm(forms.Form):
    boa_mesas = forms.IntegerField(
        min_value=0,
        initial=0,
        label="Mesas recolhidas em bom estado",
        widget=forms.NumberInput(
            attrs={
                "class": "form-control",
                "min": "0",
                "step": "1",
            }
        ),
    )
    boa_cadeiras = forms.IntegerField(
        min_value=0,
        initial=0,
        label="Cadeiras recolhidas em bom estado",
        widget=forms.NumberInput(
            attrs={
                "class": "form-control",
                "min": "0",
                "step": "1",
            }
        ),
    )
    quebrada_mesas = forms.IntegerField(
        min_value=0,
        initial=0,
        label="Mesas quebradas",
        widget=forms.NumberInput(
            attrs={
                "class": "form-control",
                "min": "0",
                "step": "1",
            }
        ),
    )
    quebrada_cadeiras = forms.IntegerField(
        min_value=0,
        initial=0,
        label="Cadeiras quebradas",
        widget=forms.NumberInput(
            attrs={
                "class": "form-control",
                "min": "0",
                "step": "1",
            }
        ),
    )
    perdida_mesas = forms.IntegerField(
        min_value=0,
        initial=0,
        label="Mesas perdidas",
        widget=forms.NumberInput(
            attrs={
                "class": "form-control",
                "min": "0",
                "step": "1",
            }
        ),
    )
    perdida_cadeiras = forms.IntegerField(
        min_value=0,
        initial=0,
        label="Cadeiras perdidas",
        widget=forms.NumberInput(
            attrs={
                "class": "form-control",
                "min": "0",
                "step": "1",
            }
        ),
    )
    descartada_mesas = forms.IntegerField(
        min_value=0,
        initial=0,
        label="Mesas descartadas",
        widget=forms.NumberInput(
            attrs={
                "class": "form-control",
                "min": "0",
                "step": "1",
            }
        ),
    )
    descartada_cadeiras = forms.IntegerField(
        min_value=0,
        initial=0,
        label="Cadeiras descartadas",
        widget=forms.NumberInput(
            attrs={
                "class": "form-control",
                "min": "0",
                "step": "1",
            }
        ),
    )

    pessoa_local_nome = forms.CharField(
        max_length=160,
        label="Com quem foi conferido",
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "autocomplete": "off",
            }
        ),
    )
    pessoa_local_relacao = forms.ChoiceField(
        choices=ConferenciaEntregaLocacao.RELACAO_CHOICES,
        label="Relacao com o cliente",
        widget=forms.Select(
            attrs={"class": "form-select"}
        ),
    )
    pessoa_local_relacao_outro = forms.CharField(
        required=False,
        max_length=120,
        label="Qual e a relacao",
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "autocomplete": "off",
            }
        ),
    )

    justificativa_parcial = forms.CharField(
        required=False,
        label="Justificativa do recolhimento parcial",
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 3,
            }
        ),
    )
    previsao_conclusao = forms.DateTimeField(
        required=False,
        label="Previsao para concluir",
        input_formats=["%Y-%m-%dT%H:%M"],
        widget=forms.DateTimeInput(
            format="%Y-%m-%dT%H:%M",
            attrs={
                "class": "form-control",
                "type": "datetime-local",
            },
        ),
    )
    observacao = forms.CharField(
        required=False,
        label="Observacao",
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 3,
            }
        ),
    )
    responsavel = forms.CharField(
        max_length=120,
        label="Funcionario responsavel",
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "autocomplete": "off",
            }
        ),
    )

    def __init__(self, *args, locacao, **kwargs):
        super().__init__(*args, **kwargs)
        self.locacao = locacao

        previsto = (
            ConferenciaRecolhimentoLocacao
            .totais_entregues(locacao)
        )
        recolhido = (
            ConferenciaRecolhimentoLocacao
            .totais_recolhidos(locacao)
        )

        encerrado_mesas = sum(
            valor
            for chave, valor in recolhido.items()
            if chave.endswith("_mesas")
        )
        encerrado_cadeiras = sum(
            valor
            for chave, valor in recolhido.items()
            if chave.endswith("_cadeiras")
        )

        self.previsto_mesas = previsto["mesas"]
        self.previsto_cadeiras = previsto["cadeiras"]
        self.recolhido_mesas = encerrado_mesas
        self.recolhido_cadeiras = encerrado_cadeiras
        self.pendente_mesas = max(
            self.previsto_mesas - encerrado_mesas,
            0,
        )
        self.pendente_cadeiras = max(
            self.previsto_cadeiras - encerrado_cadeiras,
            0,
        )

        if not self.is_bound:
            self.fields["boa_mesas"].initial = (
                self.pendente_mesas
            )
            self.fields["boa_cadeiras"].initial = (
                self.pendente_cadeiras
            )

    def clean(self):
        cleaned_data = super().clean()

        campos_quantidade = [
            "boa_mesas",
            "boa_cadeiras",
            "quebrada_mesas",
            "quebrada_cadeiras",
            "perdida_mesas",
            "perdida_cadeiras",
            "descartada_mesas",
            "descartada_cadeiras",
        ]

        quantidades = {
            campo: int(cleaned_data.get(campo) or 0)
            for campo in campos_quantidade
        }

        atual_mesas = sum(
            valor
            for campo, valor in quantidades.items()
            if campo.endswith("_mesas")
        )
        atual_cadeiras = sum(
            valor
            for campo, valor in quantidades.items()
            if campo.endswith("_cadeiras")
        )

        if atual_mesas == 0 and atual_cadeiras == 0:
            self.add_error(
                "boa_mesas",
                "Informe pelo menos uma mesa ou cadeira.",
            )

        if atual_mesas > self.pendente_mesas:
            self.add_error(
                "boa_mesas",
                (
                    "A quantidade de mesas informada supera "
                    "o total que ainda esta pendente."
                ),
            )

        if atual_cadeiras > self.pendente_cadeiras:
            self.add_error(
                "boa_cadeiras",
                (
                    "A quantidade de cadeiras informada supera "
                    "o total que ainda esta pendente."
                ),
            )

        novo_pendente_mesas = max(
            self.pendente_mesas - atual_mesas,
            0,
        )
        novo_pendente_cadeiras = max(
            self.pendente_cadeiras - atual_cadeiras,
            0,
        )
        parcial = bool(
            novo_pendente_mesas or novo_pendente_cadeiras
        )

        justificativa = str(
            cleaned_data.get("justificativa_parcial") or ""
        ).strip()
        previsao = cleaned_data.get("previsao_conclusao")
        observacao = str(
            cleaned_data.get("observacao") or ""
        ).strip()
        relacao = cleaned_data.get("pessoa_local_relacao")
        relacao_outro = str(
            cleaned_data.get("pessoa_local_relacao_outro") or ""
        ).strip()

        if parcial:
            if not justificativa:
                self.add_error(
                    "justificativa_parcial",
                    (
                        "Informe a justificativa do "
                        "recolhimento parcial."
                    ),
                )
            if not previsao:
                self.add_error(
                    "previsao_conclusao",
                    (
                        "Informe quando o recolhimento "
                        "sera concluido."
                    ),
                )

        if (
            relacao
            == ConferenciaEntregaLocacao.RELACAO_OUTRO
            and not relacao_outro
        ):
            self.add_error(
                "pessoa_local_relacao_outro",
                "Informe a relacao da pessoa com o cliente.",
            )

        houve_ocorrencia = any(
            quantidades[campo] > 0
            for campo in [
                "quebrada_mesas",
                "quebrada_cadeiras",
                "perdida_mesas",
                "perdida_cadeiras",
                "descartada_mesas",
                "descartada_cadeiras",
            ]
        )

        if houve_ocorrencia and not observacao:
            self.add_error(
                "observacao",
                (
                    "Explique a quebra, perda ou descarte "
                    "registrado."
                ),
            )

        cleaned_data["justificativa_parcial"] = (
            justificativa
        )
        cleaned_data["pessoa_local_relacao_outro"] = (
            relacao_outro
        )
        cleaned_data["observacao"] = observacao

        return cleaned_data


class DevolucaoLocacaoForm(forms.Form):
    responsavel = forms.CharField(
        required=False,
        max_length=120,
        widget=forms.TextInput(attrs={"class": "form-control", "autocomplete": "off"}),
    )
    observacao = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 3}),
    )

    def __init__(self, *args, locacao, **kwargs):
        super().__init__(*args, **kwargs)
        self.locacao = locacao
        for item in locacao.itens.all():
            prefixo = f"item_{item.id}"
            attrs = {"class": "form-control", "min": "0", "step": "1"}
            self.fields[f"{prefixo}_boa"] = forms.IntegerField(
                required=False,
                min_value=0,
                initial=0,
                widget=forms.NumberInput(attrs=attrs),
            )
            self.fields[f"{prefixo}_quebrada"] = forms.IntegerField(
                required=False,
                min_value=0,
                initial=0,
                widget=forms.NumberInput(attrs=attrs),
            )
            self.fields[f"{prefixo}_perdida"] = forms.IntegerField(
                required=False,
                min_value=0,
                initial=0,
                widget=forms.NumberInput(attrs=attrs),
            )
            self.fields[f"{prefixo}_descartada"] = forms.IntegerField(
                required=False,
                min_value=0,
                initial=0,
                widget=forms.NumberInput(attrs=attrs),
            )

    def clean(self):
        cleaned_data = super().clean()
        for item in self.locacao.itens.all():
            prefixo = f"item_{item.id}"
            total = sum(
                cleaned_data.get(f"{prefixo}_{campo}") or 0
                for campo in ["boa", "quebrada", "perdida", "descartada"]
            )
            if total > item.quantidade_pendente():
                self.add_error(
                    f"{prefixo}_boa",
                    f"A soma informada para {item.get_tipo_display()} supera o pendente.",
                )
        return cleaned_data

    def retornos_por_item(self):
        retornos = {}
        for item in self.locacao.itens.all():
            prefixo = f"item_{item.id}"
            retornos[item.id] = {
                "devolvida_boa": self.cleaned_data.get(f"{prefixo}_boa") or 0,
                "quebrada": self.cleaned_data.get(f"{prefixo}_quebrada") or 0,
                "perdida": self.cleaned_data.get(f"{prefixo}_perdida") or 0,
                "descartada": self.cleaned_data.get(f"{prefixo}_descartada") or 0,
            }
        return retornos
