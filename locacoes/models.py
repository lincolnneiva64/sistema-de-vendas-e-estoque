from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone

from estoque.models import Cliente


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
    def registrar(cls, item, tipo, quantidade=None, responsavel="", observacao="", saldo_contado=None):
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
    STATUS_CANCELADA = "cancelada"
    STATUS_CHOICES = [
        (STATUS_RESERVADA, "Reservada"),
        (STATUS_CANCELADA, "Cancelada"),
    ]

    TIPO_PESSOA_CLIENTE = "cliente"
    TIPO_PESSOA_AVULSA = "avulsa"
    TIPO_PESSOA_CHOICES = [
        (TIPO_PESSOA_CLIENTE, "Cliente cadastrado"),
        (TIPO_PESSOA_AVULSA, "Pessoa avulsa"),
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
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)
    cancelada_em = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ["-data_entrega", "-id"]
        verbose_name = "Locacao"
        verbose_name_plural = "Locacoes"

    def __str__(self):
        return f"Locacao #{self.id}"

    @property
    def nome_contratante(self):
        if self.cliente_id and self.cliente:
            return self.cliente.nome
        return self.pessoa_avulsa_nome or "Pessoa avulsa"

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
        locacoes = cls.objects.filter(status=cls.STATUS_RESERVADA).filter(
            cls.periodo_conflita_q(data_entrega, data_prevista_devolucao)
        )
        if excluir_id:
            locacoes = locacoes.exclude(pk=excluir_id)

        mesas = 0
        cadeiras = 0
        for item in ItemLocacao.objects.filter(locacao__in=locacoes):
            necessidade = item.necessidade_estoque()
            mesas += necessidade["mesas"]
            cadeiras += necessidade["cadeiras"]
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
                faixa_preco=dados["faixa_preco"],
                faixa_preco_nome_snapshot=dados["faixa_preco"].nome,
                observacao=dados.get("observacao", ""),
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
            locacao.save(update_fields=["total", "atualizado_em"])
            EventoLocacao.objects.create(
                locacao=locacao,
                tipo="criada",
                descricao="Reserva de locacao criada. Material ainda nao saiu para entrega.",
                responsavel=responsavel,
            )
            return locacao

    def cancelar(self, motivo="", responsavel=""):
        if self.status == self.STATUS_CANCELADA:
            return
        self.status = self.STATUS_CANCELADA
        self.motivo_cancelamento = str(motivo or "").strip()
        self.cancelada_em = timezone.now()
        self.save(update_fields=["status", "motivo_cancelamento", "cancelada_em", "atualizado_em"])
        EventoLocacao.objects.create(
            locacao=self,
            tipo="cancelada",
            descricao=self.motivo_cancelamento or "Reserva cancelada.",
            responsavel=responsavel,
        )


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
