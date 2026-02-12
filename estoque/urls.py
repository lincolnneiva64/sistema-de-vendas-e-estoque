from django.urls import path
from . import views

app_name = "estoque"

urlpatterns = [
    path("", views.home, name="home"),
    path("cadastrar/", views.cadastrar_produto, name="cadastrar_produto"),

]
 