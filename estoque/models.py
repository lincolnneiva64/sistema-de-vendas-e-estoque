from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.core.exceptions import ValidationError
from django.utils import timezone
from .utils import normalize_category_name
class Produto(models.Model):

    nome = models.CharField(max_length=120)
    codigo = models.CharField(max_length=50, blank=True, null=True)
    codigo_legado = models.CharField(
        max_length=50,
        null=True,
        blank=True,
        unique=True,
        db_index=True,
    )
    categoria = models.CharField(max_length=60, blank=True, null=True)

    preco_compra = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    preco_venda = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    preco_vista = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    preco_prazo = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    unidade_compra = models.CharField(max_length=20, blank=True, null=True)
    fator_conversao = models.DecimalField(max_digits=10, decimal_places=2, default=1)
    preco_compra_fracionado = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    unidade_venda_1 = models.CharField(max_length=20, blank=True, null=True)
    preco_venda_1 = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    unidade_venda_2 = models.CharField(max_length=20, blank=True, null=True)
    preco_venda_2 = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    percentual_vista_fracionado = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    preco_vista_fracionado = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    percentual_prazo_fracionado = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    preco_prazo_fracionado = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    vende_fracionado = models.BooleanField(default=False)
    descricao_conversao = models.CharField(max_length=120, blank=True, null=True)
    permitir_prejuizo = models.BooleanField(default=False)
    motivo_prejuizo = models.CharField(max_length=200, blank=True, null=True)

    quantidade = models.DecimalField(max_digits=12, decimal_places=3, blank=True, null=True)
    estoque_minimo = models.PositiveIntegerField(blank=True, null=True)
    fornecedor = models.CharField(max_length=120, blank=True, null=True)
    ativo = models.BooleanField(default=True)
    excluido = models.BooleanField(default=False)
    excluido_em = models.DateTimeField(null=True, blank=True)
    cadastro_incompleto = models.BooleanField(default=False)
    revisado_importacao = models.BooleanField(
        default=False,
        db_index=True,
    )
    revisado_importacao_em = models.DateTimeField(
        null=True,
        blank=True,
    )
    estoque_conferido = models.BooleanField(default=False, db_index=True)
    estoque_conferido_em = models.DateTimeField(null=True, blank=True)
    estoque_conferido_por = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="produtos_conferidos_estoque",
    )
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.nome

    def clean(self):

        if self.preco_vista <= 0:
         raise ValidationError("O preço à vista deve ser maior que zero.")

        if self.preco_prazo <= 0:
         raise ValidationError("O preço a prazo deve ser maior que zero.")

        if not self.permitir_prejuizo and self.preco_vista <= self.preco_compra:
         raise ValidationError("O preço à vista deve ser MAIOR que o preço de compra.")

        if not self.permitir_prejuizo and self.preco_prazo <= self.preco_compra:
         raise ValidationError("O preço a prazo deve ser MAIOR que o preço de compra.")

        if self.permitir_prejuizo and not self.motivo_prejuizo:
         raise ValidationError("Informe o motivo ao permitir venda com prejuízo.")
    def save(self, *args, **kwargs):

        if self.nome:
            nome_limpo = " ".join(self.nome.strip().split())
            self.nome = nome_limpo.title()

        if self.categoria:
            self.categoria = normalize_category_name(self.categoria)

        if self.fornecedor:
            fornecedor_limpo = " ".join(self.fornecedor.strip().split())
            self.fornecedor = fornecedor_limpo.title()
        if self.unidade_compra:
            self.unidade_venda_1 = self.unidade_compra
        if self.preco_vista and self.preco_vista > 0:
           self.preco_venda = self.preco_vista
        if not self.vende_fracionado:
            self.fator_conversao = self.fator_conversao or 0
            self.preco_compra_fracionado = self.preco_compra_fracionado or 0
            self.unidade_venda_2 = self.unidade_venda_2 or ""
            self.percentual_vista_fracionado = self.percentual_vista_fracionado or 0
            self.preco_vista_fracionado = self.preco_vista_fracionado or 0
            self.percentual_prazo_fracionado = self.percentual_prazo_fracionado or 0
            self.preco_prazo_fracionado = self.preco_prazo_fracionado or 0
        self.full_clean()
        super().save(*args, **kwargs)


class MovimentacaoEstoqueManual(models.Model):
    TIPO_CONFERENCIA = "conferencia"
    TIPO_AJUSTE = "ajuste"
    TIPO_CHOICES = [
        (TIPO_CONFERENCIA, "Conferencia"),
        (TIPO_AJUSTE, "Ajuste manual"),
    ]

    produto = models.ForeignKey(
        Produto,
        on_delete=models.PROTECT,
        related_name="movimentacoes_estoque_manuais",
    )
    tipo = models.CharField(max_length=20, choices=TIPO_CHOICES)
    estoque_antes = models.DecimalField(max_digits=12, decimal_places=3)
    estoque_depois = models.DecimalField(max_digits=12, decimal_places=3)
    diferenca = models.DecimalField(max_digits=12, decimal_places=3)
    motivo = models.TextField(blank=True)
    observacao = models.TextField(blank=True)
    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="movimentacoes_estoque_manuais",
    )
    operador_nome = models.CharField(max_length=150, blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-criado_em", "-id"]

    def __str__(self):
        return f"{self.get_tipo_display()} - {self.produto.nome}"


class Unidade(models.Model):
    nome = models.CharField(max_length=60)
    sigla = models.CharField(max_length=20, unique=True)
    descricao = models.CharField(max_length=255, blank=True, null=True)
    ativa = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.nome} ({self.sigla})"

class Categoria(models.Model):
    nome = models.CharField(max_length=60, unique=True)
    descricao = models.CharField(max_length=255, blank=True, null=True)
    ativa = models.BooleanField(default=True)

    def __str__(self):
        return self.nome


class Cliente(models.Model):
    codigo_legado = models.CharField(
        max_length=20,
        blank=True,
        null=True,
        unique=True,
        db_index=True,
    )

    TIPO_CHAVE_PIX_CPF = "cpf"
    TIPO_CHAVE_PIX_CNPJ = "cnpj"
    TIPO_CHAVE_PIX_TELEFONE = "telefone"
    TIPO_CHAVE_PIX_EMAIL = "email"
    TIPO_CHAVE_PIX_ALEATORIA = "aleatoria"
    TIPO_CHAVE_PIX_OUTRO = "outro"

    TIPO_CHAVE_PIX_CHOICES = [
        (TIPO_CHAVE_PIX_CPF, "CPF"),
        (TIPO_CHAVE_PIX_CNPJ, "CNPJ"),
        (TIPO_CHAVE_PIX_TELEFONE, "Telefone"),
        (TIPO_CHAVE_PIX_EMAIL, "Email"),
        (TIPO_CHAVE_PIX_ALEATORIA, "Chave aleatoria"),
        (TIPO_CHAVE_PIX_OUTRO, "Outro"),
    ]

    STATUS_CREDITO_LIBERADO = "liberado"
    STATUS_CREDITO_ATENCAO = "atencao"
    STATUS_CREDITO_BLOQUEADO = "bloqueado"

    STATUS_CREDITO_CHOICES = [
        (STATUS_CREDITO_LIBERADO, "Liberado"),
        (STATUS_CREDITO_ATENCAO, "Atencao"),
        (STATUS_CREDITO_BLOQUEADO, "Bloqueado"),
    ]

    nome = models.CharField(max_length=140)
    apelido_nome_conhecido = models.CharField(max_length=120, blank=True, null=True)
    cpf_cnpj = models.CharField(max_length=20, blank=True, null=True)
    whatsapp = models.CharField(max_length=30, blank=True, null=True)
    whatsapp_normalizado = models.CharField(max_length=20, blank=True, null=True)
    telefone_alternativo = models.CharField(max_length=30, blank=True, null=True)
    email = models.EmailField(blank=True, null=True)

    cep = models.CharField(max_length=12, blank=True, null=True)
    logradouro = models.CharField(max_length=140, blank=True, null=True)
    numero = models.CharField(max_length=20, blank=True, null=True)
    complemento = models.CharField(max_length=80, blank=True, null=True)
    bairro = models.CharField(max_length=80, blank=True, null=True)
    cidade = models.CharField(max_length=80, blank=True, null=True)
    uf = models.CharField(max_length=2, blank=True, null=True)
    referencia = models.CharField(max_length=180, blank=True, null=True)

    tipo_chave_pix = models.CharField(
        max_length=20,
        choices=TIPO_CHAVE_PIX_CHOICES,
        blank=True,
        null=True,
    )
    chave_pix = models.CharField(max_length=140, blank=True, null=True)

    vende_a_prazo = models.BooleanField(default=False)
    prazo_padrao_dias = models.PositiveIntegerField(default=0)
    limite_credito = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        blank=True,
        null=True,
    )
    limite_aberto = models.BooleanField(default=False)
    status_credito = models.CharField(
        max_length=20,
        choices=STATUS_CREDITO_CHOICES,
        default=STATUS_CREDITO_LIBERADO,
    )
    observacao_financeira = models.TextField(blank=True, null=True)

    permite_contato_whatsapp = models.BooleanField(default=True)
    nome_contato_whatsapp = models.CharField(max_length=120, blank=True, null=True)
    observacao_contato = models.TextField(blank=True, null=True)

    ativo = models.BooleanField(default=True)
    observacoes = models.TextField(blank=True, null=True)
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-ativo", "nome"]

    def __str__(self):
        return self.nome

    @staticmethod
    def normalizar_whatsapp(valor):
        return "".join(caractere for caractere in str(valor or "") if caractere.isdigit())

    def clean(self):
        if self.prazo_padrao_dias is not None and self.prazo_padrao_dias < 0:
            raise ValidationError("O prazo padrao nao pode ser negativo.")

        if self.limite_credito is not None and self.limite_credito < 0:
            raise ValidationError("O limite de credito nao pode ser negativo.")

    def save(self, *args, **kwargs):
        if self.nome:
            self.nome = " ".join(self.nome.strip().split()).title()

        for campo in [
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
            "chave_pix",
            "nome_contato_whatsapp",
        ]:
            valor = getattr(self, campo, None)
            if isinstance(valor, str):
                valor_limpo = " ".join(valor.strip().split())
                setattr(self, campo, valor_limpo or None)

        if self.uf:
            self.uf = self.uf.upper()

        self.whatsapp_normalizado = self.normalizar_whatsapp(self.whatsapp)
        if not self.whatsapp_normalizado:
            self.whatsapp_normalizado = None

        if self.limite_credito is None:
            self.limite_credito = 0

        self.full_clean()
        super().save(*args, **kwargs)


class Funcionario(models.Model):
    nome = models.CharField(max_length=140)
    telefone_whatsapp = models.CharField(max_length=30, blank=True, null=True)
    telefone_whatsapp_normalizado = models.CharField(max_length=20, blank=True, null=True)
    ativo = models.BooleanField(default=True)
    pode_receber_checklist = models.BooleanField(default=False)
    pode_operar_sistema = models.BooleanField(default=False)
    pode_operar_caixa = models.BooleanField(default=False)
    observacoes = models.TextField(blank=True, null=True)
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-ativo", "-pode_operar_sistema", "-pode_receber_checklist", "nome"]
        verbose_name = "Funcionario"
        verbose_name_plural = "Funcionarios"

    def __str__(self):
        return self.nome

    @staticmethod
    def normalizar_whatsapp(valor):
        return "".join(caractere for caractere in str(valor or "") if caractere.isdigit())

    @classmethod
    def habilitados_para_checklist(cls):
        return cls.objects.filter(ativo=True, pode_receber_checklist=True).order_by("nome")

    @classmethod
    def operadores_do_sistema(cls):
        return cls.objects.filter(ativo=True, pode_operar_sistema=True).order_by("nome")

    @classmethod
    def operadores_do_caixa(cls):
        return cls.objects.filter(ativo=True, pode_operar_caixa=True).order_by("nome")

    def clean(self):
        telefone_normalizado = self.normalizar_whatsapp(self.telefone_whatsapp)
        if self.pode_receber_checklist and not telefone_normalizado:
            raise ValidationError("Informe o WhatsApp para funcionarios que podem receber checklist.")

    def save(self, *args, **kwargs):
        if self.nome:
            self.nome = " ".join(self.nome.strip().split()).title()

        if isinstance(self.telefone_whatsapp, str):
            telefone = " ".join(self.telefone_whatsapp.strip().split())
            self.telefone_whatsapp = telefone or None

        if isinstance(self.observacoes, str):
            observacoes = self.observacoes.strip()
            self.observacoes = observacoes or None

        self.telefone_whatsapp_normalizado = self.normalizar_whatsapp(self.telefone_whatsapp)
        if not self.telefone_whatsapp_normalizado:
            self.telefone_whatsapp_normalizado = None

        if not self.ativo:
            self.pode_receber_checklist = False
            self.pode_operar_sistema = False
            self.pode_operar_caixa = False

        self.full_clean()
        super().save(*args, **kwargs)


class Venda(models.Model):
    WHATSAPP_NAO_ENVIADO = "nao_enviado"
    WHATSAPP_ABERTO = "aberto"
    WHATSAPP_ENVIADO_CONFIRMADO = "enviado_confirmado"
    WHATSAPP_STATUS_CHOICES = [
        (WHATSAPP_NAO_ENVIADO, "WhatsApp ainda nao enviado"),
        (WHATSAPP_ABERTO, "WhatsApp aberto - aguardando confirmacao"),
        (WHATSAPP_ENVIADO_CONFIRMADO, "Nota enviada por WhatsApp"),
    ]

    cliente = models.ForeignKey(
        Cliente,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="vendas",
    )
    data_venda = models.DateField()
    data_vencimento = models.DateField(blank=True, null=True)
    tipo_pagamento = models.CharField(max_length=40, blank=True)
    operador = models.CharField(max_length=120, blank=True)
    total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    whatsapp_status = models.CharField(
        max_length=30,
        choices=WHATSAPP_STATUS_CHOICES,
        default=WHATSAPP_NAO_ENVIADO,
    )
    whatsapp_numero_usado = models.CharField(max_length=20, blank=True, null=True)
    whatsapp_aberto_em = models.DateTimeField(blank=True, null=True)
    whatsapp_confirmado_em = models.DateTimeField(blank=True, null=True)
    cancelada = models.BooleanField(default=False)
    cancelada_em = models.DateTimeField(blank=True, null=True)
    motivo_cancelamento = models.TextField(blank=True)
    estoque_devolvido_cancelamento = models.BooleanField(default=False)
    estoque_devolvido_cancelamento_em = models.DateTimeField(blank=True, null=True)
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-id"]

    def __str__(self):
        return f"Venda #{self.id}"

    @property
    def whatsapp_status_texto(self):
        return dict(self.WHATSAPP_STATUS_CHOICES).get(
            self.whatsapp_status,
            "WhatsApp ainda nao enviado",
        )


class EventoVenda(models.Model):
    ORIGEM_NUMERO_CADASTRO = "cadastro"
    ORIGEM_NUMERO_AVULSO = "avulso"
    ORIGEM_NUMERO_DESCONHECIDO = "desconhecido"
    ORIGEM_NUMERO_CHOICES = [
        (ORIGEM_NUMERO_CADASTRO, "Cadastro"),
        (ORIGEM_NUMERO_AVULSO, "Avulso/manual"),
        (ORIGEM_NUMERO_DESCONHECIDO, "Desconhecido"),
    ]

    venda = models.ForeignKey(
        Venda,
        on_delete=models.CASCADE,
        related_name="eventos",
    )
    tipo_evento = models.CharField(max_length=60)
    descricao = models.TextField(blank=True)
    canal = models.CharField(max_length=40, blank=True)
    usuario = models.CharField(max_length=120, blank=True, null=True)
    numero_whatsapp = models.CharField(max_length=20, blank=True, null=True)
    origem_numero = models.CharField(
        max_length=20,
        choices=ORIGEM_NUMERO_CHOICES,
        blank=True,
        null=True,
    )
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-criado_em", "id"]

    def __str__(self):
        return f"{self.tipo_evento} - Venda #{self.venda_id}"


class ItemVenda(models.Model):
    venda = models.ForeignKey(
        Venda,
        on_delete=models.CASCADE,
        related_name="itens",
    )
    produto = models.ForeignKey(
        Produto,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="itens_venda",
    )
    quantidade = models.DecimalField(max_digits=12, decimal_places=3)
    unidade = models.CharField(max_length=20, blank=True)
    preco_unitario = models.DecimalField(max_digits=12, decimal_places=2)
    valor_total = models.DecimalField(max_digits=12, decimal_places=2)
    estoque_antes = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        null=True,
        blank=True,
    )
    estoque_movimentado = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        null=True,
        blank=True,
    )
    estoque_depois = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        null=True,
        blank=True,
    )
    estoque_unidade_snapshot = models.CharField(max_length=20, blank=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        nome_produto = self.produto.nome if self.produto else "Produto nao identificado"
        return f"{nome_produto} - Venda #{self.venda_id}"


class ItemVendaRemovido(models.Model):
    STATUS_REMOVIDO = "removido"
    STATUS_REVERTIDO = "revertido"
    STATUS_CHOICES = [
        (STATUS_REMOVIDO, "Removido"),
        (STATUS_REVERTIDO, "Revertido"),
    ]

    venda = models.ForeignKey(
        Venda,
        on_delete=models.CASCADE,
        related_name="itens_removidos",
    )
    produto = models.ForeignKey(
        Produto,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="itens_venda_removidos",
    )
    produto_nome_snapshot = models.CharField(max_length=120)
    quantidade_snapshot = models.DecimalField(max_digits=12, decimal_places=3)
    unidade_snapshot = models.CharField(max_length=20, blank=True)
    preco_unitario_snapshot = models.DecimalField(max_digits=12, decimal_places=2)
    valor_total_snapshot = models.DecimalField(max_digits=12, decimal_places=2)
    estoque_antes = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        null=True,
        blank=True,
    )
    estoque_movimentado = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        null=True,
        blank=True,
    )
    estoque_depois = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        null=True,
        blank=True,
    )
    estoque_unidade_snapshot = models.CharField(max_length=20, blank=True)
    item_venda_original_id = models.PositiveIntegerField(blank=True, null=True)
    credito_gerado = models.ForeignKey(
        "CreditoCliente",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="itens_removidos_origem",
    )
    ajuste_origem = models.ForeignKey(
        "AjusteItemVendaQuitada",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="itens_removidos_origem",
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_REMOVIDO)
    estoque_devolvido = models.BooleanField(default=False)
    estoque_devolvido_em = models.DateTimeField(blank=True, null=True)
    operador = models.CharField(max_length=120, blank=True)
    observacao = models.TextField(blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)
    revertido_em = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ["-criado_em", "-id"]

    def __str__(self):
        return f"Item removido - Venda #{self.venda_id} - {self.produto_nome_snapshot}"


class ContaReceber(models.Model):
    numero_legado = models.CharField(
        max_length=40,
        blank=True,
        null=True,
        unique=True,
        db_index=True,
    )

    STATUS_ABERTA = "aberta"
    STATUS_PARCIAL = "parcial"
    STATUS_PAGA = "paga"
    STATUS_CANCELADA = "cancelada"
    STATUS_CHOICES = [
        (STATUS_ABERTA, "Aberta"),
        (STATUS_PARCIAL, "Parcial"),
        (STATUS_PAGA, "Paga"),
        (STATUS_CANCELADA, "Cancelada"),
    ]

    venda = models.OneToOneField(
        Venda,
        on_delete=models.CASCADE,
        related_name="conta_receber",
        blank=True,
        null=True,
    )
    cliente = models.ForeignKey(
        Cliente,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="contas_receber",
    )
    data_emissao = models.DateField()
    data_vencimento = models.DateField(blank=True, null=True)
    valor_original = models.DecimalField(max_digits=12, decimal_places=2)
    valor_em_aberto = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_ABERTA)
    observacao = models.TextField(blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["data_vencimento", "id"]

    def __str__(self):
        if self.venda_id:
            return f"Conta a receber - Venda #{self.venda_id}"

        referencia = self.numero_legado or self.id
        return f"Conta a receber legada #{referencia}"


class OperacaoRecebimentoCliente(models.Model):
    STATUS_RECIBO_PENDENTE = "pendente"
    STATUS_RECIBO_ENVIADO = "enviado"
    STATUS_RECIBO_DISPENSADO = "dispensado"
    STATUS_RECIBO_CHOICES = [
        (STATUS_RECIBO_PENDENTE, "Pendente"),
        (STATUS_RECIBO_ENVIADO, "Enviado"),
        (STATUS_RECIBO_DISPENSADO, "Dispensado"),
    ]

    cliente = models.ForeignKey(
        Cliente,
        on_delete=models.SET_NULL,
        null=True,
        related_name="operacoes_recebimento",
    )
    cliente_nome_snapshot = models.CharField(max_length=160)
    valor_recebido = models.DecimalField(max_digits=12, decimal_places=2)
    valor_aplicado = models.DecimalField(max_digits=12, decimal_places=2)
    credito_gerado = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    saldo_anterior = models.DecimalField(max_digits=12, decimal_places=2)
    saldo_atual = models.DecimalField(max_digits=12, decimal_places=2)
    data_recebimento = models.DateField()
    forma_pagamento = models.CharField(max_length=80)
    rota_snapshot = models.CharField(max_length=160, blank=True)
    status_recibo = models.CharField(
        max_length=20,
        choices=STATUS_RECIBO_CHOICES,
        default=STATUS_RECIBO_PENDENTE,
    )
    comprovante_dados = models.JSONField(default=dict, blank=True)
    criado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="operacoes_recebimento_criadas",
    )
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)
    recibo_confirmado_em = models.DateTimeField(null=True, blank=True)
    recibo_confirmado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="operacoes_recebimento_confirmadas",
    )
    recibo_dispensado_em = models.DateTimeField(null=True, blank=True)
    recibo_dispensado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="operacoes_recebimento_dispensadas",
    )
    motivo_dispensa = models.TextField(blank=True)

    class Meta:
        ordering = ["-criado_em", "-id"]
        verbose_name = "Operacao de recebimento de cliente"
        verbose_name_plural = "Operacoes de recebimento de clientes"

    def __str__(self):
        return f"Recebimento cliente R$ {self.valor_recebido} - {self.cliente_nome_snapshot}"


class RecebimentoContaReceber(models.Model):
    conta = models.ForeignKey(
        ContaReceber,
        on_delete=models.CASCADE,
        related_name="recebimentos",
    )
    operacao = models.ForeignKey(
        OperacaoRecebimentoCliente,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="recebimentos",
    )
    data_recebimento = models.DateField()
    valor = models.DecimalField(max_digits=12, decimal_places=2)
    forma_pagamento = models.CharField(max_length=80, blank=True)
    observacao = models.TextField(blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-data_recebimento", "-id"]

    def __str__(self):
        return f"Recebimento R$ {self.valor} - Conta #{self.conta_id}"


class RegistroCobrancaCliente(models.Model):
    TIPO_MANUAL = "manual"
    TIPO_WHATSAPP = "whatsapp"
    TIPO_TELEFONE = "telefone"
    TIPO_VISITA = "visita"
    TIPO_OUTRO = "outro"
    TIPO_CHOICES = [
        (TIPO_MANUAL, "Manual"),
        (TIPO_WHATSAPP, "WhatsApp"),
        (TIPO_TELEFONE, "Telefone"),
        (TIPO_VISITA, "Visita"),
        (TIPO_OUTRO, "Outro"),
    ]

    STATUS_PENDENTE = "pendente"
    STATUS_CONTATADO = "contatado"
    STATUS_PROMESSA_PAGAMENTO = "promessa_pagamento"
    STATUS_SEM_RESPOSTA = "sem_resposta"
    STATUS_RESOLVIDO = "resolvido"
    STATUS_CHOICES = [
        (STATUS_PENDENTE, "Pendente"),
        (STATUS_CONTATADO, "Contatado"),
        (STATUS_PROMESSA_PAGAMENTO, "Promessa de pagamento"),
        (STATUS_SEM_RESPOSTA, "Sem resposta"),
        (STATUS_RESOLVIDO, "Resolvido"),
    ]

    cliente = models.ForeignKey(
        Cliente,
        on_delete=models.CASCADE,
        related_name="registros_cobranca",
    )
    conta = models.ForeignKey(
        ContaReceber,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="registros_cobranca",
    )
    tipo = models.CharField(max_length=20, choices=TIPO_CHOICES, default=TIPO_MANUAL)
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default=STATUS_PENDENTE)
    observacao = models.TextField(blank=True)
    criado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="registros_cobranca_cliente",
    )
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-criado_em", "-id"]
        verbose_name = "Registro de cobranca de cliente"
        verbose_name_plural = "Registros de cobranca de clientes"

    def __str__(self):
        return f"Cobranca {self.get_status_display()} - {self.cliente}"


class FechamentoRotaRecebimento(models.Model):
    METODO_CEDULAS = "cedulas"
    METODO_DIRETA = "direta"
    METODO_CHOICES = [
        (METODO_CEDULAS, "Cédulas"),
        (METODO_DIRETA, "Direta"),
    ]

    STATUS_ABERTO = "aberto"
    STATUS_CONFERIDO = "conferido"
    STATUS_FINALIZADO = "finalizado"
    STATUS_CHOICES = [
        (STATUS_ABERTO, "Aberto"),
        (STATUS_CONFERIDO, "Conferido"),
        (STATUS_FINALIZADO, "Finalizado"),
    ]

    rota = models.CharField(max_length=160)
    data_referencia = models.DateField()
    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="fechamentos_rota_recebimento",
    )
    metodo_conferencia = models.CharField(max_length=20, choices=METODO_CHOICES, default=METODO_DIRETA)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_ABERTO)
    total_sistema = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    total_conferido = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    diferenca = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    observacao = models.TextField(blank=True)
    composicao_cedulas = models.JSONField(default=dict, blank=True)
    criado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="fechamentos_rota_recebimento_criados",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-data_referencia", "rota", "-id"]
        verbose_name = "Fechamento de recebimentos da rota"
        verbose_name_plural = "Fechamentos de recebimentos da rota"
        constraints = [
            models.UniqueConstraint(
                fields=["rota", "data_referencia"],
                name="uniq_fechamento_rota_recebimento_data",
            ),
        ]

    def __str__(self):
        return f"Fechamento {self.rota} - {self.data_referencia:%d/%m/%Y}"


class PixRecebido(models.Model):
    STATUS_PENDENTE = "pendente"
    STATUS_IDENTIFICADO = "identificado"
    STATUS_BAIXADO = "baixado"
    STATUS_IGNORADO = "ignorado"
    STATUS_POSSIVEL_DUPLICADO = "possivel_duplicado"
    STATUS_NAO_IDENTIFICADO = "nao_identificado"
    STATUS_DUPLICADO = "duplicado"
    STATUS_CHOICES = [
        (STATUS_PENDENTE, "Pendente"),
        (STATUS_IDENTIFICADO, "Identificado"),
        (STATUS_BAIXADO, "Baixado"),
        (STATUS_IGNORADO, "Ignorado"),
        (STATUS_POSSIVEL_DUPLICADO, "Possivel duplicado"),
        (STATUS_NAO_IDENTIFICADO, "Nao identificado"),
        (STATUS_DUPLICADO, "Duplicado/inativo"),
    ]

    cliente = models.ForeignKey(
        Cliente,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="pix_recebidos",
    )
    cliente_sugerido = models.ForeignKey(
        Cliente,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="pix_sugeridos",
    )
    pix_original = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="possiveis_duplicados",
    )
    nome_pagador = models.CharField(max_length=160, blank=True)
    enviado_por_nome = models.CharField(max_length=80, blank=True)
    valor = models.DecimalField(max_digits=12, decimal_places=2)
    data_pagamento = models.DateTimeField(default=timezone.now)
    instituicao_pix = models.CharField(max_length=80, blank=True)
    observacao = models.TextField(blank=True)
    texto_ocr_bruto = models.TextField(blank=True)
    comprovante = models.FileField(upload_to="pix/comprovantes/", blank=True, null=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDENTE)
    visualizado_em = models.DateTimeField(blank=True, null=True)
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-data_pagamento", "-id"]
        verbose_name = "Pix recebido"
        verbose_name_plural = "Pix recebidos"

    def __str__(self):
        pagador = self.nome_pagador or (self.cliente.nome if self.cliente else "Pagador nao informado")
        return f"Pix R$ {self.valor} - {pagador}"

    @property
    def comprovante_eh_imagem(self):
        nome = (self.comprovante.name or "").lower()
        return nome.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"))


class CreditoCliente(models.Model):
    TIPO_CREDITO_GERADO = "credito_gerado"
    TIPO_CHOICES = [
        (TIPO_CREDITO_GERADO, "Credito gerado"),
    ]

    cliente = models.ForeignKey(
        Cliente,
        on_delete=models.CASCADE,
        related_name="creditos",
    )
    valor = models.DecimalField(max_digits=12, decimal_places=2)
    tipo = models.CharField(max_length=30, choices=TIPO_CHOICES, default=TIPO_CREDITO_GERADO)
    origem_conta_receber = models.ForeignKey(
        ContaReceber,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="creditos_gerados",
    )
    origem_recebimento = models.ForeignKey(
        RecebimentoContaReceber,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="creditos_gerados",
    )
    observacao = models.TextField(blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-criado_em", "-id"]

    def __str__(self):
        return f"Credito R$ {self.valor} - {self.cliente}"


class AjusteItemVendaQuitada(models.Model):
    MOTIVO_ITEM_NAO_ENTREGUE = "item_nao_entregue"
    MOTIVO_CLIENTE_RECUSOU = "cliente_recusou"
    MOTIVO_PRODUTO_FALTOU = "produto_faltou"
    MOTIVO_SUBSTITUICAO = "substituicao"
    MOTIVO_OUTRO = "outro"
    MOTIVO_CHOICES = [
        (MOTIVO_ITEM_NAO_ENTREGUE, "Item nao entregue"),
        (MOTIVO_CLIENTE_RECUSOU, "Cliente recusou"),
        (MOTIVO_PRODUTO_FALTOU, "Produto faltou"),
        (MOTIVO_SUBSTITUICAO, "Substituicao"),
        (MOTIVO_OUTRO, "Outro"),
    ]

    RESOLUCAO_NAO_DEFINIDA = "nao_definida"
    RESOLUCAO_CREDITO_CLIENTE = "credito_cliente"
    RESOLUCAO_CHOICES = [
        (RESOLUCAO_NAO_DEFINIDA, "Nao definida"),
        (RESOLUCAO_CREDITO_CLIENTE, "Credito do cliente"),
    ]

    STATUS_RASCUNHO = "rascunho"
    STATUS_PENDENTE = "pendente"
    STATUS_RESOLVIDO = "resolvido"
    STATUS_CANCELADO = "cancelado"
    STATUS_CHOICES = [
        (STATUS_RASCUNHO, "Rascunho"),
        (STATUS_PENDENTE, "Pendente"),
        (STATUS_RESOLVIDO, "Resolvido"),
        (STATUS_CANCELADO, "Cancelado"),
    ]

    venda = models.ForeignKey(
        Venda,
        on_delete=models.CASCADE,
        related_name="ajustes_itens_quitados",
    )
    item_venda = models.ForeignKey(
        ItemVenda,
        on_delete=models.PROTECT,
        related_name="ajustes_quitados",
    )
    cliente = models.ForeignKey(
        Cliente,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="ajustes_itens_quitados",
    )
    produto = models.ForeignKey(
        Produto,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="ajustes_itens_quitados",
    )
    produto_nome_snapshot = models.CharField(max_length=120)
    quantidade_snapshot = models.DecimalField(max_digits=12, decimal_places=3)
    unidade_snapshot = models.CharField(max_length=20, blank=True)
    preco_unitario_snapshot = models.DecimalField(max_digits=12, decimal_places=2)
    valor_total_snapshot = models.DecimalField(max_digits=12, decimal_places=2)
    motivo = models.CharField(max_length=30, choices=MOTIVO_CHOICES)
    observacao = models.TextField(blank=True)
    diferenca_financeira = models.DecimalField(max_digits=12, decimal_places=2)
    resolucao_financeira = models.CharField(
        max_length=30,
        choices=RESOLUCAO_CHOICES,
        default=RESOLUCAO_NAO_DEFINIDA,
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDENTE)
    operador = models.CharField(max_length=120, blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)


class Pedido(models.Model):
    STATUS_ABERTO = "aberto"
    STATUS_CANCELADO = "cancelado"
    STATUS_PARCIAL = "parcial"
    STATUS_CONVERTIDO_EM_VENDA = "convertido_em_venda"
    STATUS_CHOICES = [
        (STATUS_ABERTO, "Aberto"),
        (STATUS_CANCELADO, "Cancelado"),
        (STATUS_PARCIAL, "Parcial"),
        (STATUS_CONVERTIDO_EM_VENDA, "Convertido em venda"),
    ]

    cliente = models.ForeignKey(
        Cliente,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="pedidos",
    )
    data_pedido = models.DateField()
    data_prevista_entrega = models.DateField(blank=True, null=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_ABERTO)
    operador = models.CharField(max_length=120, blank=True)
    observacao = models.TextField(blank=True, null=True)
    total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-id"]

    def __str__(self):
        cliente_nome = self.cliente.nome if self.cliente else "Cliente nao informado"
        return f"Pedido #{self.id} - {cliente_nome}"


class ItemPedido(models.Model):
    pedido = models.ForeignKey(
        Pedido,
        on_delete=models.CASCADE,
        related_name="itens",
    )
    produto = models.ForeignKey(
        Produto,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="itens_pedido",
    )
    quantidade = models.DecimalField(max_digits=12, decimal_places=3)
    unidade = models.CharField(max_length=20, blank=True)
    preco_unitario = models.DecimalField(max_digits=12, decimal_places=2)
    valor_total = models.DecimalField(max_digits=12, decimal_places=2)
    estoque_no_momento = models.IntegerField(blank=True, null=True)
    observacao = models.TextField(blank=True, null=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        nome_produto = self.produto.nome if self.produto else "Produto nao identificado"
        return f"{nome_produto} - Pedido #{self.pedido_id}"


class Fornecedor(models.Model):
    codigo_legado = models.CharField(
        max_length=20,
        blank=True,
        null=True,
        unique=True,
        db_index=True,
    )

    FORMA_AVISTA = "avista"
    FORMA_PIX = "pix"
    FORMA_DINHEIRO = "dinheiro"
    FORMA_DEBITO = "debito"
    FORMA_APRAZO = "aprazo"
    FORMA_CARTAO = "cartao"
    FORMA_PAGAMENTO_CHOICES = [
        (FORMA_AVISTA, "À vista"),
        (FORMA_PIX, "Pix"),
        (FORMA_DINHEIRO, "Dinheiro"),
        (FORMA_DEBITO, "Cartão de débito"),
        (FORMA_CARTAO, "Cartão de crédito"),
        (FORMA_APRAZO, "Boleto / a prazo"),
    ]
    DIA_SEMANA_SEGUNDA = 0
    DIA_SEMANA_TERCA = 1
    DIA_SEMANA_QUARTA = 2
    DIA_SEMANA_QUINTA = 3
    DIA_SEMANA_SEXTA = 4
    DIA_SEMANA_SABADO = 5
    DIA_SEMANA_DOMINGO = 6
    DIA_SEMANA_CHOICES = [
        (DIA_SEMANA_SEGUNDA, "Segunda-feira"),
        (DIA_SEMANA_TERCA, "Terca-feira"),
        (DIA_SEMANA_QUARTA, "Quarta-feira"),
        (DIA_SEMANA_QUINTA, "Quinta-feira"),
        (DIA_SEMANA_SEXTA, "Sexta-feira"),
        (DIA_SEMANA_SABADO, "Sabado"),
        (DIA_SEMANA_DOMINGO, "Domingo"),
    ]

    nome = models.CharField(max_length=140)
    nome_fantasia = models.CharField(max_length=140, blank=True, null=True)
    telefone_whatsapp = models.CharField(max_length=30, blank=True, null=True)
    cidade = models.CharField(max_length=80, blank=True, null=True)
    bairro = models.CharField(max_length=80, blank=True, null=True)
    aceita_pix = models.BooleanField(default=True)
    aceita_dinheiro = models.BooleanField(default=True)
    aceita_cartao_debito = models.BooleanField(default=True)
    aceita_cartao_credito = models.BooleanField(default=True)
    aceita_boleto = models.BooleanField(default=False)
    forma_pagamento_padrao = models.CharField(max_length=20, choices=FORMA_PAGAMENTO_CHOICES, default=FORMA_AVISTA)
    prazos_pagamento_padrao = models.CharField(max_length=120, blank=True, null=True, help_text="Exemplo: 7, 14, 21")
    dia_vencimento_cartao = models.PositiveSmallIntegerField(blank=True, null=True)
    frequencia_visita_ativa = models.BooleanField(default=False)
    frequencia_visita_intervalo_dias = models.PositiveSmallIntegerField(blank=True, null=True)
    frequencia_visita_dia_semana = models.PositiveSmallIntegerField(
        choices=DIA_SEMANA_CHOICES,
        blank=True,
        null=True,
    )
    frequencia_visita_data_referencia = models.DateField(blank=True, null=True)
    observacao = models.TextField(blank=True, null=True)
    ativo = models.BooleanField(default=True)
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["nome", "id"]

    def __str__(self):
        return self.nome

    def clean(self):
        super().clean()
        if not self.frequencia_visita_ativa:
            return

        erros = {}
        intervalo = self.frequencia_visita_intervalo_dias
        dia_semana = self.frequencia_visita_dia_semana
        data_referencia = self.frequencia_visita_data_referencia

        if intervalo is None:
            erros["frequencia_visita_intervalo_dias"] = "Informe o intervalo da frequencia de visita."
        elif intervalo <= 0:
            erros["frequencia_visita_intervalo_dias"] = "O intervalo da frequencia de visita deve ser maior que zero."
        elif intervalo % 7 != 0:
            erros["frequencia_visita_intervalo_dias"] = "O intervalo da frequencia de visita deve ser multiplo de 7."

        if dia_semana is None:
            erros["frequencia_visita_dia_semana"] = "Informe o dia habitual da visita."

        if data_referencia is None:
            erros["frequencia_visita_data_referencia"] = "Informe a data de referencia da visita."
        elif dia_semana is not None and data_referencia.weekday() != dia_semana:
            erros["frequencia_visita_data_referencia"] = "A data de referencia deve cair no dia da semana selecionado."

        if erros:
            raise ValidationError(erros)


class FornecedorContato(models.Model):
    fornecedor = models.ForeignKey(
        Fornecedor,
        on_delete=models.CASCADE,
        related_name="contatos",
    )
    nome = models.CharField(max_length=140)
    cargo = models.CharField(max_length=80, blank=True, null=True)
    telefone_whatsapp = models.CharField(max_length=30, blank=True, null=True)
    telefone_whatsapp_normalizado = models.CharField(max_length=20, blank=True, null=True)
    principal = models.BooleanField(default=False)
    ativo = models.BooleanField(default=True)
    observacao = models.TextField(blank=True, null=True)
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-principal", "-ativo", "nome", "id"]

    def __str__(self):
        return f"{self.nome} - {self.fornecedor}"

    @staticmethod
    def normalizar_whatsapp(valor):
        return "".join(caractere for caractere in str(valor or "") if caractere.isdigit())

    @property
    def whatsapp_url(self):
        numero = self.telefone_whatsapp_normalizado or self.normalizar_whatsapp(self.telefone_whatsapp)
        return f"https://web.whatsapp.com/send?phone={numero}" if numero else ""

    def save(self, *args, **kwargs):
        for campo in ["nome", "cargo", "telefone_whatsapp"]:
            valor = getattr(self, campo, None)
            if isinstance(valor, str):
                valor_limpo = " ".join(valor.strip().split())
                setattr(self, campo, valor_limpo or None)

        if isinstance(self.observacao, str):
            observacao = self.observacao.strip()
            self.observacao = observacao or None

        self.telefone_whatsapp_normalizado = self.normalizar_whatsapp(self.telefone_whatsapp)
        if not self.telefone_whatsapp_normalizado:
            self.telefone_whatsapp_normalizado = None

        self.full_clean()
        super().save(*args, **kwargs)

        if self.principal:
            FornecedorContato.objects.filter(
                fornecedor=self.fornecedor,
                principal=True,
            ).exclude(pk=self.pk).update(principal=False)


class FornecedorContatoTelefone(models.Model):
    TIPO_CELULAR = "celular"
    TIPO_FIXO = "fixo"
    TIPO_OUTRO = "outro"
    TIPO_CHOICES = [
        (TIPO_CELULAR, "Celular"),
        (TIPO_FIXO, "Fixo"),
        (TIPO_OUTRO, "Outro"),
    ]

    contato = models.ForeignKey(
        FornecedorContato,
        on_delete=models.CASCADE,
        related_name="telefones",
    )
    numero = models.CharField(max_length=20, blank=True)
    tipo = models.CharField(max_length=20, choices=TIPO_CHOICES, default=TIPO_CELULAR)
    whatsapp = models.BooleanField(default=False)
    principal = models.BooleanField(default=False)
    ativo = models.BooleanField(default=True)
    ordem = models.PositiveSmallIntegerField(default=1)
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-principal", "ordem", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["contato", "numero"],
                condition=Q(ativo=True),
                name="uniq_fornecedor_contato_telefone_ativo",
            ),
            models.UniqueConstraint(
                fields=["contato"],
                condition=Q(ativo=True, principal=True),
                name="uniq_fornecedor_contato_telefone_principal",
            ),
        ]

    def __str__(self):
        return f"{self.numero} - {self.contato}"

    @staticmethod
    def normalizar_numero(valor):
        return "".join(caractere for caractere in str(valor or "") if caractere.isdigit())

    def clean(self):
        super().clean()
        erros = {}
        numero = self.normalizar_numero(self.numero)

        if self.ativo and not numero:
            erros["numero"] = "Informe o numero do telefone."
        if numero and len(numero) not in (10, 11):
            erros["numero"] = "Informe um telefone com 10 ou 11 digitos."
        if self.principal and not self.ativo:
            erros["principal"] = "Um telefone principal precisa estar ativo."

        if self.contato_id and self.ativo:
            telefones_ativos = FornecedorContatoTelefone.objects.filter(
                contato=self.contato,
                ativo=True,
            )
            if self.pk:
                telefones_ativos = telefones_ativos.exclude(pk=self.pk)

            if telefones_ativos.count() >= 3:
                erros["ativo"] = "Cada contato pode ter no maximo 3 telefones ativos."
            if numero and telefones_ativos.filter(numero=numero).exists():
                erros["numero"] = "Este telefone ja esta cadastrado para este contato."
            if self.principal and telefones_ativos.filter(principal=True).exists():
                erros["principal"] = "Este contato ja possui um telefone principal ativo."

        if erros:
            raise ValidationError(erros)

    def save(self, *args, **kwargs):
        self.numero = self.normalizar_numero(self.numero)
        self.full_clean()
        super().save(*args, **kwargs)


class FornecedorDestinatarioLista(models.Model):
    TIPO_PADRAO = "padrao"
    TIPO_TEMPORARIO = "temporario"
    TIPO_CHOICES = [
        (TIPO_PADRAO, "Padr?o"),
        (TIPO_TEMPORARIO, "Tempor?rio"),
    ]

    fornecedor = models.ForeignKey(
        Fornecedor,
        on_delete=models.CASCADE,
        related_name="destinatarios_lista",
    )
    contato = models.ForeignKey(
        FornecedorContato,
        on_delete=models.PROTECT,
        related_name="destinatarios_lista",
    )
    telefone = models.ForeignKey(
        FornecedorContatoTelefone,
        on_delete=models.PROTECT,
        related_name="destinatarios_lista",
    )
    tipo = models.CharField(
        max_length=20,
        choices=TIPO_CHOICES,
        default=TIPO_PADRAO,
    )
    ativo = models.BooleanField(default=True)
    vigencia_inicio = models.DateField(blank=True, null=True)
    vigencia_fim = models.DateField(blank=True, null=True)
    motivo = models.CharField(max_length=255, blank=True, default="")
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-ativo", "tipo", "-criado_em", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["fornecedor"],
                condition=Q(tipo="padrao", ativo=True),
                name="uniq_destinatario_lista_padrao_ativo",
            ),
            models.UniqueConstraint(
                fields=["fornecedor"],
                condition=Q(tipo="temporario", ativo=True),
                name="uniq_destinatario_lista_temporario_ativo",
            ),
        ]

    def clean(self):
        super().clean()
        erros = {}

        if self.contato_id and self.contato.fornecedor_id != self.fornecedor_id:
            erros["contato"] = (
                "O contato escolhido precisa pertencer a este fornecedor."
            )

        if self.telefone_id:
            if self.telefone.contato_id != self.contato_id:
                erros["telefone"] = (
                    "O telefone escolhido precisa pertencer ao contato informado."
                )
            elif not self.telefone.ativo:
                erros["telefone"] = (
                    "O telefone escolhido precisa estar ativo."
                )
            elif not self.telefone.whatsapp:
                erros["telefone"] = (
                    "O telefone escolhido precisa estar marcado como WhatsApp."
                )

        if self.tipo == self.TIPO_TEMPORARIO:
            if self.vigencia_inicio and self.vigencia_fim:
                if self.vigencia_fim < self.vigencia_inicio:
                    erros["vigencia_fim"] = (
                        "A data final n?o pode ser anterior ? data inicial."
                    )
        else:
            self.vigencia_inicio = None
            self.vigencia_fim = None
            self.motivo = ""

        if erros:
            raise ValidationError(erros)

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return (
            f"{self.fornecedor} - {self.contato} - "
            f"{self.telefone.numero} ({self.get_tipo_display()})"
        )


class FornecedorDestinatarioRecente(models.Model):
    fornecedor = models.ForeignKey(
        Fornecedor,
        on_delete=models.CASCADE,
        related_name="destinatarios_recentes_lista",
    )
    nome = models.CharField(max_length=140, blank=True, default="")
    telefone = models.CharField(max_length=20)
    ultima_utilizacao = models.DateTimeField(db_index=True)
    quantidade_utilizacoes = models.PositiveIntegerField(default=1)
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-ultima_utilizacao", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["fornecedor", "telefone"],
                name="uniq_destinatario_recente_fornecedor_telefone",
            ),
        ]

    @staticmethod
    def normalizar_telefone(valor):
        texto = str(valor or "").strip()
        digitos = "".join(caractere for caractere in texto if caractere.isdigit())
        if texto.startswith("+") and not texto.startswith("+55"):
            return digitos
        if len(digitos) in (10, 11):
            return f"55{digitos}"
        return digitos

    @staticmethod
    def normalizar_nome(valor):
        nome = " ".join(str(valor or "").strip().split())
        if nome and nome == nome.lower():
            return " ".join(parte[:1].upper() + parte[1:] for parte in nome.split())
        return nome

    def clean(self):
        super().clean()
        self.telefone = self.normalizar_telefone(self.telefone)
        self.nome = self.normalizar_nome(self.nome)
        if not self.telefone:
            raise ValidationError({"telefone": "Informe o telefone do destinatario recente."})
        if self.quantidade_utilizacoes < 1:
            raise ValidationError({"quantidade_utilizacoes": "A quantidade de utilizacoes deve ser maior que zero."})

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        nome = self.nome or "Sem nome"
        return f"{self.fornecedor} - {nome} - {self.telefone}"


class EnvioListaCompraFornecedor(models.Model):
    ORIGEM_PADRAO = "padrao"
    ORIGEM_RECENTE = "recente"
    ORIGEM_PERSONALIZADO = "personalizado"
    ORIGEM_CHOICES = [
        (ORIGEM_PADRAO, "Destinatario padrao"),
        (ORIGEM_RECENTE, "Destinatario recente"),
        (ORIGEM_PERSONALIZADO, "Destinatario personalizado"),
    ]

    lista = models.ForeignKey(
        "ListaCompraFornecedor",
        on_delete=models.CASCADE,
        related_name="envios_vendedor",
    )
    fornecedor = models.ForeignKey(
        Fornecedor,
        on_delete=models.PROTECT,
        related_name="envios_listas_compra",
    )
    nome_destinatario = models.CharField(max_length=140, blank=True, default="")
    telefone_destinatario = models.CharField(max_length=20)
    confirmado_em = models.DateTimeField(db_index=True)
    confirmado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="envios_listas_compra_confirmados",
    )
    origem_destinatario = models.CharField(
        max_length=20,
        choices=ORIGEM_CHOICES,
        default=ORIGEM_PERSONALIZADO,
    )
    chave_idempotencia = models.CharField(max_length=80, unique=True, db_index=True)
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-confirmado_em", "-id"]
        indexes = [
            models.Index(fields=["lista", "-confirmado_em"], name="idx_envio_lista_conf_em"),
            models.Index(fields=["fornecedor", "-confirmado_em"], name="idx_envio_forn_conf_em"),
        ]

    @staticmethod
    def normalizar_telefone(valor):
        return FornecedorDestinatarioRecente.normalizar_telefone(valor)

    @staticmethod
    def normalizar_nome(valor):
        return FornecedorDestinatarioRecente.normalizar_nome(valor)

    def clean(self):
        super().clean()
        self.telefone_destinatario = self.normalizar_telefone(self.telefone_destinatario)
        self.nome_destinatario = self.normalizar_nome(self.nome_destinatario)
        if not self.telefone_destinatario:
            raise ValidationError({"telefone_destinatario": "Informe o telefone do destinatario."})
        if self.origem_destinatario not in dict(self.ORIGEM_CHOICES):
            raise ValidationError({"origem_destinatario": "Origem do destinatario invalida."})
        if self.lista_id and self.fornecedor_id and self.lista.fornecedor_id != self.fornecedor_id:
            raise ValidationError({"fornecedor": "Fornecedor deve corresponder ao fornecedor da lista."})

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        nome = self.nome_destinatario or "Sem nome"
        return f"Envio da Lista #{self.lista_id} para {nome} - {self.telefone_destinatario}"


class ResolucaoVisitaFornecedor(models.Model):
    TIPO_NAO_OCORREU = "nao_ocorreu"
    TIPO_ADIADA = "adiada"
    TIPO_IGNORAR_CICLO = "ignorar_ciclo"

    TIPO_RESOLUCAO_CHOICES = [
        (TIPO_NAO_OCORREU, "Visita nao ocorreu"),
        (TIPO_ADIADA, "Visita adiada"),
        (TIPO_IGNORAR_CICLO, "Ignorar este ciclo"),
    ]

    fornecedor = models.ForeignKey(
        Fornecedor,
        on_delete=models.PROTECT,
        related_name="resolucoes_visitas",
    )
    data_visita_original = models.DateField(db_index=True)
    tipo_resolucao = models.CharField(
        max_length=20,
        choices=TIPO_RESOLUCAO_CHOICES,
    )
    nova_data_visita = models.DateField(blank=True, null=True)
    observacao = models.TextField(blank=True, default="")
    responsavel = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="resolucoes_visitas_fornecedores",
    )
    resolvido_em = models.DateTimeField(auto_now_add=True, db_index=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-resolvido_em", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["fornecedor", "data_visita_original"],
                name="uniq_resolucao_ciclo_visita_fornecedor",
            ),
        ]
        indexes = [
            models.Index(
                fields=["fornecedor", "data_visita_original"],
                name="idx_resolucao_visita_ciclo",
            ),
            models.Index(
                fields=["tipo_resolucao", "-resolvido_em"],
                name="idx_resolucao_tipo_data",
            ),
        ]

    def clean(self):
        super().clean()
        erros = {}

        if self.tipo_resolucao == self.TIPO_ADIADA:
            if not self.nova_data_visita:
                erros["nova_data_visita"] = (
                    "Informe a nova data quando a visita for adiada."
                )
            elif (
                self.data_visita_original
                and self.nova_data_visita <= self.data_visita_original
            ):
                erros["nova_data_visita"] = (
                    "A nova data deve ser posterior a data original da visita."
                )
        elif self.nova_data_visita:
            erros["nova_data_visita"] = (
                "Nova data somente pode ser informada para visita adiada."
            )

        if erros:
            raise ValidationError(erros)

    def save(self, *args, **kwargs):
        if isinstance(self.observacao, str):
            self.observacao = self.observacao.strip()

        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return (
            f"{self.fornecedor} - {self.data_visita_original:%d/%m/%Y} - "
            f"{self.get_tipo_resolucao_display()}"
        )


class MeioPagamento(models.Model):
    TIPO_DEBITO = "debito"
    TIPO_CREDITO = "credito"
    TIPO_OUTRO = "outro"

    TIPO_CHOICES = [
        (TIPO_CREDITO, "Cartão de crédito"),
        (TIPO_DEBITO, "Cartão de débito"),
        (TIPO_OUTRO, "Outro cartão"),
    ]

    nome = models.CharField(max_length=120)
    tipo = models.CharField(max_length=30, choices=TIPO_CHOICES)
    banco_ou_pessoa = models.CharField(max_length=120, blank=True, null=True)
    dono_titular = models.CharField(max_length=120, blank=True, null=True)
    final_cartao = models.CharField(max_length=4, blank=True, null=True)
    dia_vencimento_cartao = models.PositiveSmallIntegerField(blank=True, null=True)
    principal = models.BooleanField(default=False)
    ativo = models.BooleanField(default=True)
    observacao = models.TextField(blank=True, null=True)
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-ativo", "tipo", "-principal", "nome", "id"]

    def __str__(self):
        return self.nome

    @staticmethod
    def _nome_proprio(valor):
        valor = " ".join(str(valor or "").strip().split())
        if not valor:
            return valor
        return " ".join(parte[:1].upper() + parte[1:].lower() for parte in valor.split())

    def save(self, *args, **kwargs):
        self.nome = self._nome_proprio(self.nome)
        self.banco_ou_pessoa = self._nome_proprio(self.banco_ou_pessoa) or None
        self.dono_titular = self._nome_proprio(self.dono_titular) or None
        self.final_cartao = "".join(caractere for caractere in str(self.final_cartao or "") if caractere.isdigit())[-4:] or None

        if self.tipo != self.TIPO_CREDITO:
            self.dia_vencimento_cartao = None

        super().save(*args, **kwargs)

        if self.principal:
            MeioPagamento.objects.filter(
                tipo=self.tipo,
                principal=True,
            ).exclude(pk=self.pk).update(principal=False)



class ProdutoFornecedor(models.Model):
    produto = models.ForeignKey(
        Produto,
        on_delete=models.CASCADE,
        related_name="fornecedores_vinculados",
    )
    fornecedor = models.ForeignKey(
        Fornecedor,
        on_delete=models.CASCADE,
        related_name="produtos_vinculados",
    )
    ativo = models.BooleanField(default=True)
    codigo_produto_fornecedor = models.CharField(max_length=80, blank=True, null=True)
    ultimo_preco_compra = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True)
    ultima_compra_em = models.DateField(blank=True, null=True)
    observacao = models.TextField(blank=True, null=True)
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["fornecedor__nome", "produto__nome", "id"]
        unique_together = ("produto", "fornecedor")

    def __str__(self):
        return f"{self.produto} - {self.fornecedor}"


class Compra(models.Model):
    STATUS_ABERTA = "aberta"
    STATUS_RASCUNHO = "rascunho"
    STATUS_FINALIZACAO_INICIADA = "finalizacao_iniciada"
    STATUS_CANCELADA = "cancelada"
    STATUS_FINALIZADA = "finalizada"
    STATUS_CHOICES = [
        (STATUS_ABERTA, "Aberta"),
        (STATUS_RASCUNHO, "Rascunho"),
        (STATUS_FINALIZACAO_INICIADA, "Finalizacao iniciada"),
        (STATUS_CANCELADA, "Cancelada"),
        (STATUS_FINALIZADA, "Finalizada"),
    ]

    fornecedor = models.ForeignKey(
        Fornecedor,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="compras",
    )
    data_compra = models.DateField()
    data_vencimento = models.DateField(blank=True, null=True)
    tipo_pagamento = models.CharField(max_length=40, blank=True)
    operador = models.CharField(max_length=120, blank=True)
    total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_ABERTA)
    observacao = models.TextField(blank=True, null=True)
    cancelada = models.BooleanField(default=False)
    cancelada_em = models.DateTimeField(blank=True, null=True)
    motivo_cancelamento = models.TextField(blank=True)
    estoque_entrada_realizada = models.BooleanField(default=False)
    estoque_entrada_realizada_em = models.DateTimeField(blank=True, null=True)
    fechamento_token = models.CharField(max_length=32, unique=True, blank=True, null=True, editable=False)
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-id"]

    def __str__(self):
        fornecedor_nome = self.fornecedor.nome if self.fornecedor else "Fornecedor nao informado"
        return f"Compra #{self.id} - {fornecedor_nome}"

    @property
    def tipo_pagamento_texto(self):
        valor = (self.tipo_pagamento or "").strip()
        rotulos = {
            "avista": "À vista (Dinheiro / Pix)",
            "a vista": "À vista (Dinheiro / Pix)",
            "à vista": "À vista (Dinheiro / Pix)",
            "aprazo": "A prazo",
            "a prazo": "A prazo",
            "cartao_credito": "Cartão crédito",
            "cartão crédito": "Cartão crédito",
            "cartao_debito": "Cartão débito",
            "cartão débito": "Cartão débito",
            # Compatibilidade de leitura para compras gravadas pelo fluxo antigo.
            "pix": "Pix",
            "dinheiro": "Dinheiro",
            "banco": "Banco/Transferência",
            "boleto": "Boleto",
            "cartao": "Cartão",
        }
        return rotulos.get(valor.casefold(), valor or "-")


class ItemCompra(models.Model):
    compra = models.ForeignKey(
        Compra,
        on_delete=models.CASCADE,
        related_name="itens",
    )
    produto = models.ForeignKey(
        Produto,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="itens_compra",
    )
    quantidade = models.DecimalField(max_digits=12, decimal_places=3)
    unidade = models.CharField(max_length=20, blank=True)
    preco_unitario = models.DecimalField(max_digits=12, decimal_places=2)
    valor_total = models.DecimalField(max_digits=12, decimal_places=2)
    observacao = models.TextField(blank=True, null=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        nome_produto = self.produto.nome if self.produto else "Produto nao identificado"
        return f"{nome_produto} - Compra #{self.compra_id}"



class EnvioInternoListaCompraFornecedor(models.Model):
    VERSAO_ANALITICA = "analitica"
    VERSAO_SINTETICA = "sintetica"
    VERSAO_CHOICES = [
        (VERSAO_ANALITICA, "Analitica"),
        (VERSAO_SINTETICA, "Sintetica"),
    ]

    ORIGEM_FUNCIONARIO = "funcionario"
    ORIGEM_AVULSO = "avulso"
    ORIGEM_SEM_NUMERO = "sem_numero"
    ORIGEM_CHOICES = [
        (ORIGEM_FUNCIONARIO, "Funcionario"),
        (ORIGEM_AVULSO, "Numero avulso"),
        (ORIGEM_SEM_NUMERO, "Sem numero"),
    ]

    lista = models.ForeignKey(
        "ListaCompraFornecedor",
        on_delete=models.CASCADE,
        related_name="envios_internos",
    )
    fornecedor = models.ForeignKey(
        Fornecedor,
        on_delete=models.PROTECT,
        related_name="envios_internos_listas_compra",
    )
    funcionario = models.ForeignKey(
        "Funcionario",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="envios_internos_listas_compra",
    )
    versao = models.CharField(
        max_length=20,
        choices=VERSAO_CHOICES,
        default=VERSAO_ANALITICA,
    )
    nome_destinatario = models.CharField(max_length=140, blank=True, default="")
    telefone_destinatario = models.CharField(max_length=20, blank=True, default="")
    origem_destinatario = models.CharField(
        max_length=20,
        choices=ORIGEM_CHOICES,
        default=ORIGEM_SEM_NUMERO,
    )
    registrado_em = models.DateTimeField(db_index=True)
    registrado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="envios_internos_listas_compra_registrados",
    )
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-registrado_em", "-id"]
        indexes = [
            models.Index(fields=["lista", "-registrado_em"], name="idx_envio_int_lista_em"),
            models.Index(fields=["fornecedor", "-registrado_em"], name="idx_envio_int_forn_em"),
        ]

    @staticmethod
    def normalizar_telefone(valor):
        return EnvioListaCompraFornecedor.normalizar_telefone(valor)

    def __str__(self):
        destino = self.nome_destinatario or self.telefone_destinatario or "Destino interno"
        return f"Envio interno Lista #{self.lista_id} - {destino}"


class ListaCompraFornecedor(models.Model):
    STATUS_ABERTA = "aberta"
    STATUS_ENVIADA = "enviada"
    STATUS_FINALIZADA = "finalizada"
    STATUS_CANCELADA = "cancelada"

    FORMA_COBRANCA_NAO_INFORMADA = ""
    FORMA_COBRANCA_AVISTA = "avista"
    FORMA_COBRANCA_BOLETO_UNICO = "boleto_unico"
    FORMA_COBRANCA_VARIOS_BOLETOS = "varios_boletos"
    FORMA_COBRANCA_NOTA_CHOICES = [
        (FORMA_COBRANCA_NAO_INFORMADA, "Não informada"),
        (FORMA_COBRANCA_AVISTA, "À vista"),
        (FORMA_COBRANCA_BOLETO_UNICO, "Boleto único"),
        (FORMA_COBRANCA_VARIOS_BOLETOS, "Vários boletos"),
    ]

    STATUS_CHOICES = [
        (STATUS_ABERTA, "Aberta"),
        (STATUS_ENVIADA, "Enviada"),
        (STATUS_FINALIZADA, "Finalizada"),
        (STATUS_CANCELADA, "Cancelada"),
    ]

    fornecedor = models.ForeignKey(
        Fornecedor,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="listas_compra",
    )
    data_lista = models.DateField()
    data_inicio_periodo = models.DateField()
    data_fim_periodo = models.DateField()
    data_chegada_prevista = models.DateField(blank=True, null=True)
    data_visita_fornecedor = models.DateField(blank=True, null=True, db_index=True)
    total_sugerido_original = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_lista = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    valor_nota_boleto = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True)
    classificacao_diferenca_nota = models.CharField(max_length=40, blank=True, default="")
    observacao_diferenca_nota = models.TextField(blank=True, default="")
    forma_cobranca_nota = models.CharField(
        max_length=30,
        choices=FORMA_COBRANCA_NOTA_CHOICES,
        blank=True,
        default=FORMA_COBRANCA_NAO_INFORMADA,
    )
    observacao_pagamento_nota = models.TextField(blank=True, default="")
    checklist_externa_token_hash = models.CharField(max_length=64, blank=True, default="")
    checklist_externa_token_expira_em = models.DateTimeField(blank=True, null=True)
    checklist_externa_token_usado_em = models.DateTimeField(blank=True, null=True)
    checklist_externa_conferente = models.CharField(max_length=120, blank=True, default="")
    observacao = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_ABERTA)
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-id"]

    def __str__(self):
        fornecedor_nome = self.fornecedor.nome if self.fornecedor else "Fornecedor nao informado"
        return f"Lista de compras #{self.id} - {fornecedor_nome}"


class ParcelaNotaListaCompraFornecedor(models.Model):
    lista = models.ForeignKey(
        ListaCompraFornecedor,
        on_delete=models.CASCADE,
        related_name="parcelas_nota",
    )
    numero = models.PositiveIntegerField(default=1)
    data_vencimento = models.DateField(blank=True, null=True)
    valor = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    observacao = models.CharField(max_length=120, blank=True, default="")
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["numero", "id"]

    def __str__(self):
        return f"Parcela {self.numero} - Lista #{self.lista_id}"


class ItemListaCompraFornecedor(models.Model):
    STATUS_CONFERENCIA_PENDENTE = "pendente"
    STATUS_CONFERENCIA_OK = "ok"
    STATUS_CONFERENCIA_FALTOU = "faltou"
    STATUS_CONFERENCIA_VEIO_A_MAIS = "veio_a_mais"
    STATUS_CONFERENCIA_NAO_VEIO = "nao_veio"
    STATUS_CONFERENCIA_CHOICES = [
        (STATUS_CONFERENCIA_PENDENTE, "Pendente"),
        (STATUS_CONFERENCIA_OK, "OK"),
        (STATUS_CONFERENCIA_FALTOU, "Faltou"),
        (STATUS_CONFERENCIA_VEIO_A_MAIS, "Veio a mais"),
        (STATUS_CONFERENCIA_NAO_VEIO, "Nao veio"),
    ]

    lista = models.ForeignKey(
        ListaCompraFornecedor,
        on_delete=models.CASCADE,
        related_name="itens",
    )
    produto = models.ForeignKey(
        Produto,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="itens_lista_compra_fornecedor",
    )
    estoque_atual = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    estoque_minimo = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    vendido_periodo = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    pedidos_abertos = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    quantidade_sugerida = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    quantidade_final = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    unidade = models.CharField(max_length=20, blank=True)
    preco_compra = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    preco_unitario = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    quantidade_recebida = models.DecimalField(max_digits=12, decimal_places=3, blank=True, null=True)
    status_conferencia = models.CharField(
        max_length=20,
        choices=STATUS_CONFERENCIA_CHOICES,
        default=STATUS_CONFERENCIA_PENDENTE,
    )
    observacao_conferencia = models.TextField(blank=True)
    conferido = models.BooleanField(default=False)
    conferido_em = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        nome_produto = self.produto.nome if self.produto else "Produto nao identificado"
        return f"{nome_produto} - Lista #{self.lista_id}"

    def calcular_status_conferencia(self):
        if self.quantidade_recebida is None:
            return self.STATUS_CONFERENCIA_PENDENTE
        quantidade = self.quantidade_recebida
        solicitada = self.quantidade_final or 0
        if quantidade == 0:
            return self.STATUS_CONFERENCIA_NAO_VEIO
        if quantidade == solicitada:
            return self.STATUS_CONFERENCIA_OK
        if quantidade < solicitada:
            return self.STATUS_CONFERENCIA_FALTOU
        return self.STATUS_CONFERENCIA_VEIO_A_MAIS

    @property
    def diferenca_conferencia(self):
        if self.quantidade_recebida is None:
            return None
        return self.quantidade_recebida - (self.quantidade_final or 0)

    def _atualizar_lista_mae(self, lista_id=None):
        lista_id = lista_id or self.lista_id
        if lista_id:
            ListaCompraFornecedor.objects.filter(pk=lista_id).update(
                atualizado_em=timezone.now()
            )

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        self._atualizar_lista_mae()

    def delete(self, *args, **kwargs):
        lista_id = self.lista_id
        resultado = super().delete(*args, **kwargs)
        self._atualizar_lista_mae(lista_id)
        return resultado


class ContaPagar(models.Model):
    documento_legado = models.CharField(
        max_length=40,
        blank=True,
        null=True,
        unique=True,
        db_index=True,
    )

    STATUS_ABERTA = "aberta"
    STATUS_PARCIAL = "parcial"
    STATUS_PAGA = "paga"
    STATUS_CANCELADA = "cancelada"
    STATUS_CHOICES = [
        (STATUS_ABERTA, "Aberta"),
        (STATUS_PARCIAL, "Parcial"),
        (STATUS_PAGA, "Paga"),
        (STATUS_CANCELADA, "Cancelada"),
    ]

    compra = models.OneToOneField(
        Compra,
        on_delete=models.CASCADE,
        related_name="conta_pagar",
        blank=True,
        null=True,
    )
    fornecedor = models.ForeignKey(
        Fornecedor,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="contas_pagar",
    )
    data_emissao = models.DateField()
    data_vencimento = models.DateField(blank=True, null=True)
    valor_original = models.DecimalField(max_digits=12, decimal_places=2)
    valor_em_aberto = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_ABERTA)
    observacao = models.TextField(blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["data_vencimento", "id"]

    def __str__(self):
        if self.compra_id:
            return f"Conta a pagar - Compra #{self.compra_id}"
        if self.documento_legado:
            return f"Conta a pagar - Documento {self.documento_legado}"
        return f"Conta a pagar #{self.pk or '-'}"


class PagamentoContaPagar(models.Model):
    conta = models.ForeignKey(
        ContaPagar,
        on_delete=models.CASCADE,
        related_name="pagamentos",
    )
    data_pagamento = models.DateField()
    valor = models.DecimalField(max_digits=12, decimal_places=2)
    juros_bancarios = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    forma_pagamento = models.CharField(max_length=80, blank=True)
    observacao = models.TextField(blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-data_pagamento", "-id"]

    def __str__(self):
        return f"Pagamento R$ {self.valor} - Conta #{self.conta_id}"


class ContaFinanceira(models.Model):
    TIPO_CAIXA = "caixa"
    TIPO_BANCO = "banco"
    TIPO_CHOICES = [
        (TIPO_CAIXA, "Caixa"),
        (TIPO_BANCO, "Banco"),
    ]

    nome = models.CharField(max_length=100)
    tipo = models.CharField(max_length=20, choices=TIPO_CHOICES)
    saldo_inicial = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    ativo = models.BooleanField(default=True)
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["tipo", "nome"]

    def __str__(self):
        return self.nome


class MovimentoFinanceiro(models.Model):
    TIPO_ENTRADA = "entrada"
    TIPO_SAIDA = "saida"
    TIPO_TRANSFERENCIA = "transferencia"
    TIPO_AJUSTE = "ajuste"
    TIPO_CHOICES = [
        (TIPO_ENTRADA, "Entrada"),
        (TIPO_SAIDA, "Saida"),
        (TIPO_TRANSFERENCIA, "Transferencia"),
        (TIPO_AJUSTE, "Ajuste"),
    ]

    conta = models.ForeignKey(
        ContaFinanceira,
        on_delete=models.PROTECT,
        related_name="movimentos",
    )
    tipo = models.CharField(max_length=20, choices=TIPO_CHOICES)
    valor = models.DecimalField(max_digits=12, decimal_places=2)
    data = models.DateField()
    descricao = models.CharField(max_length=255, blank=True)
    operador = models.CharField(max_length=120, blank=True)
    conta_destino = models.ForeignKey(
        ContaFinanceira,
        on_delete=models.PROTECT,
        blank=True,
        null=True,
        related_name="transferencias_recebidas",
    )
    origem = models.CharField(max_length=100, blank=True)
    compra = models.ForeignKey(
        Compra,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="movimentos_financeiros",
    )
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-data", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["compra", "conta", "origem"],
                condition=models.Q(compra__isnull=False, origem="compra_a_vista"),
                name="movimento_unico_compra_avista_conta",
            ),
        ]

    def __str__(self):
        return f"{self.get_tipo_display()} R$ {self.valor} - {self.conta}"


class EmprestimoRapido(models.Model):
    STATUS_ABERTO = "aberto"
    STATUS_QUITADO = "quitado"
    STATUS_CHOICES = [
        (STATUS_ABERTO, "Aberto"),
        (STATUS_QUITADO, "Quitado"),
    ]

    pessoa_nome = models.CharField(max_length=150)
    valor = models.DecimalField(max_digits=12, decimal_places=2)
    data_emprestimo = models.DateField()
    previsao_devolucao = models.DateField(blank=True, null=True)
    conta_saida = models.ForeignKey(
        ContaFinanceira,
        on_delete=models.PROTECT,
        related_name="emprestimos_rapidos_saida",
    )
    observacao = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_ABERTO)
    data_quitacao = models.DateField(blank=True, null=True)
    conta_entrada_quitacao = models.ForeignKey(
        ContaFinanceira,
        on_delete=models.PROTECT,
        blank=True,
        null=True,
        related_name="emprestimos_rapidos_quitacao",
    )
    valor_devolvido = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True)
    observacao_quitacao = models.TextField(blank=True)
    operador = models.CharField(max_length=120, blank=True)
    operador_quitacao = models.CharField(max_length=120, blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["status", "-data_emprestimo", "-id"]

    def clean(self):
        if self.valor is not None and self.valor <= 0:
            raise ValidationError("O valor do emprestimo deve ser maior que zero.")
        if self.valor_devolvido is not None and self.valor_devolvido < 0:
            raise ValidationError("O valor devolvido nao pode ser negativo.")
        if self.status == self.STATUS_QUITADO:
            if not self.data_quitacao:
                raise ValidationError("Informe a data de quitacao.")
            if not self.conta_entrada_quitacao_id:
                raise ValidationError("Informe a conta de entrada da devolucao.")
            if self.valor_devolvido != self.valor:
                raise ValidationError("Na primeira versao, a devolucao deve ser igual ao valor emprestado.")

    def __str__(self):
        return f"{self.pessoa_nome} - R$ {self.valor} - {self.get_status_display()}"


class EmprestimoDivida(models.Model):
    TIPO_EMPRESTIMO_RECEBIDO = "emprestimo_recebido"
    TIPO_DIVIDA_AVULSA = "divida_avulsa"
    TIPO_FINANCIAMENTO = "financiamento"
    TIPO_OUTRO = "outro"
    TIPO_CHOICES = [
        (TIPO_EMPRESTIMO_RECEBIDO, "Emprestimo recebido"),
        (TIPO_DIVIDA_AVULSA, "Divida avulsa"),
        (TIPO_FINANCIAMENTO, "Financiamento"),
        (TIPO_OUTRO, "Outro"),
    ]

    STATUS_ABERTO = "aberto"
    STATUS_PARCIAL = "parcial"
    STATUS_QUITADO = "quitado"
    STATUS_CANCELADO = "cancelado"
    STATUS_CHOICES = [
        (STATUS_ABERTO, "Aberto"),
        (STATUS_PARCIAL, "Parcial"),
        (STATUS_QUITADO, "Quitado"),
        (STATUS_CANCELADO, "Cancelado"),
    ]

    tipo = models.CharField(max_length=30, choices=TIPO_CHOICES)
    credor = models.CharField(max_length=150)
    descricao = models.CharField(max_length=255, blank=True)
    valor_original = models.DecimalField(max_digits=12, decimal_places=2)
    saldo_devedor = models.DecimalField(max_digits=12, decimal_places=2, blank=True)
    data_contratacao = models.DateField()
    data_vencimento = models.DateField(blank=True, null=True)
    quantidade_parcelas = models.PositiveIntegerField(blank=True, null=True)
    valor_parcela = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_ABERTO)
    observacao = models.TextField(blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["status", "data_vencimento", "id"]

    def clean(self):
        if self.valor_original is not None and self.valor_original < 0:
            raise ValidationError("O valor original nao pode ser negativo.")
        if self.saldo_devedor is not None and self.saldo_devedor < 0:
            raise ValidationError("O saldo devedor nao pode ser negativo.")
        if self.valor_parcela is not None and self.valor_parcela < 0:
            raise ValidationError("O valor da parcela nao pode ser negativo.")

    def atualizar_status_por_saldo(self):
        if self.status == self.STATUS_CANCELADO:
            return
        if self.saldo_devedor <= 0:
            self.saldo_devedor = Decimal("0.00")
            self.status = self.STATUS_QUITADO
        elif self.saldo_devedor < self.valor_original:
            self.status = self.STATUS_PARCIAL
        else:
            self.status = self.STATUS_ABERTO

    def save(self, *args, **kwargs):
        if self.saldo_devedor is None:
            self.saldo_devedor = self.valor_original
        self.atualizar_status_por_saldo()
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.credor} - R$ {self.valor_original} - {self.get_status_display()}"


class PagamentoEmprestimoDivida(models.Model):
    divida = models.ForeignKey(
        EmprestimoDivida,
        on_delete=models.PROTECT,
        related_name="pagamentos",
    )
    valor = models.DecimalField(max_digits=12, decimal_places=2)
    data_pagamento = models.DateField()
    forma_pagamento = models.CharField(max_length=50, blank=True)
    observacao = models.CharField(max_length=255, blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-data_pagamento", "-id"]

    def clean(self):
        if self.valor is not None and self.valor <= 0:
            raise ValidationError("O valor do pagamento deve ser maior que zero.")

    def __str__(self):
        return f"Pagamento R$ {self.valor} - {self.divida}"


class DespesaDiaria(models.Model):
    CATEGORIA_GASOLINA = "gasolina"
    CATEGORIA_ALIMENTACAO = "alimentacao"
    CATEGORIA_GELO = "gelo"
    CATEGORIA_ESTACIONAMENTO = "estacionamento"
    CATEGORIA_FRETE_ENTREGA = "frete_entrega"
    CATEGORIA_AJUDANTE_DIARIA = "ajudante_diaria"
    CATEGORIA_MANUTENCAO = "manutencao"
    CATEGORIA_MATERIAL_APOIO = "material_apoio"
    CATEGORIA_COMPRA_EMERGENCIAL = "compra_emergencial"
    CATEGORIA_PESSOAL = "pessoal"
    CATEGORIA_OUTROS = "outros"
    CATEGORIA_CHOICES = [
        (CATEGORIA_GASOLINA, "Gasolina"),
        (CATEGORIA_ALIMENTACAO, "Lanche / Alimentacao"),
        (CATEGORIA_GELO, "Gelo"),
        (CATEGORIA_ESTACIONAMENTO, "Estacionamento"),
        (CATEGORIA_FRETE_ENTREGA, "Frete / Entrega"),
        (CATEGORIA_AJUDANTE_DIARIA, "Ajudante / Diaria"),
        (CATEGORIA_MANUTENCAO, "Manutencao"),
        (CATEGORIA_MATERIAL_APOIO, "Material de apoio"),
        (CATEGORIA_COMPRA_EMERGENCIAL, "Compra emergencial"),
        (CATEGORIA_PESSOAL, "Despesa pessoal"),
        (CATEGORIA_OUTROS, "Outros"),
    ]

    FORMA_PIX = "Pix"
    FORMA_DINHEIRO = "Dinheiro"
    FORMA_CARTAO = "Cartao"
    FORMA_BOLETO = "Boleto"
    FORMA_TRANSFERENCIA = "Transferencia"
    FORMA_OUTRO = "Outro"
    FORMA_PAGAMENTO_CHOICES = [
        (FORMA_PIX, "Pix"),
        (FORMA_DINHEIRO, "Dinheiro"),
        (FORMA_CARTAO, "Cartao"),
        (FORMA_BOLETO, "Boleto"),
        (FORMA_TRANSFERENCIA, "Transferencia"),
        (FORMA_OUTRO, "Outro"),
    ]

    data_hora = models.DateTimeField(default=timezone.now)
    valor = models.DecimalField(max_digits=12, decimal_places=2)
    categoria = models.CharField(max_length=40, choices=CATEGORIA_CHOICES)
    forma_pagamento = models.CharField(max_length=40, choices=FORMA_PAGAMENTO_CHOICES, default=FORMA_PIX)
    operador = models.CharField(max_length=120, blank=True)
    observacao = models.TextField(blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-data_hora", "-id"]

    def __str__(self):
        return f"{self.get_categoria_display()} - R$ {self.valor}"


class EntregaRota(models.Model):
    TIPO_UNITARIA = "unitaria"
    TIPO_ROTA = "rota"
    TIPO_CHOICES = [
        (TIPO_UNITARIA, "Entrega unitaria"),
        (TIPO_ROTA, "Rota com varias entregas"),
    ]

    STATUS_ABERTA = "aberta"
    STATUS_EM_CARREGAMENTO = "em_carregamento"
    STATUS_SAIU_PARA_ENTREGA = "saiu_para_entrega"
    STATUS_CONCLUIDA = "concluida"
    STATUS_CHOICES = [
        (STATUS_ABERTA, "Aberta"),
        (STATUS_EM_CARREGAMENTO, "Em carregamento"),
        (STATUS_SAIU_PARA_ENTREGA, "Saiu para entrega"),
        (STATUS_CONCLUIDA, "Concluida"),
    ]

    data = models.DateField(default=timezone.localdate)
    tipo = models.CharField(max_length=20, choices=TIPO_CHOICES)
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default=STATUS_ABERTA)
    observacao = models.TextField(blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-data", "-id"]

    def __str__(self):
        return f"{self.get_tipo_display()} #{self.id}"


class EntregaRotaItem(models.Model):
    STATUS_PENDENTE = "pendente"
    STATUS_CARREGADA = "carregada"
    STATUS_ENTREGUE = "entregue"
    STATUS_PARCIAL = "parcial"
    STATUS_CANCELADA = "cancelada"
    STATUS_CHOICES = [
        (STATUS_PENDENTE, "Pendente"),
        (STATUS_CARREGADA, "Carregada"),
        (STATUS_ENTREGUE, "Entregue"),
        (STATUS_PARCIAL, "Parcial"),
        (STATUS_CANCELADA, "Cancelada"),
    ]

    rota = models.ForeignKey(
        EntregaRota,
        on_delete=models.CASCADE,
        related_name="itens",
    )
    venda = models.ForeignKey(
        Venda,
        on_delete=models.CASCADE,
        related_name="entregas_rota",
    )
    ordem_entrega = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDENTE)
    conferido_cliente = models.BooleanField(default=False)
    entrega_concluida = models.BooleanField(default=False)
    is_pendencia = models.BooleanField(default=False)
    origem_pendencia = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="reentregas_pendencia",
    )
    observacao = models.TextField(blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["ordem_entrega", "id"]
        unique_together = [("rota", "venda", "is_pendencia")]

    def __str__(self):
        return f"Entrega #{self.rota_id} - Venda #{self.venda_id}"


class EntregaChecklistItem(models.Model):
    rota_item = models.ForeignKey(
        EntregaRotaItem,
        on_delete=models.CASCADE,
        related_name="checklist_itens",
    )
    item_venda = models.ForeignKey(
        ItemVenda,
        on_delete=models.CASCADE,
        related_name="checklists_entrega",
    )
    carregado = models.BooleanField(default=False)
    entregue = models.BooleanField(default=False)
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["item_venda_id"]
        unique_together = [("rota_item", "item_venda")]

    def __str__(self):
        return f"Checklist entrega #{self.rota_item_id} - item #{self.item_venda_id}"
