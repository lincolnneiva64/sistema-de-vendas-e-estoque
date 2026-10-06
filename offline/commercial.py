"""Read-only catalog for stage 2.1. It grants no authority to create a sale."""
from uuid import uuid4

from django.utils import timezone

from estoque.models import Cliente, Funcionario, Produto

SCHEMA_VERSION = 1
SNAPSHOT_TYPE = "comercial_vendas"
REVALIDATE = ["produto_ativo", "cliente_ativo", "preco", "estoque", "conversoes",
              "regras_comerciais", "forma_pagamento", "credito", "operador", "pedido_origem"]


def decimal_text(value, places=2):
    return format(value or 0, f".{places}f")


def build_commercial_snapshot(user, environment, device_id=None):
    # Explicit projection: no financial summaries, model serialization, or writes.
    products = Produto.objects.filter(ativo=True, excluido=False).order_by("nome", "pk").values(
        "id", "nome", "codigo", "unidade_compra", "unidade_venda_1", "unidade_venda_2",
        "vende_fracionado", "fator_conversao", "preco_venda", "preco_vista_fracionado",
        "preco_compra", "preco_compra_fracionado", "quantidade", "estoque_conferido", "preco_conferido",
    )
    products = [{
        "id": str(p["id"]), "nome": p["nome"], "codigo": p["codigo"] or "", "ativo": True,
        "unidade_base": p["unidade_venda_1"] or p["unidade_compra"] or "",
        "unidade_venda_1": p["unidade_venda_1"] or p["unidade_compra"] or "",
        "unidade_venda_2": (p["unidade_venda_2"] or "") if p["vende_fracionado"] else "",
        "vende_fracionado": p["vende_fracionado"], "fator_conversao": decimal_text(p["fator_conversao"]),
        "preco_venda": decimal_text(p["preco_venda"]),
        "preco_venda_fracionado": decimal_text(p["preco_vista_fracionado"]),
        # The current sale screen displays both costs in its read-only Custo field.
        "custo_referencia": decimal_text(p["preco_compra"]),
        "custo_fracionado_referencia": decimal_text(p["preco_compra_fracionado"]),
        "estoque_referencia": decimal_text(p["quantidade"], 3),
        "estoque_conferido": p["estoque_conferido"], "preco_conferido": p["preco_conferido"],
    } for p in products]
    clients = Cliente.objects.filter(ativo=True).order_by("nome", "pk").values(
        "id", "nome", "apelido_nome_conhecido", "whatsapp", "telefone_alternativo", "prazo_padrao_dias",
    )
    clients = [{
        "id": str(c["id"]), "nome": c["nome"], "ativo": True,
        "apelido_nome_conhecido": c["apelido_nome_conhecido"] or "",
        "telefone": c["whatsapp"] or c["telefone_alternativo"] or "",
        "prazo_padrao_dias": c["prazo_padrao_dias"] or 0,
    } for c in clients]
    operators = [{"id": str(o["id"]), "nome": o["nome"]} for o in
                 Funcionario.objects.filter(ativo=True, pode_operar_sistema=True).order_by("nome", "pk").values("id", "nome")]
    return {
        "tipo": SNAPSHOT_TYPE, "schema_version": SCHEMA_VERSION, "snapshot_id": str(uuid4()),
        "gerado_em": timezone.now().isoformat(), "origem": "/vendas/", "environment_id": environment,
        "actor": {"id": str(user.pk), "name": user.get_username()}, "device_id": device_id,
        "contagens": {"clientes": len(clients), "produtos": len(products), "operadores": len(operators)},
        "somente_referencia": True, "revalidar_no_servidor": REVALIDATE,
        "clientes": clients, "produtos": products, "operadores": operators,
        # These are sale types in tipoVenda, not the cash-receipt methods.
        "formas_pagamento": [{"valor": "À vista", "label": "À vista"},
                             {"valor": "A prazo", "label": "A prazo"},
                             {"valor": "consumo_proprio", "label": "Consumo próprio"}],
    }
