from django import forms

from estoque.models import Cliente

from .models import (
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
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    cliente = forms.ModelChoiceField(
        queryset=Cliente.objects.filter(ativo=True).order_by("nome"),
        required=False,
        widget=forms.Select(attrs={"class": "form-select"}),
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
