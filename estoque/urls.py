from django.urls import path
from . import views

app_name = "estoque"

urlpatterns = [
    path("", views.home, name="home"),
    path("cadastrar/", views.cadastrar_produto, name="cadastrar_produto"),
    path("cadastrar-unidade/", views.cadastrar_unidade, name="cadastrar_unidade"),
    path("unidades/", views.unidades_produto, name="unidades_produto"),
    path("categorias/", views.categorias_produto, name="categorias_produto"),
    path("verificar-produto/", views.verificar_produto, name="verificar_produto"),
    path("produto/<int:pk>/", views.produto_detalhe, name="produto_detalhe"),
    path("produto/<int:pk>/editar/", views.produto_editar, name="produto_editar"),
    path("vendas/", views.vendas, name="vendas"),
    path("excluir/<int:pk>/", views.produto_excluir, name="produto_excluir"),
    path("lixeira/", views.lixeira, name="lixeira"),
    path("restaurar/<int:pk>/", views.produto_restaurar, name="produto_restaurar"),
    path("excluir-definitivo/<int:pk>/", views.produto_excluir_definitivo, name="produto_excluir_definitivo"),
]
