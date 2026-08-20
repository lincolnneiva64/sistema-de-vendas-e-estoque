import sqlite3
from datetime import datetime

db_path = r'c:\Users\linco\Documents\projeto de estoque\sistema-de-vendas-e-estoque\db.sqlite3'
conn = sqlite3.connect(db_path)
cursor = conn.cursor()

# 1. QUANTIFICAÇÕES BÁSICAS
cursor.execute('SELECT COUNT(*) FROM estoque_produto')
total_produtos = cursor.fetchone()[0]

cursor.execute('SELECT COUNT(*) FROM estoque_produto WHERE excluido = 0')
produtos_ativos = cursor.fetchone()[0]

cursor.execute('SELECT COUNT(*) FROM estoque_produto WHERE excluido = 1')
produtos_inativos = cursor.fetchone()[0]

# 2. PRODUTOS COM CÓDIGO
cursor.execute('SELECT COUNT(*) FROM estoque_produto WHERE codigo IS NOT NULL AND codigo != ""')
com_codigo = cursor.fetchone()[0]

sem_codigo = total_produtos - com_codigo

# 3. PRODUTOS COM CADASTRO INCOMPLETO
# NOTA: Campo cadastro_incompleto não existe no SQLite atual (migration 0066 não aplicada)
# cursor.execute('SELECT COUNT(*) FROM estoque_produto WHERE cadastro_incompleto = 1')
# cadastro_incompleto = cursor.fetchone()[0]

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
print()
print("NOTA: Campo 'cadastro_incompleto' não existe no banco SQLite atual")
print("      (Migration 0066 ainda não foi aplicada)")
print()
print("=" * 80)
print("DISTRIBUIÇÃO POR DATA DE CRIAÇÃO")
print("=" * 80)

cursor.execute('''
SELECT DATE(criado_em) as data, COUNT(*) as total
FROM estoque_produto
GROUP BY DATE(criado_em)
ORDER BY data DESC
LIMIT 20
''')

print("\nÚltimas 20 datas (mais recentes primeiro):")
for row in cursor.fetchall():
    print(f"{row[0]}: {row[1]} produtos")

# Verificar se há concentração específica
print()
cursor.execute('''
SELECT DATE(criado_em) as data, COUNT(*) as total
FROM estoque_produto
GROUP BY DATE(criado_em)
ORDER BY total DESC
LIMIT 5
''')

print("\nTOP 5 datas com mais produtos criados:")
for row in cursor.fetchall():
    print(f"{row[0]}: {row[1]} produtos")

# Total por mês
print()
print("=" * 80)
print("DISTRIBUIÇÃO POR MÊS")
print("=" * 80)

cursor.execute('''
SELECT strftime('%Y-%m', criado_em) as mes, COUNT(*) as total
FROM estoque_produto
GROUP BY strftime('%Y-%m', criado_em)
ORDER BY mes
''')

for row in cursor.fetchall():
    print(f"{row[0]}: {row[1]} produtos")

conn.close()
