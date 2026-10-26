"""Resolve fornecedores para leitura dos cards, com duas consultas em lote."""
from estoque.models import Compra, ItemCompra, ProdutoFornecedor


def preparar_fornecedores_cards(produtos):
    produtos = list(produtos)
    ids = [produto.pk for produto in produtos]
    if not ids:
        return produtos
    por_produto = {pk: {} for pk in ids}
    for produto_id, fornecedor_id, nome, ativo, cadastro_ativo in (
        ProdutoFornecedor.objects.filter(produto_id__in=ids).order_by()
        .values_list("produto_id", "fornecedor_id", "fornecedor__nome", "ativo", "fornecedor__ativo")
    ):
        por_produto[produto_id][fornecedor_id] = {
            "id": fornecedor_id, "nome": nome,
            "origem": "Vinculado" if ativo else "Vínculo inativo",
            "prioridade": 0 if ativo else 1, "cadastro_ativo": cadastro_ativo,
        }
    # Mesmo critério do histórico existente de compras: inclui abertas legadas,
    # mas nunca rascunhos, canceladas ou finalizações em andamento. Sem limite.
    historico = (
        ItemCompra.objects.filter(produto_id__in=ids, compra__cancelada=False,
                                  compra__fornecedor_id__isnull=False)
        .exclude(compra__status__in=[Compra.STATUS_CANCELADA, Compra.STATUS_RASCUNHO,
                                    Compra.STATUS_FINALIZACAO_INICIADA])
        .order_by().values_list("produto_id", "compra__fornecedor_id",
                               "compra__fornecedor__nome", "compra__fornecedor__ativo").distinct()
    )
    for produto_id, fornecedor_id, nome, cadastro_ativo in historico:
        por_produto[produto_id].setdefault(fornecedor_id, {
            "id": fornecedor_id, "nome": nome, "origem": "Histórico",
            "prioridade": 2, "cadastro_ativo": cadastro_ativo,
        })
    for produto in produtos:
        itens = sorted(por_produto[produto.pk].values(),
                       key=lambda item: (item["prioridade"], item["nome"].casefold(), item["id"]))
        legado = (produto.fornecedor or "").strip()
        # Texto não vira identidade nem entra na contagem. Correspondência textual
        # exata serve apenas para evitar repetir a mesma informação na apresentação.
        if any(item["nome"].strip().casefold() == legado.casefold() for item in itens):
            legado = ""
        if len(itens) > 1:
            resumo = f"{len(itens)} fornecedores"
        elif itens:
            item = itens[0]
            resumo = f'Fornecedor: {item["nome"]}'
            if item["origem"] != "Vinculado":
                resumo += f' · {item["origem"]}'
            if not item["cadastro_ativo"]:
                resumo += " · Cadastro inativo"
        else:
            resumo = f"Fornecedor: {legado} · Cadastro" if legado else "Fornecedor não informado"
        produto.fornecedores_card = {
            "itens": itens, "legado": legado, "resumo": resumo,
            "expansivel": len(itens) > 1 or bool(itens and legado),
        }
    return produtos
