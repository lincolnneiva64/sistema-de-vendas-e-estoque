from django.db import models
from django.core.exceptions import ValidationError
from .utils import normalize_category_name
class Produto(models.Model):

    nome = models.CharField(max_length=120)
    codigo = models.CharField(max_length=50, blank=True, null=True)
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

    quantidade = models.IntegerField(blank=True, null=True)
    estoque_minimo = models.PositiveIntegerField(blank=True, null=True)
    fornecedor = models.CharField(max_length=120, blank=True, null=True)
    excluido = models.BooleanField(default=False)
    excluido_em = models.DateTimeField(null=True, blank=True)
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
