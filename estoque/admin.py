from django.contrib import admin
from .models import Categoria, EnvioListaCompraFornecedor, Fornecedor, FornecedorContato, Funcionario, ItemVenda, PixRecebido, Produto, ProdutoFornecedor, ResolucaoVisitaFornecedor, Unidade, Venda

admin.site.register(Produto)
admin.site.register(Unidade)
admin.site.register(Categoria)
admin.site.register(FornecedorContato)
admin.site.register(Funcionario)
admin.site.register(Venda)
admin.site.register(ItemVenda)
admin.site.register(PixRecebido)


class FornecedorContatoInline(admin.TabularInline):
    model = FornecedorContato
    extra = 1


@admin.register(Fornecedor)
class FornecedorAdmin(admin.ModelAdmin):
    list_display = ("nome", "nome_fantasia", "telefone_whatsapp", "ativo")
    search_fields = ("nome", "nome_fantasia", "telefone_whatsapp", "contatos__nome", "contatos__telefone_whatsapp")
    list_filter = ("ativo",)
    inlines = [FornecedorContatoInline]


@admin.register(ProdutoFornecedor)
class ProdutoFornecedorAdmin(admin.ModelAdmin):
    list_display = ("produto", "fornecedor", "ativo", "criado_em")
    search_fields = ("produto__nome", "fornecedor__nome", "fornecedor__nome_fantasia")
    list_filter = ("ativo",)




@admin.register(ResolucaoVisitaFornecedor)
class ResolucaoVisitaFornecedorAdmin(admin.ModelAdmin):
    list_display = (
        "fornecedor",
        "data_visita_original",
        "tipo_resolucao",
        "nova_data_visita",
        "responsavel",
        "resolvido_em",
    )
    search_fields = (
        "fornecedor__nome",
        "observacao",
        "responsavel__username",
    )
    list_filter = (
        "tipo_resolucao",
        "data_visita_original",
        "resolvido_em",
    )
    readonly_fields = (
        "resolvido_em",
        "atualizado_em",
    )


@admin.register(EnvioListaCompraFornecedor)
class EnvioListaCompraFornecedorAdmin(admin.ModelAdmin):
    list_display = ("lista", "fornecedor", "nome_destinatario", "telefone_destinatario", "origem_destinatario", "confirmado_em", "confirmado_por")
    search_fields = ("lista__id", "fornecedor__nome", "nome_destinatario", "telefone_destinatario")
    list_filter = ("origem_destinatario", "confirmado_em")
    readonly_fields = ("criado_em", "atualizado_em")
