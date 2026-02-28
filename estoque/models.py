from django.db import models

class Produto(models.Model):
    nome = models.CharField(max_length=120)
    codigo = models.CharField(max_length=50, blank=True, null=True)
    categoria = models.CharField(max_length=60, blank=True, null=True)
    preco = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    quantidade = models.IntegerField(default=0)
    estoque_minimo = models.PositiveIntegerField(default=5)

    fornecedor = models.CharField(max_length=120, blank=True, null=True)

    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)



    def __str__(self):
        return self.nome

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
        super().save(*args, **kwargs)