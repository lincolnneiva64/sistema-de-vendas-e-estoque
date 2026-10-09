from django.contrib import admin
from django import forms
from django.core.exceptions import ValidationError
from .models import Categoria, EnvioListaCompraFornecedor, Fornecedor, FornecedorContato, Funcionario, ItemVenda, OperacaoRecebimentoCliente, PixRecebido, Produto, ProdutoFornecedor, ResolucaoVisitaFornecedor, Unidade, Venda

class ProdutoAdminForm(forms.ModelForm):
    class Meta:
        model = Produto
        fields = '__all__'

    def clean(self):
        values=super().clean()
        if self.instance.pk:
            from .services.precos_vinculados import impedir_escrita_direta
            try:impedir_escrita_direta(self.instance.pk,values)
            except ValidationError as exc:raise forms.ValidationError(exc.messages) from exc
        return values


@admin.register(Produto)
class ProdutoAdmin(admin.ModelAdmin):
    form = ProdutoAdminForm

    def changeform_view(self, request, object_id=None, form_url='', extra_context=None):
        # Keep validation and the legacy save in the same catalog critical
        # section: group changes cannot slip between clean() and save().
        if request.method == 'POST':
            from .services.precos_vinculados import bloquear_catalogo
            with bloquear_catalogo():
                return super().changeform_view(request, object_id, form_url, extra_context)
        return super().changeform_view(request, object_id, form_url, extra_context)
admin.site.register(Unidade)
admin.site.register(Categoria)
admin.site.register(FornecedorContato)
admin.site.register(Funcionario)
admin.site.register(Venda)
admin.site.register(ItemVenda)
admin.site.register(PixRecebido)


@admin.register(OperacaoRecebimentoCliente)
class OperacaoRecebimentoClienteAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "cliente",
        "cliente_nome_snapshot",
        "valor_recebido",
        "data_recebimento",
        "forma_pagamento",
        "rota_snapshot",
        "status_recibo",
        "criado_por",
        "criado_em",
    )
    list_filter = (
        "status_recibo",
        "data_recebimento",
        "forma_pagamento",
        "rota_snapshot",
    )
    search_fields = (
        "cliente__nome",
        "cliente_nome_snapshot",
    )


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
