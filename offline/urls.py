from django.urls import path
from django.contrib.auth.views import LoginView
from . import views

urlpatterns = [
    path("offline/assets/2-8d/<str:filename>", views.versioned_asset),
    path("api/vendas/online/", views.online_sale),
    path("api/vendas/online/<uuid:operation_id>/", views.online_sale_result),
    path("offline/assets/2-8ab/<str:filename>", views.versioned_asset),
    path("offline/assets/2-8c/<str:filename>", views.versioned_asset),
    path("offline/revisao/", views.revision_shell, name="offline_sale_revision"),
    path("offline/vendas-shell/", views.sales_shell, name="offline_sales_shell"),
    path("offline/login/", LoginView.as_view(template_name="offline/login.html", redirect_authenticated_user=True, next_page="offline_pilot"), name="offline_login"),
    path("offline/", views.shell, name="offline_pilot"),
    path("service-worker.js", views.service_worker, name="offline_worker"),
    path("api/offline/health/", views.health),
    path("api/offline/session/", views.session),
    path("api/offline/snapshot/", views.snapshot),
    path("api/offline/snapshot/comercial/", views.commercial_snapshot, name="offline_commercial_snapshot"),
    path("api/offline/observations/", views.synchronize),
    path("api/offline/operations/<uuid:operation_id>/", views.operation_result),
]
