from django.contrib import admin

from .models import ConfiguracaoLocacao, EventoLocacao, FaixaPrecoLocacao, ItemLocacao, Locacao, MovimentoEstoqueLocacao


@admin.register(ConfiguracaoLocacao)
class ConfiguracaoLocacaoAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "total_mesas",
        "total_cadeiras",
        "preco_mesa_avulsa_diaria",
        "preco_cadeira_avulsa_diaria",
        "valor_reposicao_mesa",
        "valor_reposicao_cadeira",
    )


@admin.register(FaixaPrecoLocacao)
class FaixaPrecoLocacaoAdmin(admin.ModelAdmin):
    list_display = ("nome", "codigo", "preco_jogo_diaria", "ordem", "ativa")
    list_editable = ("preco_jogo_diaria", "ordem", "ativa")
    list_filter = ("ativa",)
    search_fields = ("nome", "codigo")


@admin.register(MovimentoEstoqueLocacao)
class MovimentoEstoqueLocacaoAdmin(admin.ModelAdmin):
    list_display = (
        "data_hora",
        "item",
        "tipo",
        "quantidade",
        "saldo_anterior",
        "saldo_posterior",
        "responsavel",
        "locacao",
    )
    list_filter = ("item", "tipo", "data_hora")
    search_fields = ("responsavel", "observacao")
    readonly_fields = (
        "item",
        "tipo",
        "quantidade",
        "saldo_anterior",
        "saldo_posterior",
        "responsavel",
        "observacao",
        "locacao",
        "item_locacao",
        "data_hora",
        "criado_em",
    )


class ItemLocacaoInline(admin.TabularInline):
    model = ItemLocacao
    extra = 0
    readonly_fields = (
        "tipo",
        "quantidade",
        "preco_diaria_snapshot",
        "diarias",
        "valor_total",
        "ajuste_manual",
        "devolvida_boa",
        "quebrada",
        "perdida",
        "descartada",
    )
    can_delete = False


@admin.register(Locacao)
class LocacaoAdmin(admin.ModelAdmin):
    list_display = ("id", "nome_contratante", "status", "data_entrega", "data_prevista_devolucao", "total")
    list_filter = ("status", "data_entrega", "faixa_preco")
    search_fields = ("pessoa_avulsa_nome", "cliente__nome", "endereco_entrega")
    inlines = [ItemLocacaoInline]
    readonly_fields = ("status", "cancelada_em", "criado_em", "atualizado_em")


@admin.register(EventoLocacao)
class EventoLocacaoAdmin(admin.ModelAdmin):
    list_display = ("locacao", "tipo", "responsavel", "criado_em")
    list_filter = ("tipo", "criado_em")
    search_fields = ("locacao__id", "descricao", "responsavel")
    readonly_fields = ("locacao", "tipo", "descricao", "responsavel", "criado_em")
