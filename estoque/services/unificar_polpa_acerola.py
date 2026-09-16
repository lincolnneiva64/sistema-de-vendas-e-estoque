from dataclasses import dataclass
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from estoque.models import (
    AjusteItemVendaQuitada,
    ItemCompra,
    ItemListaCompraFornecedor,
    ItemPedido,
    ItemVenda,
    ItemVendaRemovido,
    MovimentacaoEstoqueManual,
    Produto,
    ProdutoFornecedor,
)
from estoque.utils import normalize_product_name


PRODUTO_OPERACIONAL_ID = 277
PRODUTO_DUPLICADO_ID = 1028
NOME_FINAL = "Polpa Acerola 1Kg"
CODIGO_LEGADO_FINAL = "1.13.0065"


class UnificacaoPolpaAcerolaErro(ValueError):
    pass


@dataclass(frozen=True)
class ResultadoUnificacaoPolpaAcerola:
    aplicado: bool
    produto_operacional_id: int
    produto_duplicado_id: int
    fornecedor_ids_operacional: tuple[int, ...]
    fornecedor_ids_duplicado: tuple[int, ...]
    fornecedor_ids_finais: tuple[int, ...]
    relacoes_duplicado: dict[str, int]


def relacoes_produto(produto_id):
    return {
        "itens_venda": ItemVenda.objects.filter(produto_id=produto_id).count(),
        "itens_compra": ItemCompra.objects.filter(produto_id=produto_id).count(),
        "movimentacoes_estoque_manuais": MovimentacaoEstoqueManual.objects.filter(produto_id=produto_id).count(),
        "itens_venda_removidos": ItemVendaRemovido.objects.filter(produto_id=produto_id).count(),
        "ajustes_itens_quitados": AjusteItemVendaQuitada.objects.filter(produto_id=produto_id).count(),
        "itens_pedido": ItemPedido.objects.filter(produto_id=produto_id).count(),
        "itens_lista_compra_fornecedor": ItemListaCompraFornecedor.objects.filter(produto_id=produto_id).count(),
    }


def _validar_produtos(produto_operacional, produto_duplicado):
    if produto_operacional.pk != PRODUTO_OPERACIONAL_ID:
        raise UnificacaoPolpaAcerolaErro("Produto operacional inesperado.")
    if produto_duplicado.pk != PRODUTO_DUPLICADO_ID:
        raise UnificacaoPolpaAcerolaErro("Produto duplicado inesperado.")
    if not produto_operacional.ativo or produto_operacional.excluido:
        raise UnificacaoPolpaAcerolaErro("Produto 277 nao esta operacional.")
    if produto_duplicado.ativo:
        raise UnificacaoPolpaAcerolaErro("Produto 1028 nao esta inativo.")
    if normalize_product_name(produto_operacional.nome).casefold() not in {
        normalize_product_name("Polpa Acerola 1Kg1").casefold(),
        normalize_product_name(NOME_FINAL).casefold(),
    }:
        raise UnificacaoPolpaAcerolaErro("Nome atual do produto 277 nao corresponde ao caso esperado.")
    if normalize_product_name(produto_duplicado.nome).casefold() != normalize_product_name(NOME_FINAL).casefold():
        raise UnificacaoPolpaAcerolaErro("Nome atual do produto 1028 nao corresponde ao caso esperado.")
    if produto_duplicado.codigo_legado and produto_duplicado.codigo_legado != CODIGO_LEGADO_FINAL:
        raise UnificacaoPolpaAcerolaErro("Codigo legado do produto 1028 nao corresponde ao caso esperado.")
    if produto_operacional.codigo_legado and produto_operacional.codigo_legado != CODIGO_LEGADO_FINAL:
        raise UnificacaoPolpaAcerolaErro("Produto 277 ja possui outro codigo legado.")
    if produto_operacional.quantidade != Decimal("5.000"):
        raise UnificacaoPolpaAcerolaErro("Estoque do produto 277 divergiu do esperado.")
    if produto_operacional.preco_compra != Decimal("9.50"):
        raise UnificacaoPolpaAcerolaErro("Preco de compra do produto 277 divergiu do esperado.")
    if produto_operacional.preco_vista != Decimal("15.00"):
        raise UnificacaoPolpaAcerolaErro("Preco a vista do produto 277 divergiu do esperado.")
    if produto_operacional.preco_prazo != Decimal("16.00"):
        raise UnificacaoPolpaAcerolaErro("Preco a prazo do produto 277 divergiu do esperado.")


def _validar_sem_historico_transacional(produto_id):
    relacoes = relacoes_produto(produto_id)
    com_historico = {nome: total for nome, total in relacoes.items() if total}
    if com_historico:
        raise UnificacaoPolpaAcerolaErro(
            f"Produto {produto_id} possui historico transacional: {com_historico}"
        )
    return relacoes


def unificar_polpa_acerola(aplicar=False):
    with transaction.atomic():
        produtos = {
            produto.pk: produto
            for produto in Produto.objects.select_for_update().filter(
                pk__in=[PRODUTO_OPERACIONAL_ID, PRODUTO_DUPLICADO_ID]
            )
        }
        produto_operacional = produtos.get(PRODUTO_OPERACIONAL_ID)
        produto_duplicado = produtos.get(PRODUTO_DUPLICADO_ID)
        if not produto_operacional or not produto_duplicado:
            raise UnificacaoPolpaAcerolaErro("Produtos 277 e 1028 precisam existir.")

        _validar_produtos(produto_operacional, produto_duplicado)
        relacoes_duplicado = _validar_sem_historico_transacional(PRODUTO_DUPLICADO_ID)

        conflito_codigo = (
            Produto.objects.select_for_update()
            .filter(codigo_legado=CODIGO_LEGADO_FINAL)
            .exclude(pk__in=[PRODUTO_OPERACIONAL_ID, PRODUTO_DUPLICADO_ID])
            .first()
        )
        if conflito_codigo:
            raise UnificacaoPolpaAcerolaErro(
                f"Codigo legado ja esta em outro produto: {conflito_codigo.pk}."
            )

        conflito_nome = (
            Produto.objects.select_for_update()
            .filter(excluido=False)
            .exclude(pk__in=[PRODUTO_OPERACIONAL_ID, PRODUTO_DUPLICADO_ID])
            .only("id", "nome")
        )
        nome_final_key = normalize_product_name(NOME_FINAL).casefold()
        for produto in conflito_nome:
            if normalize_product_name(produto.nome).casefold() == nome_final_key:
                raise UnificacaoPolpaAcerolaErro(
                    f"Nome final ja esta em outro produto ativo/logico: {produto.pk}."
                )

        fornecedores_operacional = set(
            ProdutoFornecedor.objects.select_for_update()
            .filter(produto=produto_operacional, ativo=True)
            .values_list("fornecedor_id", flat=True)
        )
        fornecedores_duplicado = set(
            ProdutoFornecedor.objects.select_for_update()
            .filter(produto=produto_duplicado, ativo=True)
            .values_list("fornecedor_id", flat=True)
        )
        fornecedores_finais = fornecedores_operacional | fornecedores_duplicado

        if aplicar:
            agora = timezone.now()
            produto_duplicado.codigo_legado = None
            produto_duplicado.excluido = True
            produto_duplicado.excluido_em = produto_duplicado.excluido_em or agora
            produto_duplicado.save(update_fields=["codigo_legado", "excluido", "excluido_em", "atualizado_em"])

            produto_operacional.nome = NOME_FINAL
            produto_operacional.codigo_legado = CODIGO_LEGADO_FINAL
            produto_operacional.save(update_fields=["nome", "codigo_legado", "atualizado_em"])

            for fornecedor_id in fornecedores_finais:
                ProdutoFornecedor.objects.update_or_create(
                    produto=produto_operacional,
                    fornecedor_id=fornecedor_id,
                    defaults={"ativo": True},
                )

        return ResultadoUnificacaoPolpaAcerola(
            aplicado=aplicar,
            produto_operacional_id=produto_operacional.pk,
            produto_duplicado_id=produto_duplicado.pk,
            fornecedor_ids_operacional=tuple(sorted(fornecedores_operacional)),
            fornecedor_ids_duplicado=tuple(sorted(fornecedores_duplicado)),
            fornecedor_ids_finais=tuple(sorted(fornecedores_finais)),
            relacoes_duplicado=relacoes_duplicado,
        )
