from django.urls import path
from . import views

app_name = "estoque"

urlpatterns = [
    path("", views.home, name="home"),
    path("cadastrar/", views.cadastrar_produto, name="cadastrar_produto"),
path("produto/<int:pk>/", views.produto_detalhe, name="produto_detalhe"),
path("produto/<int:pk>/editar/", views.produto_editar, name="produto_editar"), 
]