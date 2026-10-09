from django.conf import settings
from django.db import models


class OperacaoSincronizacao(models.Model):
    operation_id = models.UUIDField(unique=True)
    device_id = models.UUIDField()
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    environment_id = models.CharField(max_length=160)
    type = models.CharField(max_length=60)
    schema_version = models.PositiveSmallIntegerField()
    aggregate_id = models.CharField(max_length=40)
    payload = models.JSONField()
    payload_hash = models.CharField(max_length=64)
    comando = models.JSONField()
    status = models.CharField(max_length=30)
    resultado = models.JSONField(default=dict)
    referencia = models.ForeignKey("locacoes.EventoLocacao", null=True, blank=True, on_delete=models.SET_NULL)
    recebido_em = models.DateTimeField(auto_now_add=True)
    concluido_em = models.DateTimeField(null=True)
    erro = models.TextField(blank=True)

    class Meta:
        permissions = [("registrar_observacao", "Preparar e sincronizar observacoes offline")]


class RevisaoVendaOffline(models.Model):
    # Separate immutable lineage: never update the rejected command/receipt.
    original = models.OneToOneField(OperacaoSincronizacao, on_delete=models.PROTECT,
                                   related_name="substituicao")
    substituta = models.OneToOneField(OperacaoSincronizacao, on_delete=models.PROTECT,
                                     related_name="revisao_origem")
    relacao = models.CharField(max_length=40, default="revisao_de_conflito")
    original_hash = models.CharField(max_length=64)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    environment_id = models.CharField(max_length=160)
    motivo_original = models.TextField()
    revisada_em = models.DateTimeField()
    registrado_em = models.DateTimeField(auto_now_add=True)


class EncerramentoVendaOffline(models.Model):
    """Administrative decision; the original commercial receipt stays immutable."""
    original = models.OneToOneField(OperacaoSincronizacao, on_delete=models.PROTECT,
                                   related_name="encerramento_administrativo")
    closure_id = models.UUIDField(unique=True)
    original_hash = models.CharField(max_length=64)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    environment_id = models.CharField(max_length=160)
    device_id = models.UUIDField()
    motivo = models.TextField()
    evidencia = models.JSONField()
    encerrado_em = models.DateTimeField(auto_now_add=True)
