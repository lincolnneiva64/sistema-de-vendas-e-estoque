from django.urls import path

from . import views


app_name = "locacoes"

urlpatterns = [
    path("", views.lista, name="lista"),
    path("nova/", views.nova, name="nova"),
    path("configuracoes/", views.configuracoes, name="configuracoes"),
    path("<int:pk>/", views.detalhe, name="detalhe"),
    path("<int:pk>/cancelar/", views.cancelar, name="cancelar"),
]
