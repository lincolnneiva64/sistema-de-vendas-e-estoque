import sqlite3

db_path = r'c:\Users\linco\Documents\projeto de estoque\sistema-de-vendas-e-estoque\db.sqlite3'
conn = sqlite3.connect(db_path)
cursor = conn.cursor()

# Obter schema da tabela estoque_produto
cursor.execute("PRAGMA table_info(estoque_produto)")
columns = cursor.fetchall()

print("=" * 80)
print("SCHEMA DA TABELA estoque_produto")
print("=" * 80)
print()
print(f"{'ID':<5} {'Nome':<30} {'Tipo':<15} {'Not Null':<10} {'Default':<15} {'PK':<5}")
print("-" * 90)

for col in columns:
    col_id, col_name, col_type, not_null, default, pk = col
    print(f"{col_id:<5} {col_name:<30} {col_type:<15} {str(not_null):<10} {str(default):<15} {str(pk):<5}")

conn.close()
