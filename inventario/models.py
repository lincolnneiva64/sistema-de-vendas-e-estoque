from django.conf import settings
from django.db import models
from uuid import uuid4


class Inventario(models.Model):
    titulo = models.CharField(max_length=160)
    criterio = models.CharField(max_length=20, choices=[("categoria", "Categoria"), ("fornecedor", "Fornecedor"), ("manual", "Produtos selecionados")])
    filtro = models.JSONField(default=dict)
    criado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    criado_em = models.DateTimeField(auto_now_add=True)
    ciclo = models.PositiveIntegerField(default=1)
    versao = models.PositiveIntegerField(default=0)
    cancelada_em = models.DateTimeField(null=True, blank=True)
    cancelada_por = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="inventarios_cancelados")
    finalizada_em = models.DateTimeField(null=True, blank=True)
    finalizacao_versao = models.PositiveIntegerField(null=True)
    finalizada_por = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="inventarios_finalizados")
    finalizada_por_acesso = models.ForeignKey("AcessoInventario", null=True, on_delete=models.PROTECT, related_name="inventarios_finalizados")

    class Meta:
        permissions = [("gerenciar_inventario", "Gerenciar inventários e acessos"),
                       ("revisar_inventario", "Revisar contagens e solicitar recontagem"),
                       ("aprovar_ajuste", "Aprovar ajuste de inventário")]


class ItemInventario(models.Model):
    inventario = models.ForeignKey(Inventario, on_delete=models.PROTECT, related_name="itens")
    produto = models.ForeignKey("estoque.Produto", on_delete=models.PROTECT)
    cadastro = models.JSONField()  # Immutable selection-time conversion and display snapshot.
    custo_base = models.DecimalField(max_digits=10, decimal_places=2, null=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["inventario", "produto"], name="inventario_produto_unico")]


class RodadaContagem(models.Model):
    item = models.ForeignKey(ItemInventario, on_delete=models.PROTECT, related_name="rodadas")
    numero = models.PositiveIntegerField()
    versao = models.PositiveIntegerField(default=0)
    embalagens = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    avulsos = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    contado = models.DecimalField(max_digits=18, decimal_places=9, null=True, blank=True)
    contado_base = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    esperado = models.DecimalField(max_digits=12, decimal_places=3, null=True)
    sequencia_ok = models.PositiveBigIntegerField(null=True)
    ok_em = models.DateTimeField(null=True)
    encerrada_em = models.DateTimeField(null=True)
    revisao_sequencia = models.PositiveBigIntegerField(null=True)
    revisao_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, related_name="revisoes_inventario")
    revisao_motivo = models.TextField(blank=True)
    revisada_em = models.DateTimeField(null=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["item", "numero"], name="inventario_rodada_unica")]


class AcessoInventario(models.Model):
    inventario = models.ForeignKey(Inventario, on_delete=models.PROTECT, related_name="acessos")
    funcionario = models.ForeignKey("estoque.Funcionario", on_delete=models.PROTECT)
    funcionario_nome = models.CharField(max_length=150)
    token_hash = models.CharField(max_length=64, unique=True)
    expira_em = models.DateTimeField()
    revogado_em = models.DateTimeField(null=True)
    criado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    criado_em = models.DateTimeField(auto_now_add=True)
    ciclo = models.PositiveIntegerField(default=1)
    enviada_em = models.DateTimeField(null=True, blank=True)
    envio_versao = models.PositiveIntegerField(null=True, blank=True)


class ConfirmacaoContagem(models.Model):
    """Snapshot of an accepted OK, never overwritten by subsequent drafts/OKs."""
    rodada = models.ForeignKey(RodadaContagem, on_delete=models.PROTECT, related_name="confirmacoes")
    versao_entrada = models.PositiveIntegerField()
    embalagens = models.DecimalField(max_digits=12, decimal_places=3)
    avulsos = models.DecimalField(max_digits=12, decimal_places=3, null=True)
    contado = models.DecimalField(max_digits=18, decimal_places=9)
    contado_base = models.DecimalField(max_digits=12, decimal_places=3)
    esperado = models.DecimalField(max_digits=12, decimal_places=3)
    sequencia_ok = models.PositiveBigIntegerField()
    ok_em = models.DateTimeField()
    acesso = models.ForeignKey(AcessoInventario, null=True, on_delete=models.PROTECT)
    responsavel_nome = models.CharField(max_length=150, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["rodada", "versao_entrada"], name="inventario_ok_versao_unica")]


class AtribuicaoContagem(models.Model):
    rodada = models.OneToOneField(RodadaContagem, on_delete=models.PROTECT, related_name="atribuicao")
    acesso = models.ForeignKey(AcessoInventario, on_delete=models.PROTECT, related_name="atribuicoes")


class SequenciaEstoque(models.Model):
    produto = models.OneToOneField("estoque.Produto", on_delete=models.CASCADE, primary_key=True)
    sequencia = models.PositiveBigIntegerField(default=0)


class MovimentoObservado(models.Model):
    # Written by database triggers, not an alternative stock balance.
    produto = models.ForeignKey("estoque.Produto", on_delete=models.PROTECT, related_name="movimentos_inventario")
    sequencia = models.PositiveBigIntegerField()
    antes = models.DecimalField(max_digits=12, decimal_places=3, null=True)
    depois = models.DecimalField(max_digits=12, decimal_places=3, null=True)
    registrado_em = models.DateTimeField()
    origem = models.CharField(max_length=40, default="nao_identificada", db_default="nao_identificada")
    referencia = models.CharField(max_length=160, blank=True, default="", db_default="")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["produto", "sequencia"], name="inventario_movimento_sequencia")]


class ConciliacaoMovimento(models.Model):
    rodada = models.ForeignKey(RodadaContagem, on_delete=models.PROTECT, related_name="conciliacoes")
    movimento = models.ForeignKey(MovimentoObservado, on_delete=models.PROTECT)
    responsavel = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    referencia = models.TextField()
    conciliado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["rodada", "movimento"], name="inventario_conciliacao_unica")]


class DecisaoAjuste(models.Model):
    item = models.OneToOneField(ItemInventario, on_delete=models.PROTECT, related_name="decisao")
    rodada = models.OneToOneField(RodadaContagem, on_delete=models.PROTECT)
    movimentacao = models.OneToOneField("estoque.MovimentacaoEstoqueManual", on_delete=models.PROTECT)
    diferenca = models.DecimalField(max_digits=12, decimal_places=3)
    justificativa = models.TextField()
    aprovado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    aprovado_em = models.DateTimeField(auto_now_add=True)


class EventoInventario(models.Model):
    inventario = models.ForeignKey(Inventario, on_delete=models.PROTECT, related_name="eventos")
    rodada = models.ForeignKey(RodadaContagem, on_delete=models.PROTECT, null=True)
    acesso = models.ForeignKey(AcessoInventario, on_delete=models.PROTECT, null=True)
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True)
    acao = models.CharField(max_length=40)
    dados = models.JSONField(default=dict)
    criado_em = models.DateTimeField(auto_now_add=True)


class SinalizacaoFisica(models.Model):
    rodada = models.ForeignKey(RodadaContagem, on_delete=models.PROTECT, related_name="sinalizacoes")
    chave = models.UUIDField(unique=True)
    descricao = models.TextField()
    acesso = models.ForeignKey(AcessoInventario, on_delete=models.PROTECT, null=True)
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, related_name="sinalizacoes_inventario")
    criado_em = models.DateTimeField(auto_now_add=True)
    resolvida_em = models.DateTimeField(null=True)
    resolvida_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, related_name="sinalizacoes_inventario_resolvidas")
    evidencia = models.TextField(blank=True)


class PropostaAjuste(models.Model):
    """Approval freezes evidence, never a stock write. No delete/admin endpoint."""
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    rodada = models.ForeignKey(RodadaContagem, on_delete=models.PROTECT, related_name="propostas")
    confirmacao = models.ForeignKey(ConfirmacaoContagem, on_delete=models.PROTECT)
    assinatura = models.CharField(max_length=64)
    evidencia = models.JSONField()
    justificativa = models.TextField()
    estado = models.CharField(max_length=12, choices=[("aprovado", "Aprovado"), ("rejeitado", "Rejeitado"), ("aplicado", "Aplicado")])
    autorizado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["rodada", "assinatura"], name="inventario_proposta_evidencia_unica")]


class RegistroImutavel(models.Model):
    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError("Registro de auditoria imutável. Use uma movimentação compensatória.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("A auditoria não pode ser apagada.")


class AplicacaoAjuste(RegistroImutavel):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    proposta = models.OneToOneField(PropostaAjuste, on_delete=models.PROTECT, related_name="aplicacao")
    decisao = models.OneToOneField(DecisaoAjuste, on_delete=models.PROTECT)
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    aplicado_em = models.DateTimeField(auto_now_add=True)
    lote = models.UUIDField()
    antes = models.DecimalField(max_digits=12, decimal_places=3)
    diferenca = models.DecimalField(max_digits=12, decimal_places=3)
    depois = models.DecimalField(max_digits=12, decimal_places=3)
    unidade = models.CharField(max_length=20)
    evidencia = models.JSONField()


class EventoAjuste(RegistroImutavel):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    proposta = models.ForeignKey(PropostaAjuste, on_delete=models.PROTECT, related_name="eventos_ajuste")
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    acao = models.CharField(max_length=20)
    dados = models.JSONField(default=dict)
    criado_em = models.DateTimeField(auto_now_add=True)
