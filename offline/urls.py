from django.urls import path
from django.contrib.auth.views import LoginView
from . import views

urlpatterns = [
    path("offline/login/", LoginView.as_view(template_name="offline/login.html"), name="offline_login"),
    path("offline/", views.shell, name="offline_pilot"),
    path("service-worker.js", views.service_worker, name="offline_worker"),
    path("api/offline/health/", views.health),
    path("api/offline/snapshot/", views.snapshot),
    path("api/offline/observations/", views.synchronize),
]
