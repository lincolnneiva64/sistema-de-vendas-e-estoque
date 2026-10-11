"""Edicao historica: deltas de estoque, obrigacoes abertas e nenhuma saida de dinheiro."""
import hashlib
import json
from decimal import Decimal
from uuid import uuid4

from django.core import signing
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from estoque.models import (
    Compra, ContaPagar, EdicaoCompra, ItemCompra, LancamentoCartao, ListaCompraFornecedor,
    MovimentoFinanceiro, PagamentoContaPagar, ParcelaCompra, ParcelaNotaListaCompraFornecedor, Produto,
)
from estoque.services.precos_vinculados import bloquear_catalogo

SALT = "estoque.edicao-compra-finalizada.v1"


def _json(valor):
    return json.loads(json.dumps(valor, cls=DjangoJSONEncoder, sort_keys=True))


def _hash(valor):
    return hashlib.sha256(json.dumps(_json(valor), sort_keys=True).encode()).hexdigest()


def estado_compra(compra):
    from estoque.views import _movimentos_financeiros_compra, _lista_origem_compra
    lista = _lista_origem_compra(compra)
    contas = ContaPagar.objects.filter(compra=compra)
    movimentos = MovimentoFinanceiro.objects.filter(
        Q(compra=compra) | Q(pagamento_conta_pagar__conta__compra=compra)
        | Q(pk__in=[m.pk for m in _movimentos_financeiros_compra(compra)])
    ).distinct()
    return _json({
        "compra": Compra.objects.filter(pk=compra.pk).values().get(),
        "itens": list(compra.itens.order_by("pk").values()),
        "contas": list(contas.order_by("pk").values()),
        "pagamentos": list(PagamentoContaPagar.objects.filter(conta__in=contas).order_by("pk").values()),
        "movimentos": list(movimentos.order_by("pk").values()),
        "cartao": list(LancamentoCartao.objects.filter(compra=compra).order_by("pk").values()),
        "parcelas_diretas": list(compra.parcelas_nota.order_by("pk").values()),
        "lista": ListaCompraFornecedor.objects.filter(pk=lista.pk).values().get() if lista else None,
        "parcelas_lista": list(lista.parcelas_nota.order_by("pk").values()) if lista else [],
    })


def _bloqueio_vinculos(compra, estado):
    if estado["lista"] and not compra.lista_fornecedor_id:
        return "Vinculo legado com lista identificado apenas por texto; concilie o vinculo antes de editar."
    if any(not item["produto_id"] for item in estado["itens"]):
        return "Produto removido do cadastro; vinculo dos itens exige revisao manual."
    ativos = [c for c in estado["contas"] if c["status"] != ContaPagar.STATUS_CANCELADA]
    numeros = [c["numero_parcela"] for c in ativos]
    if (len(ativos) > 1 and (None in numeros or sorted(numeros) != list(range(1, len(ativos) + 1)))) or any(
        c["fornecedor_id"] != compra.fornecedor_id or c["data_emissao"] != compra.data_compra.isoformat() for c in ativos
    ):
        return "Vinculos das contas divergem da compra ou possuem numeracao ambigua; conciliacao necessaria."
    return ""


def _tipo_formulario(tipo):
    from estoque.views import _compra_pagamento_a_prazo, _compra_pagamento_imediato, _compra_pagamento_cartao_credito, normalizar_texto_cliente
    if _compra_pagamento_a_prazo(tipo):
        return "aprazo"
    if _compra_pagamento_cartao_credito(tipo):
        return "cartao_credito"
    if _compra_pagamento_imediato(tipo):
        return "cartao_debito" if "debito" in normalizar_texto_cliente(tipo) else "avista"
    return tipo


def bloqueio_financeiro(compra, estado):
    if compra.cancelada or compra.status != Compra.STATUS_FINALIZADA or not compra.estoque_entrada_realizada:
        return "Compra cancelada ou com estado de estoque inconsistente; revisao manual necessaria."
    vinculo = _bloqueio_vinculos(compra, estado)
    if vinculo:
        return vinculo
    if _tipo_formulario(compra.tipo_pagamento) not in {"aprazo", "avista", "cartao_credito", "cartao_debito"}:
        return "Forma de pagamento historica desconhecida; exige conciliacao manual."
    if estado["pagamentos"]:
        return "Existe pagamento registrado, inclusive cancelado. Itens, valores e forma de pagamento exigem conciliacao."
    if estado["movimentos"]:
        return "Existe movimento financeiro ou vinculo legado ambiguo. Registro nao comprova pagamento: correcao de lancamento e devolucao real exigem conciliacao."
    if estado["cartao"] or compra.cartao_id:
        return "Compra vinculada a cartao/fatura; alteracoes financeiras exigem conciliacao."
    if any(c["status"] != ContaPagar.STATUS_CANCELADA and (
        c["status"] != ContaPagar.STATUS_ABERTA or Decimal(c["valor_em_aberto"]) != Decimal(c["valor_original"])
    ) for c in estado["contas"]):
        return "Conta quitada, parcial ou com saldo movimentado; alteracoes financeiras bloqueadas."
    return ""


def contexto_edicao(compra, usuario):
    from estoque.views import _compra_pagamento_a_prazo
    estado = estado_compra(compra)
    contas = list(compra.contas_pagar.exclude(status=ContaPagar.STATUS_CANCELADA).order_by("numero_parcela", "pk"))
    pagamento = None
    if _compra_pagamento_a_prazo(compra.tipo_pagamento):
        total = sum((c.valor_original for c in contas), Decimal("0.00"))
        pagamento = {
            "forma": "varios_boletos" if len(contas) > 1 else "boleto_unico",
            "valor_nota": total, "valor_nota_input": f"{total:.2f}".replace(".", ","),
            "quantidade": len(contas), "observacao": (compra.lista_fornecedor.observacao_pagamento_nota if compra.lista_fornecedor_id else compra.observacao_pagamento_nota),
            "linhas": [{"numero": i, "conta_id": c.pk, "valor": c.valor_original,
                        "valor_input": f"{c.valor_original:.2f}".replace(".", ","),
                        "vencimento": c.data_vencimento.isoformat() if c.data_vencimento else "",
                        "observacao": ""} for i, c in enumerate(contas, 1)],
        }
        origem = compra.lista_fornecedor if compra.lista_fornecedor_id else compra
        por_numero = {p.numero: p for p in origem.parcelas_nota.all()}
        for linha, conta in zip(pagamento["linhas"], contas):
            parcela = por_numero.get(conta.numero_parcela or linha["numero"])
            linha["observacao"] = parcela.observacao if parcela else ""
    motivo = bloqueio_financeiro(compra, estado)
    if _compra_pagamento_a_prazo(compra.tipo_pagamento) and not contas:
        motivo = motivo or "Compra a prazo sem obrigacoes ativas; reconcilie as contas antes de editar."
    return {
        "edicao_finalizada": True, "bloqueio_edicao": motivo,
        "tipo_pagamento_form": _tipo_formulario(compra.tipo_pagamento),
        "edicao_token": signing.dumps({"compra": compra.pk, "usuario": usuario.pk,
                                      "versao": _hash(estado), "operacao": uuid4().hex}, salt=SALT),
        "pagamento_nota_compra": pagamento,
        "movimentos_edicao": estado["movimentos"],
        "contas_financeiras_edicao": list(MovimentoFinanceiro.objects.filter(pk__in=[m["id"] for m in estado["movimentos"]]).select_related("conta")),
    }


def _sincronizar_plano(compra, parcelas, valor, forma, observacao):
    origem = compra.lista_fornecedor if compra.lista_fornecedor_id else compra
    modelo = ParcelaNotaListaCompraFornecedor if compra.lista_fornecedor_id else ParcelaCompra
    vinculo = {"lista": origem} if compra.lista_fornecedor_id else {"compra": compra}
    origem.valor_nota_boleto = valor
    origem.forma_cobranca_nota = forma
    origem.observacao_pagamento_nota = observacao
    origem.save(update_fields=["valor_nota_boleto", "forma_cobranca_nota", "observacao_pagamento_nota", "atualizado_em"])
    existentes = {p.numero: p for p in origem.parcelas_nota.select_for_update()}
    for parcela in parcelas:
        atual = existentes.pop(parcela["numero"], None)
        if atual:
            for campo, novo in parcela.items():
                setattr(atual, campo, novo)
            atual.save(update_fields=["valor", "data_vencimento", "observacao", "atualizado_em"])
        else:
            modelo.objects.create(**vinculo, **parcela)
    for parcela in existentes.values():
        parcela.delete()  # Programacao apenas; contas financeiras retiradas sao canceladas.


def salvar_edicao(request, compra_id):
    from estoque.views import (
        _dados_compra_post, _dados_parcelas_pagamento_nota, _valor_monetario_obrigatorio_lista_fornecedor,
        _compra_pagamento_a_prazo, _compra_pagamento_imediato, _registrar_observacao_compra, _parse_decimal_financeiro,
    )
    try:
        token = signing.loads(request.POST.get("edicao_token", ""), salt=SALT, max_age=7200)
    except signing.BadSignature as exc:
        raise ValueError("A edicao expirou ou possui identificacao invalida. Recarregue a compra.") from exc
    if not isinstance(token, dict) or not {"compra", "usuario", "versao", "operacao"}.issubset(token):
        raise ValueError("Identificacao da edicao invalida.")
    if token["compra"] != compra_id or token["usuario"] != request.user.pk:
        raise ValueError("A identificacao da edicao nao pertence a esta compra ou usuario.")
    motivo = (request.POST.get("motivo_edicao") or "").strip()
    if not motivo:
        raise ValueError("Informe o motivo da correcao.")
    payload_hash = _hash({k: request.POST.getlist(k) for k in request.POST if k not in {"csrfmiddlewaretoken", "edicao_token"}})
    with transaction.atomic(), bloquear_catalogo():
        compra = Compra.objects.select_for_update().get(pk=compra_id)
        anterior = EdicaoCompra.objects.filter(operacao=token["operacao"]).first()
        if anterior:
            if anterior.compra_id != compra_id or anterior.usuario_id != request.user.pk or anterior.payload_hash != payload_hash:
                raise ValueError("Identificacao ja utilizada em outra correcao. Recarregue a compra.")
            return anterior, False
        if compra.cancelada or compra.status != Compra.STATUS_FINALIZADA or not compra.estoque_entrada_realizada:
            raise ValueError("Compra cancelada ou nao finalizada com estoque confirmado. Nenhuma alteracao foi salva.")
        itens = list(ItemCompra.objects.select_for_update().filter(compra=compra).order_by("pk"))
        contas = list(ContaPagar.objects.select_for_update().filter(compra=compra).order_by("numero_parcela", "pk"))
        list(PagamentoContaPagar.objects.select_for_update().filter(conta__in=contas))
        if compra.lista_fornecedor_id:
            compra.lista_fornecedor = ListaCompraFornecedor.objects.select_for_update().get(pk=compra.lista_fornecedor_id)
        antes = estado_compra(compra)
        if _hash(antes) != token["versao"]:
            raise ValueError("A compra, parcelas ou pagamentos mudaram. Recarregue antes de corrigir.")
        if any(k.startswith(("atualizar_custo_", "atualizar_preco_venda_", "novo_preco_venda_")) for k in request.POST):
            raise ValueError("Edicao historica nao atualiza custos do catalogo nem precos de venda.")
        dados = _dados_compra_post(request, exigir_itens=True, compra_edicao=compra)
        if any(not dados[c].is_finite() or abs(dados[c]) >= Decimal("10000000000") for c in ("total", "total_produtos", "ajuste_total")):
            raise ValueError("Total fora do limite monetario permitido.")
        if not (dados["compra_a_prazo"] or dados["compra_a_vista"] or dados["cartao"]):
            raise ValueError("Forma de pagamento desconhecida; exige conciliacao manual.")
        por_id = {item.pk: item for item in itens}
        ids_usados = set()
        planos = []
        deltas = {}
        for item in itens:
            if item.produto_id:
                deltas[item.produto_id] = deltas.get(item.produto_id, Decimal("0")) - item.quantidade
        for linha in dados["itens"]:
            if linha["quantidade"] >= Decimal("1000000000") or linha["preco_unitario"] >= Decimal("10000000000"):
                raise ValueError("Quantidade ou custo fora do limite permitido.")
            item_id = linha.get("item_id")
            existente = None
            if item_id is not None:
                if item_id not in por_id or item_id in ids_usados:
                    raise ValueError("Identificacao de item invalida ou duplicada.")
                existente = por_id[item_id]
                ids_usados.add(item_id)
                if existente.produto_id != linha["produto"].pk and (existente.alteracoes_precos or existente.alteracoes_precos_vinculados):
                    raise ValueError("Item com historico de precos nao pode trocar de produto.")
            else:
                candidatos = [i for i in itens if i.produto_id == linha["produto"].pk and i.pk not in ids_usados]
                if len(candidatos) == 1:
                    existente = candidatos[0]
                    ids_usados.add(existente.pk)
            planos.append((existente, linha))
            pk = linha["produto"].pk
            deltas[pk] = deltas.get(pk, Decimal("0")) + linha["quantidade"]
        removidos = [i for i in itens if i.pk not in ids_usados]
        if any(i.alteracoes_precos or i.alteracoes_precos_vinculados for i in removidos):
            raise ValueError("Item com historico de precos nao pode ser removido por esta edicao.")
        mudou_itens = bool(removidos) or any(i is None or any(getattr(i, campo) != linha[campo] for campo in (
            "quantidade", "preco_unitario", "valor_total", "unidade"
        )) or i.produto_id != linha["produto"].pk for i, linha in planos)
        mudou_base = mudou_itens or any(getattr(compra, campo) != dados[campo] for campo in (
            "data_compra", "total", "total_produtos", "ajuste_total"
        )) or compra.fornecedor_id != dados["fornecedor"].pk or compra.cartao_id != (dados["cartao"].pk if dados["cartao"] else None)
        antigo_prazo = _compra_pagamento_a_prazo(compra.tipo_pagamento)
        mudou_pagamento = _tipo_formulario(compra.tipo_pagamento) != _tipo_formulario(dados["tipo_pagamento"])
        ativos = [c for c in contas if c.status != ContaPagar.STATUS_CANCELADA]
        bloqueio = bloqueio_financeiro(compra, antes)
        if bloqueio and not antigo_prazo and dados["compra_a_prazo"]:
            raise ValueError(bloqueio)
        vinculo_inseguro = _bloqueio_vinculos(compra, antes)
        if vinculo_inseguro:
            raise ValueError(vinculo_inseguro)
        parcelas = []
        ids_contas = []
        valor_nota = None
        forma = ""
        if dados["compra_a_prazo"]:
            forma = request.POST.get("compra_forma_cobranca_nota") or "boleto_unico"
            valor_nota = _valor_monetario_obrigatorio_lista_fornecedor(request.POST.get("compra_valor_nota_boleto") or dados["total"])
            parcelas = _dados_parcelas_pagamento_nota(request, forma, valor_nota, "compra_", dados["data_vencimento"])
            usados = set()
            for parcela in parcelas:
                bruto = request.POST.get(f"compra_conta_id_{parcela['numero']}") or ""
                try:
                    pk = int(bruto) if bruto else None
                except ValueError:
                    raise ValueError("Identificacao de conta invalida.")
                if pk and (pk not in {c.pk for c in ativos} or pk in usados):
                    raise ValueError("Conta a Pagar invalida, cancelada ou duplicada.")
                usados.add(pk)
                ids_contas.append(pk)
            mesmos_ids = set(ids_contas) == {c.pk for c in ativos} and None not in ids_contas
            por_conta = {c.pk: c for c in ativos}
            mudou_valores = not mesmos_ids or any(p["valor"] != por_conta[pk].valor_original for p, pk in zip(parcelas, ids_contas) if pk)
            if bloqueio and ids_contas != [c.pk for c in ativos]:
                raise ValueError("Contas com historico nao podem ser reordenadas ou substituidas.")
            if antigo_prazo and not ativos:
                raise ValueError("Compra a prazo sem obrigacoes ativas; exige reconciliacao.")
            if bloqueio and (mudou_base or mudou_pagamento or mudou_valores):
                raise ValueError(bloqueio)
        elif antigo_prazo and mudou_pagamento:
            raise ValueError("Conversao para pagamento imediato/cartao exige conciliacao e baixa das contas existentes; nenhuma conta foi apagada.")
        elif bloqueio and (mudou_base or mudou_pagamento):
            raise ValueError(bloqueio)
        if dados["cartao"] and mudou_pagamento:
            raise ValueError("Conversao para cartao exige conciliacao especifica.")
        origens = [_parse_decimal_financeiro(request.POST.get(c) or "0") for c in ("origem_caixa", "origem_reserva", "origem_banco")]
        if any(v is None or not v.is_finite() or v != 0 for v in origens) or request.POST.get("conta_financeira_edicao"):
            raise ValueError("Troca de conta financeira ou novo pagamento exige conciliacao; esta edicao nao movimenta dinheiro.")
        produtos = {p.pk: p for p in Produto.objects.select_for_update().filter(pk__in=deltas).order_by("pk")}
        for pk, delta in deltas.items():
            if delta and (pk not in produtos or not Decimal("0") <= (produtos[pk].quantidade or Decimal("0")) + delta < Decimal("1000000000")):
                raise ValueError("A correcao deixaria estoque negativo ou possui produto sem vinculo.")
        for pk, delta in deltas.items():
            if delta:
                Produto.objects.filter(pk=pk).update(quantidade=(produtos[pk].quantidade or Decimal("0")) + delta, atualizado_em=timezone.now())
        for item, linha in planos:
            if item is None:
                item = ItemCompra(compra=compra)
            for campo in ("quantidade", "preco_unitario", "valor_total", "unidade", "observacao"):
                if campo != "observacao" or linha[campo] or item.observacao:
                    setattr(item, campo, linha[campo])
            item.produto = linha["produto"]
            item.save()
        for item in removidos:
            item.delete()
        if parcelas:
            usados = {pk for pk in ids_contas if pk}
            for parcela, pk in zip(parcelas, ids_contas):
                conta = next((c for c in ativos if c.pk == pk), None) if pk else None
                if conta is None:
                    conta = ContaPagar(compra=compra, status=ContaPagar.STATUS_ABERTA, valor_em_aberto=parcela["valor"])
                if not bloqueio:
                    conta.fornecedor = dados["fornecedor"]
                    conta.data_emissao = dados["data_compra"]
                    conta.valor_original = parcela["valor"]
                    conta.valor_em_aberto = parcela["valor"]
                    conta.numero_parcela = parcela["numero"]
                    conta.total_parcelas = len(parcelas)
                conta.data_vencimento = parcela["data_vencimento"]
                conta.save()
            for conta in ativos:
                if conta.pk not in usados:
                    conta.status = ContaPagar.STATUS_CANCELADA
                    conta.observacao = f"{conta.observacao}\nCancelada na edicao {token['operacao']}: {motivo}".strip()
                    conta.save(update_fields=["status", "observacao", "atualizado_em"])
            _sincronizar_plano(compra, parcelas, valor_nota, forma, (request.POST.get("compra_observacao_pagamento_nota") or "").strip())
            if compra.lista_fornecedor_id and not bloqueio and compra.lista_fornecedor.fornecedor_id != dados["fornecedor"].pk:
                compra.lista_fornecedor.fornecedor = dados["fornecedor"]
                compra.lista_fornecedor.save(update_fields=["fornecedor", "atualizado_em"])
        for campo in ("data_compra", "total", "total_produtos", "ajuste_total"):
            setattr(compra, campo, dados[campo])
        compra.fornecedor = dados["fornecedor"]
        if not bloqueio and mudou_pagamento:
            compra.tipo_pagamento = dados["tipo_pagamento"]
        compra.data_vencimento = parcelas[0]["data_vencimento"] if parcelas else compra.data_vencimento
        if dados["observacao"] != (compra.observacao or ""):
            compra.observacao = f"{compra.observacao or ''}\nObservacao informada na edicao: {dados['observacao']}".strip()
        compra.save()
        _registrar_observacao_compra(compra, f"Edicao {token['operacao']} por {request.user.get_username()}. Motivo: {motivo}. Estoque corrigido por diferenca; nenhum movimento financeiro criado.")
        registro = EdicaoCompra.objects.create(compra=compra, operacao=token["operacao"], usuario=request.user,
            motivo=motivo, payload_hash=payload_hash, antes=antes, depois=estado_compra(compra))
        return registro, True
