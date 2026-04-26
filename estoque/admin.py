from django.contrib import admin
from .models import Categoria, Produto, Unidade

admin.site.register(Produto)
admin.site.register(Unidade)
admin.site.register(Categoria)
