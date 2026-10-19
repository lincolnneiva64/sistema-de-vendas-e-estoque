from django.contrib.auth.views import LoginView
from django.urls import path
from . import views
from . import ajustes_views

app_name = "inventario"
urlpatterns = [
    path("sessao/<int:inventario_id>/ajustes/", ajustes_views.painel, name="ajustes"),
    path("sessao/<int:inventario_id>/historico/<int:rodada_id>/ajuste/<int:movimento_id>/", views.movimento_manual, name="movimento_manual"),
    path("sessao/<int:inventario_id>/historico/<int:rodada_id>/", views.historico_produto, name="historico_produto"),
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
