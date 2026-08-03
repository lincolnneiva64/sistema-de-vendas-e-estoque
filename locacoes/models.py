import uuid

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone

from estoque.models import Cliente, ContaFinanceira, MovimentoFinanceiro


class ConfiguracaoLocacao(models.Model):
    JOGO_MESAS = 1
    JOGO_CADEIRAS = 4

    total_mesas = models.PositiveIntegerField(blank=True, null=True)
    total_cadeiras = models.PositiveIntegerField(blank=True, null=True)
    preco_mesa_avulsa_diaria = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
    )
    preco_cadeira_avulsa_diaria = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
    )
    valor_reposicao_mesa = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("80.00"),
    )
    valor_reposicao_cadeira = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("40.00"),
    )
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Configuracao de locacao"
        verbose_name_plural = "Configuracoes de locacao"

    def __str__(self):
        return "Configuracao de locacoes"

    def clean(self):
        campos_monetarios = [
            "preco_mesa_avulsa_diaria",
            "preco_cadeira_avulsa_diaria",
            "valor_reposicao_mesa",
            "valor_reposicao_cadeira",
        ]
        for campo in campos_monetarios:
            valor = getattr(self, campo)
            if valor is not None and valor < Decimal("0.00"):
                raise ValidationError({campo: "Informe um valor maior ou igual a zero."})
        if self.pk:
            anterior = ConfiguracaoLocacao.objects.filter(pk=self.pk).first()
            if anterior:
                erros = {}
                for campo in ["total_mesas", "total_cadeiras"]:
                    valor_anterior = getattr(anterior, campo)
                    valor_novo = getattr(self, campo)
                    if valor_anterior is not None and valor_novo != valor_anterior:
                        erros[campo] = (
                            "Saldo ja configurado. Use Registrar movimentacao "
                            "para alterar a disponibilidade de locacao."
                        )
                if erros:
                    raise ValidationError(erros)

    def save(self, *args, **kwargs):
        if not self.pk and ConfiguracaoLocacao.objects.exists():
            raise ValidationError("Ja existe uma configuracao de locacao.")
        self.full_clean()
        super().save(*args, **kwargs)

    @classmethod
    def obter(cls):
        configuracao = cls.objects.order_by("id").first()
        if configuracao:
            return configuracao
        return cls.objects.create()

    @classmethod
    def composicao_jogo(cls):
        return {
            "mesas": cls.JOGO_MESAS,
            "cadeiras": cls.JOGO_CADEIRAS,
        }


class MovimentoEstoqueLocacao(models.Model):
    ITEM_MESA = "mesa"
    ITEM_CADEIRA = "cadeira"
    ITEM_CHOICES = [
        (ITEM_MESA, "Mesa"),
        (ITEM_CADEIRA, "Cadeira"),
    ]

    TIPO_ENTRADA = "entrada"
    TIPO_BAIXA_QUEBRA = "baixa_quebra"
    TIPO_BAIXA_PERDA = "baixa_perda"
    TIPO_BAIXA_DESCARTE = "baixa_descarte"
    TIPO_AJUSTE_INVENTARIO = "ajuste_inventario"
    TIPO_CHOICES = [
        (TIPO_ENTRADA, "Entrada/compra ou aquisicao"),
        (TIPO_BAIXA_QUEBRA, "Baixa definitiva por quebra"),
        (TIPO_BAIXA_PERDA, "Baixa definitiva por perda"),
        (TIPO_BAIXA_DESCARTE, "Baixa definitiva por descarte"),
        (TIPO_AJUSTE_INVENTARIO, "Ajuste de inventario"),
    ]

    item = models.CharField(max_length=20, choices=ITEM_CHOICES)
    tipo = models.CharField(max_length=30, choices=TIPO_CHOICES)
    quantidade = models.PositiveIntegerField()
    saldo_anterior = models.PositiveIntegerField()
    saldo_posterior = models.PositiveIntegerField()
    responsavel = models.CharField(max_length=120)
    observacao = models.TextField(blank=True)
    locacao = models.ForeignKey(
        "Locacao",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="movimentos_estoque",
    )
    item_locacao = models.ForeignKey(
        "ItemLocacao",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="movimentos_estoque",
    )
    data_hora = models.DateTimeField(default=timezone.now)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-data_hora", "-id"]
        verbose_name = "Movimento de estoque de locacao"
        verbose_name_plural = "Movimentos de estoque de locacao"

    def __str__(self):
        return f"{self.get_tipo_display()} - {self.get_item_display()} #{self.id}"

    @classmethod
    def campo_saldo_configuracao(cls, item):
        if item == cls.ITEM_MESA:
            return "total_mesas"
        if item == cls.ITEM_CADEIRA:
            return "total_cadeiras"
        raise ValidationError({"item": "Item de locacao invalido."})

    @classmethod
    def calcular_saldo_posterior(cls, saldo_anterior, tipo, quantidade=None, saldo_contado=None):
        saldo_anterior = int(saldo_anterior or 0)
        if tipo == cls.TIPO_ENTRADA:
            return saldo_anterior + int(quantidade or 0)
        if tipo in {
            cls.TIPO_BAIXA_QUEBRA,
            cls.TIPO_BAIXA_PERDA,
            cls.TIPO_BAIXA_DESCARTE,
        }:
            saldo_posterior = saldo_anterior - int(quantidade or 0)
            if saldo_posterior < 0:
                raise ValidationError({"quantidade": "A baixa nao pode deixar saldo negativo."})
            return saldo_posterior
        if tipo == cls.TIPO_AJUSTE_INVENTARIO:
            if saldo_contado is None:
                raise ValidationError({"saldo_contado": "Informe o saldo contado no inventario."})
            return int(saldo_contado)
        raise ValidationError({"tipo": "Tipo de movimentacao invalido."})

    @classmethod
    def registrar(
        cls,
        item,
        tipo,
        quantidade=None,
        responsavel="",
        observacao="",
        saldo_contado=None,
        locacao=None,
        item_locacao=None,
    ):
        if tipo == cls.TIPO_AJUSTE_INVENTARIO and not str(observacao or "").strip():
            raise ValidationError({"observacao": "Informe o motivo do ajuste de inventario."})
        if not str(responsavel or "").strip():
            raise ValidationError({"responsavel": "Informe o responsavel pela movimentacao."})

        quantidade_normalizada = int(quantidade or 0)
        if tipo != cls.TIPO_AJUSTE_INVENTARIO and quantidade_normalizada <= 0:
            raise ValidationError({"quantidade": "Informe uma quantidade maior que zero."})

        with transaction.atomic():
            configuracao = ConfiguracaoLocacao.objects.select_for_update().order_by("id").first()
            if not configuracao:
                configuracao = ConfiguracaoLocacao.objects.create()
                configuracao = ConfiguracaoLocacao.objects.select_for_update().get(pk=configuracao.pk)

            campo_saldo = cls.campo_saldo_configuracao(item)
            saldo_anterior = int(getattr(configuracao, campo_saldo) or 0)
            saldo_posterior = cls.calcular_saldo_posterior(
                saldo_anterior,
                tipo,
                quantidade=quantidade_normalizada,
                saldo_contado=saldo_contado,
            )
            if saldo_posterior < 0:
                raise ValidationError({"saldo_contado": "O saldo posterior nao pode ser negativo."})
            if tipo == cls.TIPO_AJUSTE_INVENTARIO:
                quantidade_normalizada = abs(saldo_posterior - saldo_anterior)

            movimento = cls.objects.create(
                item=item,
                tipo=tipo,
                quantidade=quantidade_normalizada,
                saldo_anterior=saldo_anterior,
                saldo_posterior=saldo_posterior,
                responsavel=" ".join(str(responsavel).strip().split()),
                observacao=str(observacao or "").strip(),
                locacao=locacao,
                item_locacao=item_locacao,
            )
            ConfiguracaoLocacao.objects.filter(pk=configuracao.pk).update(
                **{campo_saldo: saldo_posterior},
                atualizado_em=timezone.now(),
            )
            return movimento


class FaixaPrecoLocacao(models.Model):
    CENTRO_PERTO = "centro_perto"
    MAIS_DISTANTE = "mais_distante"
    MUITO_DISTANTE = "muito_distante"
    CODIGO_CHOICES = [
        (CENTRO_PERTO, "Centro/perto"),
        (MAIS_DISTANTE, "Mais distante"),
        (MUITO_DISTANTE, "Muito distante"),
    ]

    codigo = models.CharField(max_length=30, choices=CODIGO_CHOICES, unique=True)
    nome = models.CharField(max_length=80)
    preco_jogo_diaria = models.DecimalField(max_digits=10, decimal_places=2)
    ordem = models.PositiveSmallIntegerField(default=0)
    ativa = models.BooleanField(default=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["ordem", "id"]
        verbose_name = "Faixa de preco de locacao"
        verbose_name_plural = "Faixas de preco de locacao"

    def __str__(self):
        return self.nome

    def clean(self):
        if self.preco_jogo_diaria is not None and self.preco_jogo_diaria < Decimal("0.00"):
            raise ValidationError({
                "preco_jogo_diaria": "Informe um valor maior ou igual a zero."
            })

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)


class Locacao(models.Model):
    STATUS_RESERVADA = "reservada"
    STATUS_SAIU_PARA_ENTREGA = "saiu_para_entrega"
    STATUS_ENTREGUE = "entregue"
    STATUS_DEVOLVIDA = "devolvida"
    STATUS_DEVOLVIDA_COM_AVARIA = "devolvida_com_avaria"
    STATUS_PENDENTE_DEVOLUCAO = "pendente_devolucao"
    STATUS_CANCELADA = "cancelada"
    STATUS_CHOICES = [
        (STATUS_RESERVADA, "Reservada"),
        (STATUS_SAIU_PARA_ENTREGA, "Saiu para entrega"),
        (STATUS_ENTREGUE, "Entregue"),
        (STATUS_DEVOLVIDA, "Devolvida"),
        (STATUS_DEVOLVIDA_COM_AVARIA, "Devolvida com avaria"),
        (STATUS_PENDENTE_DEVOLUCAO, "Pendente de devolucao"),
        (STATUS_CANCELADA, "Cancelada"),
    ]

    TIPO_PESSOA_CLIENTE = "cliente"
    TIPO_PESSOA_AVULSA = "avulsa"
    TIPO_PESSOA_CHOICES = [
        (TIPO_PESSOA_CLIENTE, "Cliente cadastrado"),
        (TIPO_PESSOA_AVULSA, "Pessoa avulsa"),
    ]

    FINANCEIRO_PENDENTE = "pendente"
    FINANCEIRO_PARCIAL = "parcial"
    FINANCEIRO_QUITADA = "quitada"
    FINANCEIRO_CHOICES = [
        (FINANCEIRO_PENDENTE, "Pendente"),
        (FINANCEIRO_PARCIAL, "Parcialmente pago"),
        (FINANCEIRO_QUITADA, "Quitada"),
    ]

    cliente = models.ForeignKey(
        Cliente,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="locacoes",
    )
    tipo_pessoa = models.CharField(max_length=20, choices=TIPO_PESSOA_CHOICES)
    pessoa_avulsa_nome = models.CharField(max_length=160, blank=True)
    pessoa_avulsa_telefone = models.CharField(max_length=40, blank=True)
    pessoa_avulsa_endereco = models.TextField(blank=True)
    endereco_entrega = models.TextField()
    data_entrega = models.DateField()
    horario_entrega = models.TimeField()
    data_evento = models.DateField()
    horario_evento = models.TimeField()
    data_prevista_devolucao = models.DateField()
    data_vencimento_saldo = models.DateField(blank=True, null=True)
    faixa_preco = models.ForeignKey(
        FaixaPrecoLocacao,
        on_delete=models.PROTECT,
        related_name="locacoes",
    )
    faixa_preco_nome_snapshot = models.CharField(max_length=80)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_RESERVADA)
    observacao = models.TextField(blank=True)
    motivo_cancelamento = models.TextField(blank=True)
    total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    total_pago = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    saldo_devedor = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    status_financeiro = models.CharField(
        max_length=20,
        choices=FINANCEIRO_CHOICES,
        default=FINANCEIRO_PENDENTE,
    )
    valor_reposicao_mesa_snapshot = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
    )
    valor_reposicao_cadeira_snapshot = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
    )
    termo_gerado_em = models.DateTimeField(blank=True, null=True)
    termo_gerado_por = models.CharField(max_length=120, blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)
    cancelada_em = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ["-data_entrega", "-id"]
        verbose_name = "Locacao"
        verbose_name_plural = "Locacoes"

    def __str__(self):
        return f"Locacao #{self.id}"

    def save(self, *args, **kwargs):
        if self.pk:
            anterior = Locacao.objects.filter(pk=self.pk).only("status").first()
            if (
                anterior
                and anterior.status != self.status
                and not getattr(self, "_permitir_alterar_status", False)
            ):
                raise ValidationError("Status de locacao deve ser alterado por uma acao propria.")
        super().save(*args, **kwargs)

    @property
    def nome_contratante(self):
        if self.cliente_id and self.cliente:
            return self.cliente.nome
        return self.pessoa_avulsa_nome or "Pessoa avulsa"

    @property
    def telefone_contratante(self):
        if self.cliente_id and self.cliente:
            return self.cliente.whatsapp or self.cliente.telefone_alternativo or ""
        return self.pessoa_avulsa_telefone

    @property
    def endereco_contratante(self):
        if self.cliente_id and self.cliente:
            partes = [
                self.cliente.logradouro,
                self.cliente.numero,
                self.cliente.complemento,
                self.cliente.bairro,
                self.cliente.cidade,
                self.cliente.uf,
            ]
            return ", ".join(str(parte).strip() for parte in partes if parte)
        return self.pessoa_avulsa_endereco

    def atualizar_financeiro(self):
        total_pago = (
            self.pagamentos.aggregate(total=models.Sum("valor")).get("total")
            or Decimal("0.00")
        ).quantize(Decimal("0.01"))
        total = (self.total or Decimal("0.00")).quantize(Decimal("0.01"))
        saldo = max(total - total_pago, Decimal("0.00")).quantize(Decimal("0.01"))
        if total_pago <= Decimal("0.00"):
            status = self.FINANCEIRO_PENDENTE
        elif saldo > Decimal("0.00"):
            status = self.FINANCEIRO_PARCIAL
        else:
            status = self.FINANCEIRO_QUITADA
        self.total_pago = total_pago
        self.saldo_devedor = saldo
        self.status_financeiro = status
        self.save(update_fields=["total_pago", "saldo_devedor", "status_financeiro", "atualizado_em"])
        return {
            "total_pago": total_pago,
            "saldo_devedor": saldo,
            "status_financeiro": status,
        }

    @classmethod
    def calcular_diarias(cls, data_entrega, data_prevista_devolucao):
        if not data_entrega or not data_prevista_devolucao:
            raise ValidationError("Informe as datas da locacao.")
        if data_prevista_devolucao < data_entrega:
            raise ValidationError("A devolucao prevista nao pode ser anterior a entrega.")
        return max((data_prevista_devolucao - data_entrega).days, 1)

    @classmethod
    def periodo_conflita_q(cls, data_entrega, data_prevista_devolucao):
        return models.Q(data_entrega__lte=data_prevista_devolucao) & models.Q(
            data_prevista_devolucao__gte=data_entrega
        )

    @classmethod
    def reservados_no_periodo(cls, data_entrega, data_prevista_devolucao, excluir_id=None):
        locacoes_reservadas = cls.objects.filter(status=cls.STATUS_RESERVADA).filter(
            cls.periodo_conflita_q(data_entrega, data_prevista_devolucao)
        )
        locacoes_rua = (
            cls.objects
            .filter(
                status__in=[
                    cls.STATUS_SAIU_PARA_ENTREGA,
                    cls.STATUS_ENTREGUE,
                    cls.STATUS_PENDENTE_DEVOLUCAO,
                ],
                data_entrega__lte=data_prevista_devolucao,
            )
            .prefetch_related(
                "itens",
                "conferencias_entrega",
                "conferencias_recolhimento",
            )
        )
        if excluir_id:
            locacoes_reservadas = locacoes_reservadas.exclude(pk=excluir_id)
            locacoes_rua = locacoes_rua.exclude(pk=excluir_id)

        mesas = 0
        cadeiras = 0
        for item in ItemLocacao.objects.filter(locacao__in=locacoes_reservadas):
            necessidade = item.necessidade_estoque()
            mesas += necessidade["mesas"]
            cadeiras += necessidade["cadeiras"]
        for locacao in locacoes_rua:
            conferencias_entrega = list(
                locacao.conferencias_entrega.all()
            )

            # Enquanto a entrega ainda estiver incompleta,
            # toda a quantidade contratada continua reservada.
            if (
                locacao.status
                == cls.STATUS_SAIU_PARA_ENTREGA
                or not conferencias_entrega
            ):
                for item in locacao.itens.all():
                    pendente = item.necessidade_pendente()
                    mesas += pendente["mesas"]
                    cadeiras += pendente["cadeiras"]
                continue

            entregues_mesas = sum(
                conferencia.entregue_mesas
                for conferencia in conferencias_entrega
            )
            entregues_cadeiras = sum(
                conferencia.entregue_cadeiras
                for conferencia in conferencias_entrega
            )

            recolhidos_mesas = 0
            recolhidos_cadeiras = 0

            for conferencia in (
                locacao.conferencias_recolhimento.all()
            ):
                recolhidos_mesas += (
                    conferencia.boa_mesas
                    + conferencia.quebrada_mesas
                    + conferencia.perdida_mesas
                    + conferencia.descartada_mesas
                )
                recolhidos_cadeiras += (
                    conferencia.boa_cadeiras
                    + conferencia.quebrada_cadeiras
                    + conferencia.perdida_cadeiras
                    + conferencia.descartada_cadeiras
                )

            mesas += max(
                entregues_mesas - recolhidos_mesas,
                0,
            )
            cadeiras += max(
                entregues_cadeiras - recolhidos_cadeiras,
                0,
            )
        return {"mesas": mesas, "cadeiras": cadeiras}

    @classmethod
    def disponibilidade_periodo(cls, data_entrega, data_prevista_devolucao, excluir_id=None):
        configuracao = ConfiguracaoLocacao.obter()
        reservado = cls.reservados_no_periodo(data_entrega, data_prevista_devolucao, excluir_id=excluir_id)
        saldo_mesas = int(configuracao.total_mesas or 0)
        saldo_cadeiras = int(configuracao.total_cadeiras or 0)
        return {
            "saldo_mesas": saldo_mesas,
            "saldo_cadeiras": saldo_cadeiras,
            "reservado_mesas": reservado["mesas"],
            "reservado_cadeiras": reservado["cadeiras"],
            "disponivel_mesas": saldo_mesas - reservado["mesas"],
            "disponivel_cadeiras": saldo_cadeiras - reservado["cadeiras"],
        }

    @staticmethod
    def necessidades_itens(itens):
        mesas = 0
        cadeiras = 0
        for item in itens:
            tipo = item["tipo"]
            quantidade = int(item.get("quantidade") or 0)
            if tipo == ItemLocacao.TIPO_JOGO:
                mesas += quantidade * ConfiguracaoLocacao.JOGO_MESAS
                cadeiras += quantidade * ConfiguracaoLocacao.JOGO_CADEIRAS
            elif tipo == ItemLocacao.TIPO_MESA_AVULSA:
                mesas += quantidade
            elif tipo == ItemLocacao.TIPO_CADEIRA_AVULSA:
                cadeiras += quantidade
        return {"mesas": mesas, "cadeiras": cadeiras}

    @classmethod
    def validar_disponibilidade(cls, data_entrega, data_prevista_devolucao, itens, excluir_id=None):
        necessidade = cls.necessidades_itens(itens)
        disponibilidade = cls.disponibilidade_periodo(data_entrega, data_prevista_devolucao, excluir_id=excluir_id)
        erros = []
        if necessidade["mesas"] > disponibilidade["disponivel_mesas"]:
            erros.append("Mesas indisponiveis no periodo informado.")
        if necessidade["cadeiras"] > disponibilidade["disponivel_cadeiras"]:
            erros.append("Cadeiras indisponiveis no periodo informado.")
        if erros:
            raise ValidationError(erros)
        return {**disponibilidade, "solicitado_mesas": necessidade["mesas"], "solicitado_cadeiras": necessidade["cadeiras"]}

    @classmethod
    def criar_reserva(cls, dados, itens, responsavel=""):
        itens_validos = [item for item in itens if int(item.get("quantidade") or 0) > 0]
        if not itens_validos:
            raise ValidationError("Inclua pelo menos um item na locacao.")

        diarias = cls.calcular_diarias(dados["data_entrega"], dados["data_prevista_devolucao"])
        cls.validar_disponibilidade(dados["data_entrega"], dados["data_prevista_devolucao"], itens_validos)

        with transaction.atomic():
            configuracao = ConfiguracaoLocacao.obter()
            locacao = cls.objects.create(
                cliente=dados.get("cliente"),
                tipo_pessoa=dados["tipo_pessoa"],
                pessoa_avulsa_nome=dados.get("pessoa_avulsa_nome", ""),
                pessoa_avulsa_telefone=dados.get("pessoa_avulsa_telefone", ""),
                pessoa_avulsa_endereco=dados.get("pessoa_avulsa_endereco", ""),
                endereco_entrega=dados["endereco_entrega"],
                data_entrega=dados["data_entrega"],
                horario_entrega=dados["horario_entrega"],
                data_evento=dados["data_evento"],
                horario_evento=dados["horario_evento"],
                data_prevista_devolucao=dados["data_prevista_devolucao"],
                data_vencimento_saldo=(
                    None
                    if dados.get("sem_vencimento_saldo")
                    else (
                        dados.get("data_vencimento_saldo")
                        or dados["data_entrega"]
                    )
                ),
                faixa_preco=dados["faixa_preco"],
                faixa_preco_nome_snapshot=dados["faixa_preco"].nome,
                observacao=dados.get("observacao", ""),
                valor_reposicao_mesa_snapshot=configuracao.valor_reposicao_mesa,
                valor_reposicao_cadeira_snapshot=configuracao.valor_reposicao_cadeira,
            )
            total = Decimal("0.00")
            for item in itens_validos:
                quantidade = int(item["quantidade"])
                preco_diaria = Decimal(item["preco_diaria"]).quantize(Decimal("0.01"))
                valor_total = (Decimal(quantidade) * preco_diaria * Decimal(diarias)).quantize(Decimal("0.01"))
                ItemLocacao.objects.create(
                    locacao=locacao,
                    tipo=item["tipo"],
                    quantidade=quantidade,
                    preco_diaria_snapshot=preco_diaria,
                    diarias=diarias,
                    valor_total=valor_total,
                    ajuste_manual=bool(item.get("ajuste_manual")),
                )
                total += valor_total
            locacao.total = total.quantize(Decimal("0.01"))
            locacao.saldo_devedor = locacao.total
            locacao.status_financeiro = cls.FINANCEIRO_PENDENTE
            locacao.save(update_fields=["total", "saldo_devedor", "status_financeiro", "atualizado_em"])
            EventoLocacao.objects.create(
                locacao=locacao,
                tipo="criada",
                descricao="Reserva de locacao criada. Material ainda nao saiu para entrega.",
                responsavel=responsavel,
            )
            return locacao

    def registrar_pagamento(self, valor, forma_pagamento, data_hora=None, observacao="", responsavel=""):
        valor = Decimal(valor or "0").quantize(Decimal("0.01"))
        if valor <= Decimal("0.00"):
            raise ValidationError("Informe um valor de pagamento maior que zero.")
        saldo_atual = (self.saldo_devedor or self.total or Decimal("0.00")).quantize(Decimal("0.01"))
        if valor > saldo_atual:
            raise ValidationError("Pagamento nao pode superar o saldo devedor da locacao.")
        if forma_pagamento not in dict(PagamentoLocacao.FORMA_CHOICES):
            raise ValidationError("Forma de pagamento invalida.")

        with transaction.atomic():
            locacao = Locacao.objects.select_for_update().get(pk=self.pk)
            locacao.atualizar_financeiro()
            if valor > locacao.saldo_devedor:
                raise ValidationError("Pagamento nao pode superar o saldo devedor da locacao.")
            pagamento = PagamentoLocacao.objects.create(
                locacao=locacao,
                valor=valor,
                data_hora=data_hora or timezone.now(),
                forma_pagamento=forma_pagamento,
                observacao=str(observacao or "").strip(),
                responsavel=str(responsavel or "").strip(),
            )
            pagamento.criar_movimento_financeiro()
            locacao.atualizar_financeiro()
            EventoLocacao.objects.create(
                locacao=locacao,
                tipo="pagamento",
                descricao=f"Pagamento de locacao registrado: R$ {valor:.2f}.",
                responsavel=responsavel,
            )
            self.refresh_from_db()
            return pagamento

    def alterar_vencimento_saldo(self, nova_data, responsavel="", observacao=""):
        data_anterior = self.data_vencimento_saldo
        self.data_vencimento_saldo = nova_data
        self.save(update_fields=["data_vencimento_saldo", "atualizado_em"])
        anterior_texto = data_anterior.strftime("%d/%m/%Y") if data_anterior else "sem data"
        nova_texto = nova_data.strftime("%d/%m/%Y") if nova_data else "sem data"
        descricao = str(observacao or "").strip()
        if descricao:
            descricao = f"{descricao}\n"
        descricao = f"{descricao}Vencimento do saldo alterado de {anterior_texto} para {nova_texto}."
        EventoLocacao.objects.create(
            locacao=self,
            tipo="vencimento_saldo",
            descricao=descricao,
            responsavel=responsavel,
        )

    def saldo_vencido_em(self, data_referencia=None):
        data_referencia = data_referencia or timezone.localdate()
        return (
            self.saldo_devedor > Decimal("0.00")
            and self.data_vencimento_saldo
            and self.data_vencimento_saldo < data_referencia
        )

    def registrar_termo_gerado(self, responsavel=""):
        self.termo_gerado_em = timezone.now()
        self.termo_gerado_por = str(responsavel or "").strip()
        self.save(update_fields=["termo_gerado_em", "termo_gerado_por", "atualizado_em"])
        EventoLocacao.objects.create(
            locacao=self,
            tipo="termo_gerado",
            descricao="Termo de compromisso gerado para impressao.",
            responsavel=self.termo_gerado_por,
        )

    def cancelar(self, motivo="", responsavel=""):
        if self.status == self.STATUS_CANCELADA:
            return
        if self.status != self.STATUS_RESERVADA:
            raise ValidationError("Somente reservas ainda nao enviadas podem ser canceladas.")
        self.status = self.STATUS_CANCELADA
        self.motivo_cancelamento = str(motivo or "").strip()
        self.cancelada_em = timezone.now()
        self._permitir_alterar_status = True
        self.save(update_fields=["status", "motivo_cancelamento", "cancelada_em", "atualizado_em"])
        self._permitir_alterar_status = False
        EventoLocacao.objects.create(
            locacao=self,
            tipo="cancelada",
            descricao=self.motivo_cancelamento or "Reserva cancelada.",
            responsavel=responsavel,
        )

    def marcar_saiu_para_entrega(self, responsavel="", observacao=""):
        if self.status != self.STATUS_RESERVADA:
            raise ValidationError("Somente reserva pode ser marcada como saiu para entrega.")
        self.status = self.STATUS_SAIU_PARA_ENTREGA
        self._permitir_alterar_status = True
        self.save(update_fields=["status", "atualizado_em"])
        self._permitir_alterar_status = False
        EventoLocacao.objects.create(
            locacao=self,
            tipo="saiu_para_entrega",
            descricao=str(observacao or "").strip() or "Material saiu para entrega.",
            responsavel=responsavel,
        )

    def confirmar_entrega(self, responsavel="", observacao=""):
        if self.status != self.STATUS_SAIU_PARA_ENTREGA:
            raise ValidationError("Somente locacao que saiu para entrega pode ter entrega confirmada.")
        self.status = self.STATUS_ENTREGUE
        self._permitir_alterar_status = True
        self.save(update_fields=["status", "atualizado_em"])
        self._permitir_alterar_status = False
        EventoLocacao.objects.create(
            locacao=self,
            tipo="entregue",
            descricao=str(observacao or "").strip() or "Entrega confirmada. Material esta na rua.",
            responsavel=responsavel,
        )

    def registrar_devolucao(self, retornos, responsavel="", observacao=""):
        if self.status not in {self.STATUS_SAIU_PARA_ENTREGA, self.STATUS_ENTREGUE, self.STATUS_PENDENTE_DEVOLUCAO}:
            raise ValidationError("Somente locacao em entrega ou pendente pode registrar devolucao.")

        houve_avaria = False
        with transaction.atomic():
            itens = list(self.itens.select_for_update().order_by("id"))
            for item in itens:
                dados = retornos.get(item.id, {})
                item.registrar_retorno(
                    devolvida_boa=int(dados.get("devolvida_boa") or 0),
                    quebrada=int(dados.get("quebrada") or 0),
                    perdida=int(dados.get("perdida") or 0),
                    descartada=int(dados.get("descartada") or 0),
                    responsavel=responsavel,
                    observacao=observacao,
                )
                if item.quebrada or item.perdida or item.descartada:
                    houve_avaria = True

            if any(item.tem_pendencia_devolucao() for item in itens):
                novo_status = self.STATUS_PENDENTE_DEVOLUCAO
            elif houve_avaria:
                novo_status = self.STATUS_DEVOLVIDA_COM_AVARIA
            else:
                novo_status = self.STATUS_DEVOLVIDA

            self.status = novo_status
            self._permitir_alterar_status = True
            self.save(update_fields=["status", "atualizado_em"])
            self._permitir_alterar_status = False
            EventoLocacao.objects.create(
                locacao=self,
                tipo="devolucao",
                descricao=str(observacao or "").strip() or "Devolucao registrada.",
                responsavel=responsavel,
            )


class TarefaOperacionalLocacao(models.Model):
    TIPO_ENTREGA = "entrega"
    TIPO_RECOLHIMENTO = "recolhimento"
    TIPO_CHOICES = [
        (TIPO_ENTREGA, "Entrega"),
        (TIPO_RECOLHIMENTO, "Recolhimento"),
    ]

    STATUS_PENDENTE = "pendente"
    STATUS_PARCIAL = "parcial"
    STATUS_CONFIRMADA = "confirmada"
    STATUS_NAO_POSSIVEL = "nao_possivel"
    STATUS_CHOICES = [
        (STATUS_PENDENTE, "Pendente"),
        (STATUS_PARCIAL, "Parcial"),
        (STATUS_CONFIRMADA, "Confirmada"),
        (STATUS_NAO_POSSIVEL, "Nao foi possivel realizar"),
    ]

    locacao = models.ForeignKey(Locacao, on_delete=models.CASCADE, related_name="tarefas_operacionais")
    tipo = models.CharField(max_length=20, choices=TIPO_CHOICES)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDENTE)
    data_agendada = models.DateField()
    horario_agendado = models.TimeField(blank=True, null=True)
    confirmado_em = models.DateTimeField(blank=True, null=True)
    confirmado_por = models.CharField(max_length=120, blank=True)
    tentativa_em = models.DateTimeField(blank=True, null=True)
    tentativa_por = models.CharField(max_length=120, blank=True)
    motivo_nao_realizado = models.TextField(blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["data_agendada", "horario_agendado", "id"]
        unique_together = [("locacao", "tipo")]
        verbose_name = "Tarefa operacional de locacao"
        verbose_name_plural = "Tarefas operacionais de locacao"

    def __str__(self):
        return f"{self.get_tipo_display()} - Locacao #{self.locacao_id}"

    @property
    def pendente_operacional(self):
        return self.status in {
            self.STATUS_PENDENTE,
            self.STATUS_PARCIAL,
            self.STATUS_NAO_POSSIVEL,
        }

    def confirmar(self, responsavel="", observacao=""):
        if self.status == self.STATUS_CONFIRMADA:
            raise ValidationError("Esta tarefa operacional ja foi confirmada.")

        responsavel = str(responsavel or "").strip()
        observacao = str(observacao or "").strip()
        agora = timezone.now()

        with transaction.atomic():
            tarefa = TarefaOperacionalLocacao.objects.select_for_update().select_related("locacao").get(pk=self.pk)
            if tarefa.status == self.STATUS_CONFIRMADA:
                raise ValidationError("Esta tarefa operacional ja foi confirmada.")

            locacao = tarefa.locacao
            if tarefa.tipo == self.TIPO_ENTREGA:
                if locacao.status == Locacao.STATUS_RESERVADA:
                    locacao.marcar_saiu_para_entrega(responsavel=responsavel, observacao="Saida registrada pela checklist operacional.")
                    locacao.refresh_from_db()
                if locacao.status != Locacao.STATUS_SAIU_PARA_ENTREGA:
                    raise ValidationError("Esta locacao nao possui entrega pendente.")
                locacao.confirmar_entrega(responsavel=responsavel, observacao=observacao)
                evento_tipo = "checklist_entrega_confirmada"
                descricao_padrao = "Entrega confirmada pela checklist operacional."
            elif tarefa.tipo == self.TIPO_RECOLHIMENTO:
                if locacao.status not in {Locacao.STATUS_ENTREGUE, Locacao.STATUS_PENDENTE_DEVOLUCAO}:
                    raise ValidationError("Esta locacao nao possui recolhimento pendente.")
                evento_tipo = "checklist_recolhimento_confirmado"
                descricao_padrao = "Recolhimento confirmado pela checklist operacional."
            else:
                raise ValidationError("Tipo de tarefa operacional invalido.")

            tarefa.status = self.STATUS_CONFIRMADA
            tarefa.confirmado_em = agora
            tarefa.confirmado_por = responsavel
            tarefa.save(update_fields=["status", "confirmado_em", "confirmado_por", "atualizado_em"])
            EventoLocacao.objects.create(
                locacao=locacao,
                tipo=evento_tipo,
                descricao=observacao or descricao_padrao,
                responsavel=responsavel,
            )

        self.refresh_from_db()
        return self

    def registrar_nao_possivel(self, motivo="", responsavel=""):
        motivo = str(motivo or "").strip()
        if not motivo:
            raise ValidationError("Informe o motivo/observacao para manter a pendencia.")
        if self.status == self.STATUS_CONFIRMADA:
            raise ValidationError("Esta tarefa ja foi confirmada. Registre uma correcao antes de alterar.")

        self.status = self.STATUS_NAO_POSSIVEL
        self.tentativa_em = timezone.now()
        self.tentativa_por = str(responsavel or "").strip()
        self.motivo_nao_realizado = motivo
        self.save(update_fields=[
            "status",
            "tentativa_em",
            "tentativa_por",
            "motivo_nao_realizado",
            "atualizado_em",
        ])
        EventoLocacao.objects.create(
            locacao=self.locacao,
            tipo=f"checklist_{self.tipo}_nao_possivel",
            descricao=motivo,
            responsavel=self.tentativa_por,
        )
        return self


class ConferenciaEntregaLocacao(models.Model):
    SITUACAO_PARCIAL = "parcial"
    SITUACAO_COMPLETA = "completa"
    SITUACAO_CHOICES = [
        (SITUACAO_PARCIAL, "Entrega parcial"),
        (SITUACAO_COMPLETA, "Entrega completa"),
    ]

    RELACAO_CLIENTE = "cliente"
    RELACAO_FUNCIONARIO = "funcionario"
    RELACAO_CASEIRO = "caseiro"
    RELACAO_FAMILIAR = "familiar"
    RELACAO_OUTRO = "outro"
    RELACAO_CHOICES = [
        (RELACAO_CLIENTE, "Cliente"),
        (RELACAO_FUNCIONARIO, "Funcionario"),
        (RELACAO_CASEIRO, "Caseiro"),
        (RELACAO_FAMILIAR, "Familiar"),
        (RELACAO_OUTRO, "Outro"),
    ]

    ESTADO_BOM = "bom"
    ESTADO_RESSALVA = "ressalva"
    ESTADO_CHOICES = [
        (ESTADO_BOM, "Em bom estado"),
        (ESTADO_RESSALVA, "Entregue com ressalva"),
    ]

    locacao = models.ForeignKey(
        Locacao,
        on_delete=models.CASCADE,
        related_name="conferencias_entrega",
    )
    tarefa = models.ForeignKey(
        TarefaOperacionalLocacao,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="conferencias_entrega",
    )

    previsto_mesas = models.PositiveIntegerField()
    previsto_cadeiras = models.PositiveIntegerField()

    entregue_mesas = models.PositiveIntegerField()
    entregue_cadeiras = models.PositiveIntegerField()

    acumulado_mesas = models.PositiveIntegerField()
    acumulado_cadeiras = models.PositiveIntegerField()

    pendente_mesas = models.PositiveIntegerField()
    pendente_cadeiras = models.PositiveIntegerField()

    situacao = models.CharField(
        max_length=20,
        choices=SITUACAO_CHOICES,
    )

    recebedor_nome = models.CharField(max_length=160)
    recebedor_relacao = models.CharField(
        max_length=20,
        choices=RELACAO_CHOICES,
    )
    recebedor_relacao_outro = models.CharField(
        max_length=120,
        blank=True,
    )

    estado_material = models.CharField(
        max_length=20,
        choices=ESTADO_CHOICES,
    )
    justificativa_parcial = models.TextField(blank=True)
    previsao_conclusao = models.DateTimeField(blank=True, null=True)
    observacao = models.TextField(blank=True)
    responsavel = models.CharField(max_length=120)

    mensagem_whatsapp_snapshot = models.TextField(blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-criado_em", "-id"]
        verbose_name = "Conferencia de entrega de locacao"
        verbose_name_plural = "Conferencias de entrega de locacao"

    def __str__(self):
        return (
            f"{self.get_situacao_display()} - "
            f"Locacao #{self.locacao_id} - Conferencia #{self.id}"
        )

    def clean(self):
        erros = {}

        if self.entregue_mesas == 0 and self.entregue_cadeiras == 0:
            erros["entregue_mesas"] = (
                "Informe pelo menos uma mesa ou cadeira entregue."
            )

        if self.acumulado_mesas > self.previsto_mesas:
            erros["acumulado_mesas"] = (
                "O total acumulado de mesas nao pode superar o previsto."
            )

        if self.acumulado_cadeiras > self.previsto_cadeiras:
            erros["acumulado_cadeiras"] = (
                "O total acumulado de cadeiras nao pode superar o previsto."
            )

        if self.pendente_mesas != self.previsto_mesas - self.acumulado_mesas:
            erros["pendente_mesas"] = (
                "A quantidade pendente de mesas esta inconsistente."
            )

        if (
            self.pendente_cadeiras
            != self.previsto_cadeiras - self.acumulado_cadeiras
        ):
            erros["pendente_cadeiras"] = (
                "A quantidade pendente de cadeiras esta inconsistente."
            )

        if self.situacao == self.SITUACAO_PARCIAL:
            if self.pendente_mesas == 0 and self.pendente_cadeiras == 0:
                erros["situacao"] = (
                    "Entrega parcial precisa possuir material pendente."
                )
            if not str(self.justificativa_parcial or "").strip():
                erros["justificativa_parcial"] = (
                    "Informe a justificativa da entrega parcial."
                )
            if not self.previsao_conclusao:
                erros["previsao_conclusao"] = (
                    "Informe a previsao para completar a entrega."
                )

        if self.situacao == self.SITUACAO_COMPLETA:
            if self.pendente_mesas or self.pendente_cadeiras:
                erros["situacao"] = (
                    "Entrega completa nao pode possuir material pendente."
                )

        if (
            self.recebedor_relacao == self.RELACAO_OUTRO
            and not str(self.recebedor_relacao_outro or "").strip()
        ):
            erros["recebedor_relacao_outro"] = (
                "Informe a relacao da pessoa que recebeu."
            )

        if (
            self.estado_material == self.ESTADO_RESSALVA
            and not str(self.observacao or "").strip()
        ):
            erros["observacao"] = (
                "Explique a ressalva sobre o estado do material."
            )

        if not str(self.recebedor_nome or "").strip():
            erros["recebedor_nome"] = "Informe quem recebeu o material."

        if not str(self.responsavel or "").strip():
            erros["responsavel"] = (
                "Informe o funcionario responsavel pela entrega."
            )

        if erros:
            raise ValidationError(erros)

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError(
                "Uma conferencia de entrega ja registrada nao pode ser alterada."
            )
        self.full_clean()
        super().save(*args, **kwargs)

    @classmethod
    def registrar(cls, tarefa, dados, calculos):
        agora = timezone.now()

        with transaction.atomic():
            tarefa = (
                TarefaOperacionalLocacao.objects
                .select_for_update()
                .select_related("locacao")
                .get(pk=tarefa.pk)
            )
            locacao = (
                Locacao.objects
                .select_for_update()
                .prefetch_related("itens", "conferencias_entrega")
                .get(pk=tarefa.locacao_id)
            )

            if tarefa.tipo != TarefaOperacionalLocacao.TIPO_ENTREGA:
                raise ValidationError(
                    "Esta tarefa nao corresponde a uma entrega."
                )

            if tarefa.status == TarefaOperacionalLocacao.STATUS_CONFIRMADA:
                raise ValidationError(
                    "Esta entrega ja foi confirmada."
                )

            if locacao.status == Locacao.STATUS_RESERVADA:
                locacao.marcar_saiu_para_entrega(
                    responsavel=dados["responsavel"],
                    observacao=(
                        "Saida registrada pelo checklist "
                        "detalhado de entrega."
                    ),
                )
                locacao.refresh_from_db()

            if locacao.status != Locacao.STATUS_SAIU_PARA_ENTREGA:
                raise ValidationError(
                    "Esta locacao nao possui entrega pendente."
                )

            previsto = Locacao.necessidades_itens(
                [
                    {
                        "tipo": item.tipo,
                        "quantidade": item.quantidade,
                    }
                    for item in locacao.itens.all()
                ]
            )

            acumulados = locacao.conferencias_entrega.aggregate(
                mesas=models.Sum("entregue_mesas"),
                cadeiras=models.Sum("entregue_cadeiras"),
            )
            acumulado_anterior_mesas = int(
                acumulados["mesas"] or 0
            )
            acumulado_anterior_cadeiras = int(
                acumulados["cadeiras"] or 0
            )

            entregue_mesas = int(dados["entregue_mesas"])
            entregue_cadeiras = int(dados["entregue_cadeiras"])

            acumulado_mesas = (
                acumulado_anterior_mesas + entregue_mesas
            )
            acumulado_cadeiras = (
                acumulado_anterior_cadeiras + entregue_cadeiras
            )

            if acumulado_mesas > previsto["mesas"]:
                raise ValidationError(
                    "A quantidade entregue de mesas esta maior "
                    "que a prevista no termo."
                )

            if acumulado_cadeiras > previsto["cadeiras"]:
                raise ValidationError(
                    "A quantidade entregue de cadeiras esta maior "
                    "que a prevista no termo."
                )

            if entregue_mesas == 0 and entregue_cadeiras == 0:
                raise ValidationError(
                    "Informe pelo menos uma mesa ou cadeira entregue."
                )

            pendente_mesas = previsto["mesas"] - acumulado_mesas
            pendente_cadeiras = (
                previsto["cadeiras"] - acumulado_cadeiras
            )
            parcial = pendente_mesas > 0 or pendente_cadeiras > 0

            if parcial:
                situacao = cls.SITUACAO_PARCIAL
                if not str(
                    dados.get("justificativa_parcial") or ""
                ).strip():
                    raise ValidationError(
                        "Informe a justificativa da entrega parcial."
                    )
                if not dados.get("previsao_conclusao"):
                    raise ValidationError(
                        "Informe a previsao para completar a entrega."
                    )
            else:
                situacao = cls.SITUACAO_COMPLETA

            relacao_codigo = dados["recebedor_relacao"]
            relacao_texto = dict(cls.RELACAO_CHOICES).get(
                relacao_codigo,
                relacao_codigo,
            )
            if relacao_codigo == cls.RELACAO_OUTRO:
                relacao_texto = dados["recebedor_relacao_outro"]

            linhas = [
                f"Conferencia da locacao #{locacao.id}",
                "",
                (
                    f"Material conferido com "
                    f"{dados['recebedor_nome']}, "
                    f"{str(relacao_texto).lower()}."
                ),
                "",
                (
                    f"Previsto: {previsto['mesas']} mesa(s) "
                    f"e {previsto['cadeiras']} cadeira(s)."
                ),
                (
                    f"Entregue agora: {entregue_mesas} mesa(s) "
                    f"e {entregue_cadeiras} cadeira(s)."
                ),
                (
                    f"Total entregue: {acumulado_mesas} mesa(s) "
                    f"e {acumulado_cadeiras} cadeira(s)."
                ),
                (
                    f"Pendente: {pendente_mesas} mesa(s) "
                    f"e {pendente_cadeiras} cadeira(s)."
                ),
            ]

            if parcial:
                previsao_local = timezone.localtime(
                    dados["previsao_conclusao"]
                )
                linhas.append(
                    "Previsao para completar: "
                    f"{previsao_local:%d/%m/%Y as %H:%M}."
                )

            linhas.extend([
                (
                    "Estado do material: "
                    f"{dict(cls.ESTADO_CHOICES).get(dados['estado_material'])}."
                ),
                (
                    "Responsavel pela entrega: "
                    f"{dados['responsavel']}."
                ),
            ])

            observacao = str(
                dados.get("observacao") or ""
            ).strip()
            if observacao:
                linhas.append(f"Observacao: {observacao}")

            mensagem = "\n".join(linhas)

            conferencia = cls.objects.create(
                locacao=locacao,
                tarefa=tarefa,
                previsto_mesas=previsto["mesas"],
                previsto_cadeiras=previsto["cadeiras"],
                entregue_mesas=entregue_mesas,
                entregue_cadeiras=entregue_cadeiras,
                acumulado_mesas=acumulado_mesas,
                acumulado_cadeiras=acumulado_cadeiras,
                pendente_mesas=pendente_mesas,
                pendente_cadeiras=pendente_cadeiras,
                situacao=situacao,
                recebedor_nome=str(
                    dados["recebedor_nome"]
                ).strip(),
                recebedor_relacao=relacao_codigo,
                recebedor_relacao_outro=str(
                    dados.get("recebedor_relacao_outro") or ""
                ).strip(),
                estado_material=dados["estado_material"],
                justificativa_parcial=str(
                    dados.get("justificativa_parcial") or ""
                ).strip(),
                previsao_conclusao=(
                    dados.get("previsao_conclusao")
                    if parcial
                    else None
                ),
                observacao=observacao,
                responsavel=str(dados["responsavel"]).strip(),
                mensagem_whatsapp_snapshot=mensagem,
            )

            if parcial:
                previsao_local = timezone.localtime(
                    dados["previsao_conclusao"]
                )
                tarefa.status = (
                    TarefaOperacionalLocacao.STATUS_PARCIAL
                )
                tarefa.data_agendada = previsao_local.date()
                tarefa.horario_agendado = previsao_local.time().replace(
                    second=0,
                    microsecond=0,
                )
                tarefa.confirmado_em = None
                tarefa.confirmado_por = ""
                tarefa.save(update_fields=[
                    "status",
                    "data_agendada",
                    "horario_agendado",
                    "confirmado_em",
                    "confirmado_por",
                    "atualizado_em",
                ])
                evento_tipo = "checklist_entrega_parcial"
                descricao = (
                    f"Entrega parcial registrada. Pendente: "
                    f"{pendente_mesas} mesa(s) e "
                    f"{pendente_cadeiras} cadeira(s)."
                )
            else:
                tarefa.status = (
                    TarefaOperacionalLocacao.STATUS_CONFIRMADA
                )
                tarefa.confirmado_em = agora
                tarefa.confirmado_por = str(
                    dados["responsavel"]
                ).strip()
                tarefa.save(update_fields=[
                    "status",
                    "confirmado_em",
                    "confirmado_por",
                    "atualizado_em",
                ])
                locacao.confirmar_entrega(
                    responsavel=dados["responsavel"],
                    observacao=(
                        "Entrega completa confirmada pelo "
                        "checklist detalhado."
                    ),
                )
                evento_tipo = "checklist_entrega_completa"
                descricao = (
                    "Entrega completa confirmada pelo "
                    "checklist detalhado."
                )

            EventoLocacao.objects.create(
                locacao=locacao,
                tipo=evento_tipo,
                descricao=descricao,
                responsavel=dados["responsavel"],
            )

        return conferencia


class ConferenciaRecolhimentoLocacao(models.Model):
    SITUACAO_PARCIAL = "parcial"
    SITUACAO_COMPLETA = "completa"
    SITUACAO_CHOICES = [
        (SITUACAO_PARCIAL, "Recolhimento parcial"),
        (SITUACAO_COMPLETA, "Recolhimento completo"),
    ]

    locacao = models.ForeignKey(
        Locacao,
        on_delete=models.CASCADE,
        related_name="conferencias_recolhimento",
    )
    tarefa = models.ForeignKey(
        TarefaOperacionalLocacao,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="conferencias_recolhimento",
    )

    previsto_mesas = models.PositiveIntegerField()
    previsto_cadeiras = models.PositiveIntegerField()

    boa_mesas = models.PositiveIntegerField(default=0)
    boa_cadeiras = models.PositiveIntegerField(default=0)
    quebrada_mesas = models.PositiveIntegerField(default=0)
    quebrada_cadeiras = models.PositiveIntegerField(default=0)
    perdida_mesas = models.PositiveIntegerField(default=0)
    perdida_cadeiras = models.PositiveIntegerField(default=0)
    descartada_mesas = models.PositiveIntegerField(default=0)
    descartada_cadeiras = models.PositiveIntegerField(default=0)

    acumulado_boa_mesas = models.PositiveIntegerField(default=0)
    acumulado_boa_cadeiras = models.PositiveIntegerField(default=0)
    acumulado_quebrada_mesas = models.PositiveIntegerField(default=0)
    acumulado_quebrada_cadeiras = models.PositiveIntegerField(default=0)
    acumulado_perdida_mesas = models.PositiveIntegerField(default=0)
    acumulado_perdida_cadeiras = models.PositiveIntegerField(default=0)
    acumulado_descartada_mesas = models.PositiveIntegerField(default=0)
    acumulado_descartada_cadeiras = models.PositiveIntegerField(default=0)

    pendente_mesas = models.PositiveIntegerField()
    pendente_cadeiras = models.PositiveIntegerField()

    situacao = models.CharField(
        max_length=20,
        choices=SITUACAO_CHOICES,
    )

    pessoa_local_nome = models.CharField(max_length=160)
    pessoa_local_relacao = models.CharField(
        max_length=20,
        choices=ConferenciaEntregaLocacao.RELACAO_CHOICES,
    )
    pessoa_local_relacao_outro = models.CharField(
        max_length=120,
        blank=True,
    )

    justificativa_parcial = models.TextField(blank=True)
    previsao_conclusao = models.DateTimeField(
        blank=True,
        null=True,
    )
    observacao = models.TextField(blank=True)
    responsavel = models.CharField(max_length=120)

    mensagem_whatsapp_snapshot = models.TextField(blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-criado_em", "-id"]
        verbose_name = "Conferencia de recolhimento de locacao"
        verbose_name_plural = (
            "Conferencias de recolhimento de locacao"
        )

    def __str__(self):
        return (
            f"{self.get_situacao_display()} - "
            f"Locacao #{self.locacao_id} - "
            f"Conferencia #{self.id}"
        )

    @staticmethod
    def totais_entregues(locacao):
        conferencias = locacao.conferencias_entrega.all()

        if conferencias.exists():
            totais = conferencias.aggregate(
                mesas=models.Sum("entregue_mesas"),
                cadeiras=models.Sum("entregue_cadeiras"),
            )
            return {
                "mesas": int(totais["mesas"] or 0),
                "cadeiras": int(totais["cadeiras"] or 0),
            }

        return Locacao.necessidades_itens(
            [
                {
                    "tipo": item.tipo,
                    "quantidade": item.quantidade,
                }
                for item in locacao.itens.all()
            ]
        )

    @staticmethod
    def totais_recolhidos(locacao):
        totais = locacao.conferencias_recolhimento.aggregate(
            boa_mesas=models.Sum("boa_mesas"),
            boa_cadeiras=models.Sum("boa_cadeiras"),
            quebrada_mesas=models.Sum("quebrada_mesas"),
            quebrada_cadeiras=models.Sum("quebrada_cadeiras"),
            perdida_mesas=models.Sum("perdida_mesas"),
            perdida_cadeiras=models.Sum("perdida_cadeiras"),
            descartada_mesas=models.Sum("descartada_mesas"),
            descartada_cadeiras=models.Sum("descartada_cadeiras"),
        )
        return {
            chave: int(valor or 0)
            for chave, valor in totais.items()
        }

    def clean(self):
        erros = {}

        atual_mesas = (
            self.boa_mesas
            + self.quebrada_mesas
            + self.perdida_mesas
            + self.descartada_mesas
        )
        atual_cadeiras = (
            self.boa_cadeiras
            + self.quebrada_cadeiras
            + self.perdida_cadeiras
            + self.descartada_cadeiras
        )

        if atual_mesas == 0 and atual_cadeiras == 0:
            erros["boa_mesas"] = (
                "Informe pelo menos uma mesa ou cadeira."
            )

        encerrado_mesas = (
            self.acumulado_boa_mesas
            + self.acumulado_quebrada_mesas
            + self.acumulado_perdida_mesas
            + self.acumulado_descartada_mesas
        )
        encerrado_cadeiras = (
            self.acumulado_boa_cadeiras
            + self.acumulado_quebrada_cadeiras
            + self.acumulado_perdida_cadeiras
            + self.acumulado_descartada_cadeiras
        )

        if encerrado_mesas > self.previsto_mesas:
            erros["pendente_mesas"] = (
                "O recolhimento de mesas supera o entregue."
            )

        if encerrado_cadeiras > self.previsto_cadeiras:
            erros["pendente_cadeiras"] = (
                "O recolhimento de cadeiras supera o entregue."
            )

        if (
            self.pendente_mesas
            != self.previsto_mesas - encerrado_mesas
        ):
            erros["pendente_mesas"] = (
                "A quantidade pendente de mesas esta inconsistente."
            )

        if (
            self.pendente_cadeiras
            != self.previsto_cadeiras - encerrado_cadeiras
        ):
            erros["pendente_cadeiras"] = (
                "A quantidade pendente de cadeiras esta inconsistente."
            )

        if self.situacao == self.SITUACAO_PARCIAL:
            if not self.pendente_mesas and not self.pendente_cadeiras:
                erros["situacao"] = (
                    "Recolhimento parcial precisa ter material pendente."
                )
            if not str(
                self.justificativa_parcial or ""
            ).strip():
                erros["justificativa_parcial"] = (
                    "Informe a justificativa do recolhimento parcial."
                )
            if not self.previsao_conclusao:
                erros["previsao_conclusao"] = (
                    "Informe quando o recolhimento sera concluido."
                )

        if self.situacao == self.SITUACAO_COMPLETA:
            if self.pendente_mesas or self.pendente_cadeiras:
                erros["situacao"] = (
                    "Recolhimento completo nao pode ter pendencia."
                )

        if not str(self.pessoa_local_nome or "").strip():
            erros["pessoa_local_nome"] = (
                "Informe com quem o material foi conferido."
            )

        if (
            self.pessoa_local_relacao
            == ConferenciaEntregaLocacao.RELACAO_OUTRO
            and not str(
                self.pessoa_local_relacao_outro or ""
            ).strip()
        ):
            erros["pessoa_local_relacao_outro"] = (
                "Informe a relacao da pessoa com o cliente."
            )

        if not str(self.responsavel or "").strip():
            erros["responsavel"] = (
                "Informe o funcionario responsavel."
            )

        if erros:
            raise ValidationError(erros)

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError(
                "Uma conferencia de recolhimento registrada "
                "nao pode ser alterada."
            )
        self.full_clean()
        super().save(*args, **kwargs)

    @classmethod
    def registrar(cls, tarefa, dados):
        agora = timezone.now()

        with transaction.atomic():
            tarefa = (
                TarefaOperacionalLocacao.objects
                .select_for_update()
                .select_related("locacao")
                .get(pk=tarefa.pk)
            )
            locacao = (
                Locacao.objects
                .select_for_update()
                .prefetch_related(
                    "itens",
                    "conferencias_entrega",
                    "conferencias_recolhimento",
                )
                .get(pk=tarefa.locacao_id)
            )

            if (
                tarefa.tipo
                != TarefaOperacionalLocacao.TIPO_RECOLHIMENTO
            ):
                raise ValidationError(
                    "Esta tarefa nao corresponde a um recolhimento."
                )

            if (
                tarefa.status
                == TarefaOperacionalLocacao.STATUS_CONFIRMADA
            ):
                raise ValidationError(
                    "Este recolhimento ja foi concluido."
                )

            if locacao.status not in {
                Locacao.STATUS_ENTREGUE,
                Locacao.STATUS_PENDENTE_DEVOLUCAO,
            }:
                raise ValidationError(
                    "Esta locacao nao possui recolhimento pendente."
                )

            previsto = cls.totais_entregues(locacao)
            anterior = cls.totais_recolhidos(locacao)

            atuais = {
                "boa_mesas": int(dados.get("boa_mesas") or 0),
                "boa_cadeiras": int(
                    dados.get("boa_cadeiras") or 0
                ),
                "quebrada_mesas": int(
                    dados.get("quebrada_mesas") or 0
                ),
                "quebrada_cadeiras": int(
                    dados.get("quebrada_cadeiras") or 0
                ),
                "perdida_mesas": int(
                    dados.get("perdida_mesas") or 0
                ),
                "perdida_cadeiras": int(
                    dados.get("perdida_cadeiras") or 0
                ),
                "descartada_mesas": int(
                    dados.get("descartada_mesas") or 0
                ),
                "descartada_cadeiras": int(
                    dados.get("descartada_cadeiras") or 0
                ),
            }

            if any(valor < 0 for valor in atuais.values()):
                raise ValidationError(
                    "As quantidades nao podem ser negativas."
                )

            atual_total_mesas = sum(
                valor
                for chave, valor in atuais.items()
                if chave.endswith("_mesas")
            )
            atual_total_cadeiras = sum(
                valor
                for chave, valor in atuais.items()
                if chave.endswith("_cadeiras")
            )

            if atual_total_mesas == 0 and atual_total_cadeiras == 0:
                raise ValidationError(
                    "Informe pelo menos uma mesa ou cadeira."
                )

            acumulados = {
                chave: anterior[chave] + atuais[chave]
                for chave in atuais
            }

            encerrado_mesas = sum(
                valor
                for chave, valor in acumulados.items()
                if chave.endswith("_mesas")
            )
            encerrado_cadeiras = sum(
                valor
                for chave, valor in acumulados.items()
                if chave.endswith("_cadeiras")
            )

            if encerrado_mesas > previsto["mesas"]:
                raise ValidationError(
                    "A quantidade de mesas supera o total entregue."
                )

            if encerrado_cadeiras > previsto["cadeiras"]:
                raise ValidationError(
                    "A quantidade de cadeiras supera o total entregue."
                )

            pendente_mesas = previsto["mesas"] - encerrado_mesas
            pendente_cadeiras = (
                previsto["cadeiras"] - encerrado_cadeiras
            )
            parcial = bool(
                pendente_mesas or pendente_cadeiras
            )

            justificativa = str(
                dados.get("justificativa_parcial") or ""
            ).strip()
            previsao = dados.get("previsao_conclusao")

            if parcial and not justificativa:
                raise ValidationError(
                    "Informe a justificativa do recolhimento parcial."
                )

            if parcial and not previsao:
                raise ValidationError(
                    "Informe quando o recolhimento sera concluido."
                )

            relacao_codigo = dados["pessoa_local_relacao"]
            relacao_texto = dict(
                ConferenciaEntregaLocacao.RELACAO_CHOICES
            ).get(relacao_codigo, relacao_codigo)

            if (
                relacao_codigo
                == ConferenciaEntregaLocacao.RELACAO_OUTRO
            ):
                relacao_texto = str(
                    dados.get("pessoa_local_relacao_outro") or ""
                ).strip()

            linhas = [
                f"Conferencia do recolhimento da locacao #{locacao.id}",
                "",
                (
                    "Material conferido com "
                    f"{dados['pessoa_local_nome']}, "
                    f"{str(relacao_texto).lower()}."
                ),
                "",
                (
                    f"Entregue ao cliente: "
                    f"{previsto['mesas']} mesa(s) e "
                    f"{previsto['cadeiras']} cadeira(s)."
                ),
                (
                    f"Recolhido em bom estado agora: "
                    f"{atuais['boa_mesas']} mesa(s) e "
                    f"{atuais['boa_cadeiras']} cadeira(s)."
                ),
                (
                    f"Quebrado: "
                    f"{atuais['quebrada_mesas']} mesa(s) e "
                    f"{atuais['quebrada_cadeiras']} cadeira(s)."
                ),
                (
                    f"Perdido: "
                    f"{atuais['perdida_mesas']} mesa(s) e "
                    f"{atuais['perdida_cadeiras']} cadeira(s)."
                ),
                (
                    f"Descartado: "
                    f"{atuais['descartada_mesas']} mesa(s) e "
                    f"{atuais['descartada_cadeiras']} cadeira(s)."
                ),
                (
                    f"Pendente: {pendente_mesas} mesa(s) e "
                    f"{pendente_cadeiras} cadeira(s)."
                ),
            ]

            if parcial:
                previsao_local = timezone.localtime(previsao)
                linhas.append(
                    "Previsao para concluir: "
                    f"{previsao_local:%d/%m/%Y as %H:%M}."
                )

            observacao = str(
                dados.get("observacao") or ""
            ).strip()
            if observacao:
                linhas.append(f"Observacao: {observacao}")

            responsavel = str(
                dados["responsavel"]
            ).strip()
            linhas.append(
                f"Responsavel pelo recolhimento: {responsavel}."
            )

            conferencia = cls.objects.create(
                locacao=locacao,
                tarefa=tarefa,
                previsto_mesas=previsto["mesas"],
                previsto_cadeiras=previsto["cadeiras"],
                pendente_mesas=pendente_mesas,
                pendente_cadeiras=pendente_cadeiras,
                situacao=(
                    cls.SITUACAO_PARCIAL
                    if parcial
                    else cls.SITUACAO_COMPLETA
                ),
                pessoa_local_nome=str(
                    dados["pessoa_local_nome"]
                ).strip(),
                pessoa_local_relacao=relacao_codigo,
                pessoa_local_relacao_outro=str(
                    dados.get(
                        "pessoa_local_relacao_outro"
                    ) or ""
                ).strip(),
                justificativa_parcial=justificativa,
                previsao_conclusao=(
                    previsao if parcial else None
                ),
                observacao=observacao,
                responsavel=responsavel,
                mensagem_whatsapp_snapshot="\n".join(linhas),
                **atuais,
                **{
                    f"acumulado_{chave}": valor
                    for chave, valor in acumulados.items()
                },
            )

            baixas = [
                (
                    "quebrada",
                    MovimentoEstoqueLocacao.TIPO_BAIXA_QUEBRA,
                ),
                (
                    "perdida",
                    MovimentoEstoqueLocacao.TIPO_BAIXA_PERDA,
                ),
                (
                    "descartada",
                    MovimentoEstoqueLocacao.TIPO_BAIXA_DESCARTE,
                ),
            ]

            for prefixo, tipo_movimento in baixas:
                quantidade_mesas = atuais[
                    f"{prefixo}_mesas"
                ]
                quantidade_cadeiras = atuais[
                    f"{prefixo}_cadeiras"
                ]

                if quantidade_mesas:
                    MovimentoEstoqueLocacao.registrar(
                        item=MovimentoEstoqueLocacao.ITEM_MESA,
                        tipo=tipo_movimento,
                        quantidade=quantidade_mesas,
                        responsavel=responsavel,
                        observacao=(
                            observacao
                            or f"Recolhimento da locacao #{locacao.id}."
                        ),
                        locacao=locacao,
                    )

                if quantidade_cadeiras:
                    MovimentoEstoqueLocacao.registrar(
                        item=MovimentoEstoqueLocacao.ITEM_CADEIRA,
                        tipo=tipo_movimento,
                        quantidade=quantidade_cadeiras,
                        responsavel=responsavel,
                        observacao=(
                            observacao
                            or f"Recolhimento da locacao #{locacao.id}."
                        ),
                        locacao=locacao,
                    )

            if parcial:
                previsao_local = timezone.localtime(previsao)
                tarefa.status = (
                    TarefaOperacionalLocacao.STATUS_PARCIAL
                )
                tarefa.data_agendada = previsao_local.date()
                tarefa.horario_agendado = (
                    previsao_local.time().replace(
                        second=0,
                        microsecond=0,
                    )
                )
                tarefa.confirmado_em = None
                tarefa.confirmado_por = ""
                locacao.status = (
                    Locacao.STATUS_PENDENTE_DEVOLUCAO
                )
                evento_tipo = "checklist_recolhimento_parcial"
                descricao = (
                    "Recolhimento parcial registrado. "
                    f"Pendente: {pendente_mesas} mesa(s) e "
                    f"{pendente_cadeiras} cadeira(s)."
                )
            else:
                tarefa.status = (
                    TarefaOperacionalLocacao.STATUS_CONFIRMADA
                )
                tarefa.confirmado_em = agora
                tarefa.confirmado_por = responsavel

                houve_avaria = any(
                    acumulados[chave] > 0
                    for chave in acumulados
                    if (
                        chave.startswith("quebrada_")
                        or chave.startswith("perdida_")
                        or chave.startswith("descartada_")
                    )
                )
                locacao.status = (
                    Locacao.STATUS_DEVOLVIDA_COM_AVARIA
                    if houve_avaria
                    else Locacao.STATUS_DEVOLVIDA
                )
                evento_tipo = "checklist_recolhimento_completo"
                descricao = (
                    "Recolhimento completo registrado pelo "
                    "checklist detalhado."
                )

            tarefa.save(update_fields=[
                "status",
                "data_agendada",
                "horario_agendado",
                "confirmado_em",
                "confirmado_por",
                "atualizado_em",
            ])

            locacao._permitir_alterar_status = True
            locacao.save(
                update_fields=["status", "atualizado_em"]
            )
            locacao._permitir_alterar_status = False

            EventoLocacao.objects.create(
                locacao=locacao,
                tipo=evento_tipo,
                descricao=descricao,
                responsavel=responsavel,
            )

        return conferencia


class ItemLocacao(models.Model):
    TIPO_JOGO = "jogo"
    TIPO_MESA_AVULSA = "mesa_avulsa"
    TIPO_CADEIRA_AVULSA = "cadeira_avulsa"
    TIPO_CHOICES = [
        (TIPO_JOGO, "Jogo"),
        (TIPO_MESA_AVULSA, "Mesa avulsa"),
        (TIPO_CADEIRA_AVULSA, "Cadeira avulsa"),
    ]

    locacao = models.ForeignKey(Locacao, on_delete=models.CASCADE, related_name="itens")
    tipo = models.CharField(max_length=30, choices=TIPO_CHOICES)
    quantidade = models.PositiveIntegerField()
    preco_diaria_snapshot = models.DecimalField(max_digits=10, decimal_places=2)
    diarias = models.PositiveIntegerField()
    valor_total = models.DecimalField(max_digits=12, decimal_places=2)
    ajuste_manual = models.BooleanField(default=False)
    devolvida_boa = models.PositiveIntegerField(default=0)
    quebrada = models.PositiveIntegerField(default=0)
    perdida = models.PositiveIntegerField(default=0)
    descartada = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["id"]
        verbose_name = "Item de locacao"
        verbose_name_plural = "Itens de locacao"

    def __str__(self):
        return f"{self.get_tipo_display()} - Locacao #{self.locacao_id}"

    def necessidade_estoque(self):
        if self.tipo == self.TIPO_JOGO:
            return {
                "mesas": self.quantidade * ConfiguracaoLocacao.JOGO_MESAS,
                "cadeiras": self.quantidade * ConfiguracaoLocacao.JOGO_CADEIRAS,
            }
        if self.tipo == self.TIPO_MESA_AVULSA:
            return {"mesas": self.quantidade, "cadeiras": 0}
        if self.tipo == self.TIPO_CADEIRA_AVULSA:
            return {"mesas": 0, "cadeiras": self.quantidade}
        return {"mesas": 0, "cadeiras": 0}

    def quantidade_encerrada(self):
        return self.devolvida_boa + self.quebrada + self.perdida + self.descartada

    def quantidade_pendente(self):
        return max(self.quantidade - self.quantidade_encerrada(), 0)

    def tem_pendencia_devolucao(self):
        return self.quantidade_pendente() > 0

    def necessidade_pendente(self):
        return self._necessidade_para_quantidade(self.quantidade_pendente())

    def _necessidade_para_quantidade(self, quantidade):
        if self.tipo == self.TIPO_JOGO:
            return {
                "mesas": quantidade * ConfiguracaoLocacao.JOGO_MESAS,
                "cadeiras": quantidade * ConfiguracaoLocacao.JOGO_CADEIRAS,
            }
        if self.tipo == self.TIPO_MESA_AVULSA:
            return {"mesas": quantidade, "cadeiras": 0}
        if self.tipo == self.TIPO_CADEIRA_AVULSA:
            return {"mesas": 0, "cadeiras": quantidade}
        return {"mesas": 0, "cadeiras": 0}

    def registrar_retorno(self, devolvida_boa=0, quebrada=0, perdida=0, descartada=0, responsavel="", observacao=""):
        incrementos = {
            "devolvida_boa": int(devolvida_boa or 0),
            "quebrada": int(quebrada or 0),
            "perdida": int(perdida or 0),
            "descartada": int(descartada or 0),
        }
        if any(valor < 0 for valor in incrementos.values()):
            raise ValidationError("Quantidades de devolucao nao podem ser negativas.")
        if sum(incrementos.values()) > self.quantidade_pendente():
            raise ValidationError("A devolucao nao pode superar a quantidade pendente do item.")

        baixas = [
            ("quebrada", MovimentoEstoqueLocacao.TIPO_BAIXA_QUEBRA),
            ("perdida", MovimentoEstoqueLocacao.TIPO_BAIXA_PERDA),
            ("descartada", MovimentoEstoqueLocacao.TIPO_BAIXA_DESCARTE),
        ]
        for campo, tipo_movimento in baixas:
            quantidade = incrementos[campo]
            if quantidade <= 0:
                continue
            necessidade = self._necessidade_para_quantidade(quantidade)
            if necessidade["mesas"]:
                MovimentoEstoqueLocacao.registrar(
                    item=MovimentoEstoqueLocacao.ITEM_MESA,
                    tipo=tipo_movimento,
                    quantidade=necessidade["mesas"],
                    responsavel=responsavel or "Sistema",
                    observacao=observacao or f"Baixa vinculada a locacao #{self.locacao_id}.",
                    locacao=self.locacao,
                    item_locacao=self,
                )
            if necessidade["cadeiras"]:
                MovimentoEstoqueLocacao.registrar(
                    item=MovimentoEstoqueLocacao.ITEM_CADEIRA,
                    tipo=tipo_movimento,
                    quantidade=necessidade["cadeiras"],
                    responsavel=responsavel or "Sistema",
                    observacao=observacao or f"Baixa vinculada a locacao #{self.locacao_id}.",
                    locacao=self.locacao,
                    item_locacao=self,
                )

        for campo, valor in incrementos.items():
            setattr(self, campo, getattr(self, campo) + valor)
        self.save(update_fields=["devolvida_boa", "quebrada", "perdida", "descartada"])


class EventoLocacao(models.Model):
    locacao = models.ForeignKey(Locacao, on_delete=models.CASCADE, related_name="eventos")
    tipo = models.CharField(max_length=40)
    descricao = models.TextField(blank=True)
    responsavel = models.CharField(max_length=120, blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-criado_em", "-id"]
        verbose_name = "Evento de locacao"
        verbose_name_plural = "Eventos de locacao"

    def __str__(self):
        return f"{self.tipo} - Locacao #{self.locacao_id}"


class PagamentoLocacao(models.Model):
    FORMA_DINHEIRO = "dinheiro"
    FORMA_PIX = "pix"
    FORMA_CARTAO = "cartao"
    FORMA_OUTRO = "outro"
    FORMA_CHOICES = [
        (FORMA_DINHEIRO, "Dinheiro"),
        (FORMA_PIX, "Pix"),
        (FORMA_CARTAO, "Cartao"),
        (FORMA_OUTRO, "Outro"),
    ]

    RECIBO_PENDENTE = "pendente"
    RECIBO_ENVIADO = "enviado"
    RECIBO_DISPENSADO = "dispensado"
    RECIBO_STATUS_CHOICES = [
        (RECIBO_PENDENTE, "Pendente"),
        (RECIBO_ENVIADO, "Enviado"),
        (RECIBO_DISPENSADO, "Dispensado / Nao enviado"),
    ]

    locacao = models.ForeignKey(Locacao, on_delete=models.CASCADE, related_name="pagamentos")
    valor = models.DecimalField(max_digits=12, decimal_places=2)
    data_hora = models.DateTimeField(default=timezone.now)
    forma_pagamento = models.CharField(max_length=20, choices=FORMA_CHOICES)
    observacao = models.TextField(blank=True)
    responsavel = models.CharField(max_length=120, blank=True)
    movimento_financeiro = models.OneToOneField(
        MovimentoFinanceiro,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="pagamento_locacao",
    )
    recibo_status = models.CharField(max_length=20, choices=RECIBO_STATUS_CHOICES, default=RECIBO_PENDENTE)
    recibo_enviado_em = models.DateTimeField(blank=True, null=True)
    recibo_enviado_por = models.CharField(max_length=120, blank=True)
    recibo_dispensado_em = models.DateTimeField(blank=True, null=True)
    recibo_dispensado_por = models.CharField(max_length=120, blank=True)
    recibo_dispensa_observacao = models.TextField(blank=True)
    recibo_token = models.UUIDField(
    default=uuid.uuid4,
    unique=True,
    editable=False,
    db_index=True,
)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-data_hora", "-id"]
        verbose_name = "Pagamento de locacao"
        verbose_name_plural = "Pagamentos de locacao"

    def __str__(self):
        return f"Pagamento locacao #{self.locacao_id} - R$ {self.valor}"

    def clean(self):
        if self.valor is not None and self.valor <= Decimal("0.00"):
            raise ValidationError({"valor": "Informe um valor maior que zero."})

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    @staticmethod
    def conta_financeira_para_forma(forma_pagamento):
        if forma_pagamento == PagamentoLocacao.FORMA_DINHEIRO:
            conta = (
                ContaFinanceira.objects
                .filter(
                    ativo=True,
                    tipo=ContaFinanceira.TIPO_CAIXA,
                    nome__in=["Caixa em especie", "Caixa em espécie"],
                )
                .order_by("id")
                .first()
            )
            if conta:
                return conta
            return ContaFinanceira.objects.create(
                nome="Caixa em especie",
                tipo=ContaFinanceira.TIPO_CAIXA,
                saldo_inicial=Decimal("0.00"),
                ativo=True,
            )
        conta = ContaFinanceira.objects.filter(
            ativo=True,
            tipo=ContaFinanceira.TIPO_BANCO,
            nome="Banco/Pix",
        ).order_by("id").first()
        if conta:
            return conta
        return ContaFinanceira.objects.create(
            nome="Banco/Pix",
            tipo=ContaFinanceira.TIPO_BANCO,
            saldo_inicial=Decimal("0.00"),
            ativo=True,
        )

    def criar_movimento_financeiro(self):
        if self.movimento_financeiro_id:
            return self.movimento_financeiro
        conta = self.conta_financeira_para_forma(self.forma_pagamento)
        if not conta:
            return None
        movimento = MovimentoFinanceiro.objects.create(
            conta=conta,
            tipo=MovimentoFinanceiro.TIPO_ENTRADA,
            valor=self.valor,
            data=timezone.localtime(self.data_hora).date(),
            descricao=f"Receita de locacao #{self.locacao_id} - {self.locacao.nome_contratante}",
            operador=self.responsavel,
            origem="locacao",
        )
        self.movimento_financeiro = movimento
        self.save(update_fields=["movimento_financeiro"])
        return movimento

    def confirmar_recibo_enviado(self, responsavel=""):
        if self.recibo_status == self.RECIBO_ENVIADO:
            return
        self.recibo_status = self.RECIBO_ENVIADO
        self.recibo_enviado_em = timezone.now()
        self.recibo_enviado_por = str(responsavel or "").strip()
        self.save(update_fields=["recibo_status", "recibo_enviado_em", "recibo_enviado_por"])
        EventoLocacao.objects.create(
            locacao=self.locacao,
            tipo="recibo_enviado",
            descricao=f"Recibo do pagamento #{self.id} confirmado como enviado.",
            responsavel=self.recibo_enviado_por,
        )

    def dispensar_recibo(self, responsavel="", observacao=""):
        self.recibo_status = self.RECIBO_DISPENSADO
        self.recibo_dispensado_em = timezone.now()
        self.recibo_dispensado_por = str(responsavel or "").strip()
        self.recibo_dispensa_observacao = str(observacao or "").strip()
        self.save(update_fields=[
            "recibo_status",
            "recibo_dispensado_em",
            "recibo_dispensado_por",
            "recibo_dispensa_observacao",
        ])
        EventoLocacao.objects.create(
            locacao=self.locacao,
            tipo="recibo_dispensado",
            descricao=self.recibo_dispensa_observacao or f"Recibo do pagamento #{self.id} dispensado.",
            responsavel=self.recibo_dispensado_por,
        )


class RegistroCobrancaLocacao(models.Model):
    TIPO_WHATSAPP = "whatsapp"
    TIPO_TELEFONE = "telefone"
    TIPO_VISITA = "visita"
    TIPO_OUTRO = "outro"
    TIPO_CHOICES = [
        (TIPO_WHATSAPP, "WhatsApp"),
        (TIPO_TELEFONE, "Ligacao"),
        (TIPO_VISITA, "Visita"),
        (TIPO_OUTRO, "Outro"),
    ]

    STATUS_CONTATADO = "contatado"
    STATUS_PENDENTE = "pendente"
    STATUS_SEM_RESPOSTA = "sem_resposta"
    STATUS_PROMESSA_PAGAMENTO = "promessa_pagamento"
    STATUS_RESOLVIDO = "resolvido"
    STATUS_OUTRO = "outro"
    STATUS_CHOICES = [
        (STATUS_PENDENTE, "Pendente"),
        (STATUS_CONTATADO, "Contatado"),
        (STATUS_SEM_RESPOSTA, "Nao atendeu"),
        (STATUS_PROMESSA_PAGAMENTO, "Promessa de pagamento"),
        (STATUS_RESOLVIDO, "Resolvido"),
        (STATUS_OUTRO, "Outro"),
    ]

    locacao = models.ForeignKey(Locacao, on_delete=models.CASCADE, related_name="registros_cobranca")
    tipo = models.CharField(max_length=20, choices=TIPO_CHOICES)
    status = models.CharField(max_length=30, choices=STATUS_CHOICES)
    observacao = models.TextField(blank=True)
    criado_por_nome = models.CharField(max_length=150, blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-criado_em", "-id"]
        verbose_name = "Registro de cobranca de locacao"
        verbose_name_plural = "Registros de cobranca de locacoes"

    def __str__(self):
        return f"Cobranca locacao #{self.locacao_id} - {self.get_tipo_display()}"
