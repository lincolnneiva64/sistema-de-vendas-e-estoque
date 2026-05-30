from django.db import models
from django.core.exceptions import ValidationError
from django.utils import timezone
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


class Funcionario(models.Model):
    nome = models.CharField(max_length=140)
    telefone_whatsapp = models.CharField(max_length=30, blank=True, null=True)
    telefone_whatsapp_normalizado = models.CharField(max_length=20, blank=True, null=True)
    ativo = models.BooleanField(default=True)
    pode_receber_checklist = models.BooleanField(default=False)
    observacoes = models.TextField(blank=True, null=True)
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-ativo", "-pode_receber_checklist", "nome"]
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

    class Meta:
        ordering = ["id"]

    def __str__(self):
        nome_produto = self.produto.nome if self.produto else "Produto nao identificado"
        return f"{nome_produto} - Venda #{self.venda_id}"


class ContaReceber(models.Model):
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
        return f"Conta a receber - Venda #{self.venda_id}"


class RecebimentoContaReceber(models.Model):
    conta = models.ForeignKey(
        ContaReceber,
        on_delete=models.CASCADE,
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
