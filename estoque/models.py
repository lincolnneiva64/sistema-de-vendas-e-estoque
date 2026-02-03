from django.db import models

class Produto(models.Model):
    nome = models.CharField(max_length=120)
    codigo = models.CharField(max_length=50, blank=True, null=True)
    categoria = models.CharField(max_length=60, blank=True, null=True)
    preco = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    quantidade = models.IntegerField(default=0)
    fornecedor = models.CharField(max_length=120, blank=True, null=True)

    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.nome

