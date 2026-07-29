from django.urls import path

from . import views


app_name = "locacoes"

urlpatterns = [
    path("", views.lista, name="lista"),
    path("nova/", views.nova, name="nova"),
    path("configuracoes/", views.configuracoes, name="configuracoes"),
    path("<int:pk>/", views.detalhe, name="detalhe"),
    path("<int:pk>/cancelar/", views.cancelar, name="cancelar"),
    path("<int:pk>/saiu-para-entrega/", views.marcar_saiu_para_entrega, name="marcar_saiu_para_entrega"),
    path("<int:pk>/confirmar-entrega/", views.confirmar_entrega, name="confirmar_entrega"),
    path("<int:pk>/devolucao/", views.registrar_devolucao, name="registrar_devolucao"),
]
