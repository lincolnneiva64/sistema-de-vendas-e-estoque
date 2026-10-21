from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.contrib.auth.views import LoginView, LogoutView

urlpatterns = [
    path('entrar/', LoginView.as_view(template_name='offline/login.html', next_page='estoque:home'), name='sistema_login'),
    path('sair/', LogoutView.as_view(next_page='sistema_login'), name='sistema_logout'),
    path('inventario/', include('inventario.urls', namespace='inventario')),
    path('', include('offline.urls')),
    path('locacoes/', include('locacoes.urls', namespace='locacoes')),
    path('', include('estoque.urls', namespace='estoque')),

    path('admin/', admin.site.urls),
    path('estoque/', include('estoque.urls')),
]
if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.BASE_DIR / "static")
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
