from django.urls import path
from . import views

app_name = "estoque"

urlpatterns = [
    path("", views.home, name="home"),
    path("cadastrar/", views.cadastrar_produto, name="cadastrar_produto"),
    path("cadastrar-unidade/", views.cadastrar_unidade, name="cadastrar_unidade"),
    path("unidades/", views.unidades_produto, name="unidades_produto"),
    path("categorias/", views.categorias_produto, name="categorias_produto"),
    path("clientes/", views.clientes, name="clientes"),
    path("clientes/consulta/", views.clientes_consulta, name="clientes_consulta"),
    path("clientes/autocomplete/", views.clientes_autocomplete, name="clientes_autocomplete"),
    path("funcionarios/", views.funcionarios, name="funcionarios"),
    path("verificar-produto/", views.verificar_produto, name="verificar_produto"),
    path("produto/<int:pk>/", views.produto_detalhe, name="produto_detalhe"),
    path("produto/<int:pk>/editar/", views.produto_editar, name="produto_editar"),
    path("vendas/", views.vendas, name="vendas"),
    path("vendas/consultar/", views.consultar_vendas, name="consultar_vendas"),
    path("vendas/gravar/", views.gravar_venda, name="gravar_venda"),
    path("entregas/", views.entregas_dia, name="entregas_dia"),
    path("entregas/<int:pk>/", views.entrega_rota_detalhe, name="entrega_rota_detalhe"),
    path("entregas/<int:pk>/checklist/", views.entrega_rota_checklist, name="entrega_rota_checklist"),
    path("vendas/<int:pk>/", views.venda_detalhe, name="venda_detalhe"),
    path("vendas/<int:pk>/whatsapp-imagem/", views.venda_whatsapp_imagem, name="venda_whatsapp_imagem"),
    path("vendas/<int:pk>/whatsapp-pdf/", views.venda_whatsapp_pdf, name="venda_whatsapp_pdf"),
    path("vendas/<int:pk>/preparar-whatsapp/", views.preparar_whatsapp_venda, name="preparar_whatsapp_venda"),
    path("vendas/<int:pk>/registrar-impressao/", views.registrar_impressao, name="registrar_impressao"),
    path("vendas/<int:pk>/registrar-whatsapp-aberto/", views.registrar_whatsapp_aberto, name="registrar_whatsapp_aberto"),
    path("vendas/<int:pk>/registrar-checklist-whatsapp-aberto/", views.registrar_checklist_whatsapp_aberto, name="registrar_checklist_whatsapp_aberto"),
    path("vendas/<int:pk>/confirmar-checklist-whatsapp/", views.confirmar_checklist_whatsapp, name="confirmar_checklist_whatsapp"),
    path("vendas/<int:pk>/confirmar-whatsapp/", views.confirmar_whatsapp, name="confirmar_whatsapp"),
    path("excluir/<int:pk>/", views.produto_excluir, name="produto_excluir"),
    path("lixeira/", views.lixeira, name="lixeira"),
    path("restaurar/<int:pk>/", views.produto_restaurar, name="produto_restaurar"),
    path("excluir-definitivo/<int:pk>/", views.produto_excluir_definitivo, name="produto_excluir_definitivo"),
]
