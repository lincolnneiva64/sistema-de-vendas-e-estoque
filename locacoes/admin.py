from django.contrib import admin
from estoque.admin_horarios import HorariosAdminMixin, coluna_horario

from .models import (
    ConfiguracaoLocacao,
    EventoLocacao,
    FaixaPrecoLocacao,
    ItemLocacao,
    Locacao,
    MovimentoEstoqueLocacao,
    PagamentoLocacao,
    RegistroCobrancaLocacao,
    TarefaOperacionalLocacao,
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
class MovimentoEstoqueLocacaoAdmin(HorariosAdminMixin, admin.ModelAdmin):
    horarios_admin = {'data_hora': 'Horário de referência do movimento', 'criado_em': 'Movimento registrado no servidor'}
    horario_data_hora = coluna_horario('data_hora', 'Horário de referência do movimento')
    horario_criado_em = coluna_horario('criado_em', 'Movimento registrado no servidor')
    list_display = ('horario_data_hora', 'item', 'tipo', 'quantidade', 'saldo_anterior', 'saldo_posterior', 'responsavel', 'locacao', 'horario_criado_em')
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
class LocacaoAdmin(HorariosAdminMixin, admin.ModelAdmin):
    horarios_admin = {'termo_gerado_em': 'Geração do termo registrada', 'cancelada_em': 'Cancelamento registrado', 'criado_em': 'Contrato criado no servidor', 'atualizado_em': 'Última atualização no servidor'}
    list_display = ('id', 'nome_contratante', 'status', 'status_financeiro', 'data_entrega', 'data_prevista_devolucao', 'data_vencimento_saldo', 'total', 'total_pago', 'saldo_devedor')
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


@admin.register(RegistroCobrancaLocacao)
class RegistroCobrancaLocacaoAdmin(HorariosAdminMixin, admin.ModelAdmin):
    horarios_admin = {'criado_em': 'Contato registrado no servidor'}
    horario_criado_em = coluna_horario('criado_em', 'Contato registrado no servidor')
    list_display = ('locacao', 'tipo', 'status', 'criado_por_nome', 'horario_criado_em')
    list_filter = ("tipo", "status", "criado_em")
    search_fields = ("locacao__id", "locacao__pessoa_avulsa_nome", "locacao__cliente__nome", "observacao")
    readonly_fields = ("locacao", "tipo", "status", "observacao", "criado_por_nome", "criado_em")


@admin.register(TarefaOperacionalLocacao)
class TarefaOperacionalLocacaoAdmin(HorariosAdminMixin, admin.ModelAdmin):
    horarios_admin = {'confirmado_em': 'Confirmação registrada no servidor', 'tentativa_em': 'Tentativa registrada no servidor', 'resolvida_em': 'Resolução administrativa registrada', 'criado_em': 'Tarefa criada no servidor', 'atualizado_em': 'Última atualização no servidor'}
    horario_confirmado_em = coluna_horario('confirmado_em', 'Confirmação registrada no servidor')
    list_display = ('locacao', 'tipo', 'status', 'data_agendada', 'horario_agendado', 'confirmado_por', 'horario_confirmado_em')
    list_filter = ("tipo", "status", "data_agendada")
    search_fields = ("locacao__id", "locacao__pessoa_avulsa_nome", "locacao__cliente__nome", "confirmado_por")
    readonly_fields = ("criado_em", "atualizado_em")


@admin.register(EventoLocacao)
class EventoLocacaoAdmin(HorariosAdminMixin, admin.ModelAdmin):
    horarios_admin = {'criado_em': 'Evento registrado no servidor'}
    horario_criado_em = coluna_horario('criado_em', 'Evento registrado no servidor')
    list_display = ('locacao', 'tipo', 'responsavel', 'horario_criado_em')
    list_filter = ("tipo", "criado_em")
    search_fields = ("locacao__id", "descricao", "responsavel")
    readonly_fields = ("locacao", "tipo", "descricao", "responsavel", "criado_em")


@admin.register(PagamentoLocacao)
class PagamentoLocacaoAdmin(HorariosAdminMixin, admin.ModelAdmin):
    horarios_admin = {'data_hora': 'Horário informado do pagamento', 'recibo_enviado_em': 'Confirmação de envio do recibo registrada', 'recibo_dispensado_em': 'Dispensa do recibo registrada', 'criado_em': 'Pagamento registrado no servidor'}
    horario_data_hora = coluna_horario('data_hora', 'Horário informado do pagamento')
    list_display = ('locacao', 'valor', 'forma_pagamento', 'horario_data_hora', 'recibo_status', 'responsavel', 'movimento_financeiro')
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
