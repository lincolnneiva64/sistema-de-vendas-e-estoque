import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'sistema.settings')
django.setup()

from estoque.models import Produto
from django.db.models import Count, Q
from datetime import datetime

print("=" * 80)
print("INVESTIGAÇÃO DO BANCO REAL (PostgreSQL)")
print("=" * 80)
print()

# 1. Total de produtos ativos
total_ativos = Produto.objects.filter(excluido=False).count()
print(f"Total de produtos ATIVOS: {total_ativos}")
print()

# 2. Produtos criados em 27/07/2026
from datetime import date
data_27_julho = date(2026, 7, 27)

# Produtos criados exatamente em 27/07
from django.db.models.functions import Cast
from django.db.models import DateField

produtos_27_julho_exatos = Produto.objects.filter(
    excluido=False,
    criado_em__date=data_27_julho
).count()

print(f"Produtos criados EM 27/07/2026: {produtos_27_julho_exatos}")

# Produtos criados entre 27/07 (todo o dia)
produtos_27_julho_range = Produto.objects.filter(
    excluido=False,
    criado_em__date__lte=data_27_julho,
    criado_em__date__gte=data_27_julho
).count()

print(f"  (confirmação): {produtos_27_julho_range}")
print()

# 3. Produtos com codigo_legado preenchido
com_codigo_legado = Produto.objects.filter(
    excluido=False,
    codigo_legado__isnull=False
).exclude(codigo_legado='').count()

print(f"Produtos com 'codigo_legado' preenchido: {com_codigo_legado}")
print()

# 4. Interseção: criado em 27/07/2026 AND codigo_legado preenchido
interseção = Produto.objects.filter(
    excluido=False,
    criado_em__date=data_27_julho,
    codigo_legado__isnull=False
).exclude(codigo_legado='').count()

print(f"Interseção (27/07/2026 AND codigo_legado): {interseção}")
print()

# 5. Distribuição por data de criação (últimas 20 datas) - usando SQL direto
print("=" * 80)
print("DISTRIBUIÇÃO POR DATA DE CRIAÇÃO (top 20)")
print("=" * 80)

from django.db import connection

with connection.cursor() as cursor:
    cursor.execute("""
        SELECT DATE(criado_em)::text as data, COUNT(*) as total
        FROM estoque_produto
        WHERE excluido = false
        GROUP BY DATE(criado_em)
        ORDER BY total DESC
        LIMIT 20
    """)
    
    for row in cursor.fetchall():
        print(f"{row[0]}: {row[1]} produtos")

print()

# 6. Análise de critérios possíveis
print("=" * 80)
print("ANÁLISE DE CRITÉRIOS POSSÍVEIS")
print("=" * 80)
print()

print("CRITÉRIO 1: codigo_legado IS NOT NULL")
criterio1 = Produto.objects.filter(
    excluido=False,
    codigo_legado__isnull=False
).exclude(codigo_legado='').count()
print(f"  Produtos: {criterio1}")
print(f"  Confiabilidade: ⭐⭐⭐⭐⭐ (RECOMENDADO)")
print()

print("CRITÉRIO 2: Criado em 27/07/2026")
criterio2 = Produto.objects.filter(
    excluido=False,
    criado_em__date=data_27_julho
).count()
print(f"  Produtos: {criterio2}")
print(f"  Confiabilidade: ⭐⭐⭐ (PODE QUEBRAR se houver importações em outros dias)")
print()

print("CRITÉRIO 3: Combinado (27/07/2026 AND codigo_legado)")
criterio3 = Produto.objects.filter(
    excluido=False,
    criado_em__date=data_27_julho,
    codigo_legado__isnull=False
).exclude(codigo_legado='').count()
print(f"  Produtos: {criterio3}")
print(f"  Confiabilidade: ⭐⭐⭐⭐ (MAS pode deixar de fora importações em outras datas)")
print()

# 7. Verificar se campo codigo_legado existe na tabela
print("=" * 80)
print("VERIFICAÇÃO: Campo 'codigo_legado' existe?")
print("=" * 80)
try:
    test = Produto.objects.filter(codigo_legado__isnull=False).exists()
    print("✅ SIM - Campo 'codigo_legado' está funcionando")
    print("   Migração 0086_produto_codigo_legado foi aplicada")
except Exception as e:
    print(f"❌ NÃO - Erro: {str(e)}")
    print("   Migração não foi aplicada ainda")

print()

# 8. Estatísticas finais para decisão
print("=" * 80)
print("RECOMENDAÇÃO FINAL")
print("=" * 80)
print()

if com_codigo_legado > 0:
    print("✅ Use CRITÉRIO 1: codigo_legado IS NOT NULL")
    print()
    print(f"   Razão: Detectou {com_codigo_legado} produtos com codigo_legado")
    print("   Esse é o identificador mais confiável de importação")
    print()
    print(f"   Produtos para revisar: {com_codigo_legado}")
else:
    print("⚠️ Nenhum produto com 'codigo_legado' encontrado")
    print(f"   Tentando usar CRITÉRIO 2: Criado em 27/07/2026")
    print(f"   Produtos encontrados: {criterio2}")
