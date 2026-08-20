import sqlite3

# Tentar analisar os dois bancos mais interessantes
databases = [
    (r'c:\Users\linco\Documents\projeto de estoque\sistema-de-vendas-e-estoque\db.sqlite3', 'db.sqlite3 (atual)'),
    (r'c:\Users\linco\Documents\projeto de estoque\sistema-de-vendas-e-estoque\db_antigo_desktop.sqlite3', 'db_antigo_desktop.sqlite3'),
]

for db_path, db_name in databases:
    print()
    print("=" * 80)
    print(f"ANALISANDO: {db_name}")
    print("=" * 80)
    
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Verificar se tabela estoque_produto existe
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='estoque_produto'")
        if not cursor.fetchone():
            print("Tabela 'estoque_produto' não encontrada neste banco")
            conn.close()
            continue
        
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
        
        print(f"Total de produtos: {total_produtos}")
        print(f"  - Ativos: {produtos_ativos}")
        print(f"  - Inativos: {produtos_inativos}")
        print()
        print(f"Códigos:")
        print(f"  - Com código: {com_codigo}")
        print(f"  - Sem código: {sem_codigo}")
        print()
        
        # Distribuição por data
        cursor.execute('''
        SELECT DATE(criado_em) as data, COUNT(*) as total
        FROM estoque_produto
        GROUP BY DATE(criado_em)
        ORDER BY total DESC
        LIMIT 5
        ''')
        
        print("TOP 5 datas com mais produtos:")
        for row in cursor.fetchall():
            print(f"  {row[0]}: {row[1]} produtos")
        
        conn.close()
        
    except Exception as e:
        print(f"ERRO: {str(e)}")
