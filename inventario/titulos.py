"""Resolve generic historical labels from the original selection, without writes."""
from estoque.models import Fornecedor


GENERICOS = {'', 'fornecedor', 'categoria', 'manual', 'produtos selecionados',
             'seleção manual', 'conferência', 'conferência de estoque'}


def fornecedor_original(inventario):
    filtro = inventario.filtro
    if (inventario.criterio != 'fornecedor' or not isinstance(filtro, dict)
            or set(filtro) != {'fornecedor_id'}):
        return None
    valor = filtro['fornecedor_id']
    if type(valor) not in (str, int):
        return None
    texto = str(valor)
    if len(texto) > 19 or not texto.isascii() or not texto.isdigit() or not 0 < int(texto) < 2**63:
        return None
    return int(texto)


def preparar_titulos(inventarios):
    genericos = [i for i in inventarios if str(i.titulo or '').strip().casefold()
                 in GENERICOS | {f'conferência nº {i.pk}'}]
    ids = {fornecedor_original(i) for i in genericos} - {None}
    nomes = dict(Fornecedor.objects.filter(pk__in=ids).values_list('pk', 'nome')) if ids else {}
    for inventario in inventarios:
        inventario.titulo_exibicao = inventario.titulo
        inventario.aviso_titulo = ''
    for inventario in genericos:
        filtro = inventario.filtro if isinstance(inventario.filtro, dict) else {}
        descricao = None
        if inventario.criterio == 'fornecedor':
            nome = nomes.get(fornecedor_original(inventario), '')
            descricao = nome.strip() or 'Fornecedor não identificado'
            if not nome.strip():
                inventario.aviso_titulo = 'A seleção original não identifica um fornecedor único disponível. Nenhum nome foi presumido.'
        elif inventario.criterio == 'categoria':
            categoria = filtro.get('categoria')
            if set(filtro) == {'categoria'} and isinstance(categoria, str) and categoria.strip():
                descricao = categoria.strip()
            else:
                descricao = 'Categoria não identificada'
                inventario.aviso_titulo = 'A seleção original não identifica uma categoria única. Nenhum nome foi presumido.'
        elif inventario.criterio == 'manual':
            descricao = 'Produtos selecionados'
        if descricao:
            inventario.titulo_exibicao = f'Conferência nº {inventario.pk} — {descricao}'
