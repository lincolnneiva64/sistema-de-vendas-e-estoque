"""Origem das compras para consultas, sem alterar dados ou regras de gravação."""
import re

from django.db.models import Q

from estoque.models import Compra, ListaCompraFornecedor


_MARCADOR_ORIGEM = re.compile(
    r"^\s*Gerada a partir da Lista de Compras #([1-9]\d*)\b", re.IGNORECASE | re.MULTILINE
)


def _id_origem_registrada(compra):
    if compra.lista_fornecedor_id:
        return compra.lista_fornecedor_id
    origens = {int(marcador.group(1)) for marcador in _MARCADOR_ORIGEM.finditer(compra.observacao or "")}
    return origens.pop() if len(origens) == 1 else None


def anotar_origens_compras(compras):
    compras = list(compras)
    origens = {compra.pk: _id_origem_registrada(compra) for compra in compras}
    ids = {origem for origem in origens.values() if origem}
    existentes = set(ListaCompraFornecedor.objects.filter(pk__in=ids).values_list("pk", flat=True))
    for compra in compras:
        origem = origens[compra.pk]
        compra.origem_lista_id = origem if origem in existentes else None
    return compras


def anotar_compras_listas_desktop(listas):
    listas = list(listas)
    ids = {lista.pk for lista in listas}
    diretas, historicas = {}, {}
    if ids:
        compras = Compra.objects.filter(
            Q(lista_fornecedor_id__in=ids)
            | Q(lista_fornecedor__isnull=True, observacao__icontains="Gerada a partir da Lista de Compras #")
        ).order_by("-id")
        for compra in compras:
            origem = _id_origem_registrada(compra)
            if origem in ids:
                destino = diretas if compra.lista_fornecedor_id else historicas
                destino.setdefault(origem, compra)
    for lista in listas:
        lista.compra_gerada = diretas.get(lista.pk) or historicas.get(lista.pk)
    return listas
