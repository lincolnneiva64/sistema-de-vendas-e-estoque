from estoque.models import Produto
from django.db.models import Count, Q

# 1. QUANTIFICAÇÕES BÁSICAS
total_produtos = Produto.objects.all().count()
produtos_ativos = Produto.objects.filter(excluido=False).count()
produtos_inativos = Produto.objects.filter(excluido=True).count()

# 2. PRODUTOS COM CÓDIGO
com_codigo = Produto.objects.filter(codigo__isnull=False).exclude(codigo='').count()
sem_codigo = Produto.objects.filter(Q(codigo__isnull=True) | Q(codigo='')).count()

# 3. PRODUTOS COM CÓDIGO LEGADO (forte indicador de importação)
com_codigo_legado = Produto.objects.filter(codigo_legado__isnull=False).exclude(codigo_legado='').count()
sem_codigo_legado = Produto.objects.filter(Q(codigo_legado__isnull=True) | Q(codigo_legado='')).count()

# 4. PRODUTOS COM CADASTRO INCOMPLETO
cadastro_incompleto = Produto.objects.filter(cadastro_incompleto=True).count()

# 5. DISTRIBUIÇÃO POR DATA DE CRIAÇÃO
produtos_por_data = Produto.objects.extra(
    select={'data': 'DATE(criado_em)'}
).values('data').annotate(total=Count('id')).order_by('data')

print("=" * 80)
print("QUANTIFICAÇÃO BÁSICA DE PRODUTOS")
print("=" * 80)
print(f"Total de produtos: {total_produtos}")
print(f"  - Ativos: {produtos_ativos}")
print(f"  - Inativos (excluído=True): {produtos_inativos}")
print()
print("CÓDIGOS")
print(f"  - Com código: {com_codigo}")
print(f"  - Sem código: {sem_codigo}")
print(f"  - Com código_legado: {com_codigo_legado}")
print(f"  - Sem código_legado: {sem_codigo_legado}")
print()
print(f"Cadastro incompleto: {cadastro_incompleto}")
print()
print("=" * 80)
print("DISTRIBUIÇÃO POR DATA DE CRIAÇÃO")
print("=" * 80)

for row in produtos_por_data:
    print(f"{row['data']}: {row['total']} produtos")
