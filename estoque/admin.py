from django.contrib import admin
from .models import Categoria, Fornecedor, FornecedorContato, Funcionario, ItemVenda, PixRecebido, Produto, ProdutoFornecedor, Unidade, Venda

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
