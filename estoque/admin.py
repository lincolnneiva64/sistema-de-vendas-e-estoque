from django.contrib import admin
from .models import Categoria, Funcionario, ItemVenda, Produto, Unidade, Venda

admin.site.register(Produto)
admin.site.register(Unidade)
admin.site.register(Categoria)
admin.site.register(Funcionario)
admin.site.register(Venda)
admin.site.register(ItemVenda)
