from django.contrib import admin

from .models import (
    ConfiguracaoLocacao,
    EventoLocacao,
    FaixaPrecoLocacao,
    ItemLocacao,
    Locacao,
    MovimentoEstoqueLocacao,
    PagamentoLocacao,
)


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
    list_display = (
        "id",
        "nome_contratante",
        "status",
        "status_financeiro",
        "data_entrega",
        "data_prevista_devolucao",
        "total",
        "total_pago",
        "saldo_devedor",
    )
    list_filter = ("status", "status_financeiro", "data_entrega", "faixa_preco")
    search_fields = ("pessoa_avulsa_nome", "cliente__nome", "endereco_entrega")
    inlines = [ItemLocacaoInline]
    readonly_fields = (
        "status",
        "total_pago",
        "saldo_devedor",
        "status_financeiro",
        "valor_reposicao_mesa_snapshot",
        "valor_reposicao_cadeira_snapshot",
        "termo_gerado_em",
        "termo_gerado_por",
        "cancelada_em",
        "criado_em",
        "atualizado_em",
    )


@admin.register(EventoLocacao)
class EventoLocacaoAdmin(admin.ModelAdmin):
    list_display = ("locacao", "tipo", "responsavel", "criado_em")
    list_filter = ("tipo", "criado_em")
    search_fields = ("locacao__id", "descricao", "responsavel")
    readonly_fields = ("locacao", "tipo", "descricao", "responsavel", "criado_em")


@admin.register(PagamentoLocacao)
class PagamentoLocacaoAdmin(admin.ModelAdmin):
    list_display = (
        "locacao",
        "valor",
        "forma_pagamento",
        "data_hora",
        "recibo_status",
        "responsavel",
        "movimento_financeiro",
    )
    list_filter = ("forma_pagamento", "recibo_status", "data_hora")
    search_fields = ("locacao__id", "locacao__pessoa_avulsa_nome", "locacao__cliente__nome", "responsavel")
    readonly_fields = (
        "locacao",
        "valor",
        "data_hora",
        "forma_pagamento",
        "observacao",
        "responsavel",
        "movimento_financeiro",
        "recibo_status",
        "recibo_enviado_em",
        "recibo_enviado_por",
        "recibo_dispensado_em",
        "recibo_dispensado_por",
        "recibo_dispensa_observacao",
        "criado_em",
    )
