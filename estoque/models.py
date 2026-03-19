from django.db import models
from django.core.exceptions import ValidationError
class Produto(models.Model):

    nome = models.CharField(max_length=120)
    codigo = models.CharField(max_length=50, blank=True, null=True)
    categoria = models.CharField(max_length=60, blank=True, null=True)

    preco_compra = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    preco_venda = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    preco_vista = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    preco_prazo = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    permitir_prejuizo = models.BooleanField(default=False)
    motivo_prejuizo = models.CharField(max_length=200, blank=True, null=True)

    quantidade = models.IntegerField(default=0)
    estoque_minimo = models.PositiveIntegerField(default=5)

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
            categoria_limpa = " ".join(self.categoria.strip().split())
            self.categoria = categoria_limpa.title()

        if self.fornecedor:
            fornecedor_limpo = " ".join(self.fornecedor.strip().split())
            self.fornecedor = fornecedor_limpo.title()
        if self.preco_vista and self.preco_vista > 0:
           self.preco_venda = self.preco_vista
        self.full_clean()
        super().save(*args, **kwargs)


