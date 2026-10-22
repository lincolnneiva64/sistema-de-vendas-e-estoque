"""Display-only ordering, independent of database collation and round status."""
from unicodedata import normalize, combining


def chave_produto(item):
    nome = ' '.join(item.cadastro['nome'].split()).casefold()
    nome = ''.join(c for c in normalize('NFD', nome) if not combining(c))
    # Product and inventory item stay the same when a recount creates a new round.
    return nome, item.produto_id, item.pk
