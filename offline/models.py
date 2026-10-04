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
