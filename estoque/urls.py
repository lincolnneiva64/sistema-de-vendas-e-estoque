from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("produtos/<int:pk>/", views.produto_detalhe, name="produto_detalhe"),
    path("produtos/<int:pk>/editar/", views.produto_editar, name="produto_editar"),
]
