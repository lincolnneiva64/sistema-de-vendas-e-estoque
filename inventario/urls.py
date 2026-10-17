from django.contrib.auth.views import LoginView
from django.urls import path
from . import views

app_name = "inventario"
urlpatterns = [
    path("", views.lista, name="lista"),
    path("entrar/", LoginView.as_view(template_name="inventario/login.html", next_page="inventario:lista"), name="login"),
    path("sessao/<int:inventario_id>/", views.detalhe, name="detalhe"),
    path("sessao/<int:inventario_id>/relatorio.csv", views.relatorio, name="relatorio"),
    path("acesso/<str:token>/", views.convite, name="convite"),
    path("contagem/", views.mobile, name="mobile"),
    path("contagem/finalizar/", views.finalizar, name="finalizar"),
    path("contagem/<int:rodada_id>/", views.contar, name="contar"),
    path("contagem/<int:rodada_id>/sinalizar/", views.sinalizar, name="sinalizar"),
]
