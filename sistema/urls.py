from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    path('locacoes/', include('locacoes.urls', namespace='locacoes')),
    path('', include('estoque.urls', namespace='estoque')),

    path('admin/', admin.site.urls),
    path('estoque/', include('estoque.urls')),
]
if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.BASE_DIR / "static")
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
