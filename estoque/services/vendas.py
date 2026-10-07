"""Regra oficial de gravação reutilizável sem requisição HTTP.

Ainda não há idempotência por operação offline (etapa 2.5).
"""
from dataclasses import dataclass
from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date

from ..models import Cliente, ItemVenda, ItemVendaRemovido, Produto, SeparacaoVenda, Venda


class ErroGravarVenda(Exception):
    """Rejeição de negócio; o adaptador decide como apresentá-la."""

    def __init__(self, mensagem, status=400, *, extras=None):
        super().__init__(mensagem)
        self.mensagem = mensagem
        self.status = status
        self.extras = dict(extras or {})


@dataclass
class ResultadoVenda:
    venda: Venda
    mensagem: str
    edicao: bool
    produtos_estoque_atualizados_ids: set[int]


def criar_ou_atualizar_venda(*, usuario, dados):
    """Valida dados estruturados e grava nova venda ou edição atomicamente.

    `usuario` é o ator usado na separação; `dados["operador"]` mantém a autoria
    comercial do contrato atual. Os auxiliares legados são reutilizados por
    imports locais para evitar ciclo na carga dos módulos, sem duplicar regras.
    Erros inesperados propagam; rejeições mantêm status/mensagem existentes.
    """
    from ..views import (
        _abrir_conta_receber_conversao_venda_a_prazo,
        _ajustar_movimentos_edicao_venda_a_vista,
        _ajustes_separacao_foram_aplicados,
        _atualizar_saldo_pendente_pedido,
        _baixar_estoque_movimentos,
        _baixar_estoque_produto,
        _contexto_venda_quitada,
        _custo_produto_para_unidade_venda,
        _decimal_do_front,
        _devolver_estoque_item_removido,
        _devolver_estoque_produto,
        _estornar_movimentos_venda_a_vista_para_prazo,
        _formatar_moeda,
        _formatar_quantidade,
        _montar_payload_ajuste_separacao_venda,
        _normalizar_tipo_pagamento_venda,
        _normalizar_unidade_estoque,
        _quantidade_decimal_estoque,
        _quantidade_estoque_inteira,
        _quantidade_estoque_para_unidade_base,
        _recalcular_status_separacao_da_venda,
        _recalcular_total_venda_pelos_itens,
        _registrar_evento_venda,
        _registrar_movimento_venda_a_vista,
        _registrar_movimentos_venda_a_vista,
        _regularizar_conta_receber_conversao_venda_a_vista,
        _sincronizar_conta_receber,
        _sincronizar_despesas_consumo_proprio,
        _snapshot_custo_item_venda,
        _snapshot_custo_item_venda_existente,
        _snapshot_item_venda_quantidade_atual,
        _tipo_pagamento_a_prazo_texto,
        _tipo_pagamento_consumo_proprio_texto,
        _validar_itens_edicao_inalterados,
        _validar_origem_venda_a_vista,
        _validar_quantidade_produto_unidade,
        _valores_origem_venda_post,
        _venda_consumo_proprio,
        _venda_pagamento_imediato,
    )

    itens = dados.get("itens") or []

    venda_edicao_id = str(dados.get("venda_id") or "").strip()
    venda_em_edicao = None
    if venda_edicao_id:
        if not venda_edicao_id.isdigit():
            raise ErroGravarVenda("Venda informada para edicao e invalida.")

        venda_em_edicao = Venda.objects.filter(
            pk=int(venda_edicao_id),
            cancelada=False,
        ).first()

        if not venda_em_edicao:
            raise ErroGravarVenda("Venda informada para edicao nao foi encontrada.", status=404)

    ajuste_separacao_id = str(dados.get("ajuste_separacao_id") or "").strip()
    separacao_ajuste_edicao = None

    if ajuste_separacao_id:
        if not venda_em_edicao or not ajuste_separacao_id.isdigit():
            raise ErroGravarVenda("Separacao informada para ajuste e invalida.")

        separacao_ajuste_edicao = SeparacaoVenda.objects.filter(
            pk=int(ajuste_separacao_id),
            venda=venda_em_edicao,
        ).first()

        if not separacao_ajuste_edicao:
            raise ErroGravarVenda(
                "Separacao informada para ajuste nao pertence a esta venda.",
                status=404,
            )

    if not itens:
        raise ErroGravarVenda("Inclua pelo menos 1 item antes de gravar a venda.")

    data_venda = parse_date(dados.get("data_venda") or "")
    if not data_venda:
        raise ErroGravarVenda("Informe uma data valida para a venda.")

    data_vencimento = (
        parse_date(dados.get("data_vencimento") or "")
        if dados.get("data_vencimento")
        else None
    )
    cliente = None
    cliente_id = dados.get("cliente_id")

    if cliente_id:
        cliente = Cliente.objects.filter(pk=cliente_id, ativo=True).first()
        if not cliente:
            raise ErroGravarVenda("Cliente selecionado nao foi encontrado.")

    itens_validados = []
    total_calculado = Decimal("0.00")

    for item in itens:
        produto_nome = str(item.get("produto_nome") or "").strip()
        produto_id = str(item.get("produto_id") or "").strip()
        unidade = str(item.get("unidade") or "").strip()

        if not produto_nome:
            raise ErroGravarVenda("Existe item sem produto informado.")

        try:
            quantidade = _decimal_do_front(item.get("quantidade"), "0.001")
            preco_unitario = _decimal_do_front(item.get("preco_unitario"), "0.01")
        except ValueError:
            raise ErroGravarVenda(f"Revise quantidade e preco do item {produto_nome}.")

        if quantidade <= 0 or preco_unitario <= 0:
            raise ErroGravarVenda(f"Quantidade e preco precisam ser maiores que zero em {produto_nome}.")

        valor_total = (quantidade * preco_unitario).quantize(Decimal("0.01"))
        item_id = str(item.get("item_id") or "").strip()

        produto = None

        # Na edicao, um item antigo pode continuar usando um produto que
        # foi desativado depois da venda. Se o produto estiver sendo trocado
        # ou se for um item novo, continua obrigatorio usar produto ativo.
        item_existente_edicao = None
        if venda_em_edicao and item_id.isdigit():
            item_existente_edicao = (
                ItemVenda.objects
                .select_related("produto")
                .filter(pk=int(item_id), venda=venda_em_edicao)
                .first()
            )

        if (
            item_existente_edicao
            and produto_id.isdigit()
            and item_existente_edicao.produto_id == int(produto_id)
        ):
            produto = item_existente_edicao.produto

        if not produto and produto_id.isdigit():
            produto = Produto.objects.filter(
                pk=int(produto_id),
                excluido=False,
                ativo=True,
            ).first()

        if not produto:
            produto = Produto.objects.filter(
                nome__iexact=produto_nome,
                excluido=False,
                ativo=True,
            ).first()

        if not produto:
            raise ErroGravarVenda(f'Produto "{produto_nome}" nao foi encontrado no estoque.')
        custo_venda = _custo_produto_para_unidade_venda(produto, unidade)
        if custo_venda > 0 and preco_unitario < custo_venda:
            raise ErroGravarVenda(
                f"Preco de venda abaixo do custo. Custo: {_formatar_moeda(custo_venda)}."
            )
        print(
            "[venda item recebido]",
            f"produto_nome={produto_nome}",
            f"produto_id_recebido={produto_id or '(vazio)'}",
            f"produto_resolvido_id={produto.pk}",
            f"produto_resolvido_nome={produto.nome}",
            f"unidade={unidade}",
            f"quantidade={quantidade}",
            f"estoque_cadastro={produto.quantidade}",
        )
        total_calculado += valor_total

        itens_validados.append({
            "item_id": int(item_id) if item_id.isdigit() else None,
            "produto": produto,
            "quantidade": quantidade,
            "unidade": unidade,
            "preco_unitario": preco_unitario,
            "valor_total": valor_total,
        })

    tipo_pagamento_venda = _normalizar_tipo_pagamento_venda(dados.get("tipo_pagamento"))
    if venda_em_edicao:
        venda_edicao_consumo_proprio = _venda_consumo_proprio(venda_em_edicao)
        pagamento_novo_consumo_proprio = _tipo_pagamento_consumo_proprio_texto(tipo_pagamento_venda)
        if not venda_edicao_consumo_proprio and pagamento_novo_consumo_proprio:
            raise ErroGravarVenda(
                (
                    "Nao e permitido alterar uma venda normal para consumo proprio. "
                    "Cancele a operacao e registre uma nova venda como consumo proprio."
                ),
                status=409,
            )
        if venda_edicao_consumo_proprio and not pagamento_novo_consumo_proprio:
            raise ErroGravarVenda(
                "Nao e permitido alterar uma venda de consumo proprio para uma forma de pagamento normal.",
                status=409,
            )

    valores_origem_venda = None
    if (
        _venda_pagamento_imediato(tipo_pagamento_venda)
        and "origem_recebimento" in dados
    ):
        try:
            valores_origem_venda = _valores_origem_venda_post(dados)
            if not (
                venda_em_edicao
                and _venda_pagamento_imediato(venda_em_edicao.tipo_pagamento)
                and _venda_pagamento_imediato(tipo_pagamento_venda)
            ):
                _validar_origem_venda_a_vista(
                    valores_origem_venda,
                    total_calculado.quantize(Decimal("0.01")),
                )
        except ValueError as exc:
            raise ErroGravarVenda(str(exc))

    # ============================================================
    # EDICAO UNIFICADA DE VENDA EXISTENTE
    # ============================================================
    if venda_em_edicao:
        produtos_estoque_atualizados_ids = set()
        pagamento_antigo_imediato_pre = _venda_pagamento_imediato(venda_em_edicao.tipo_pagamento)
        pagamento_novo_imediato_pre = _venda_pagamento_imediato(tipo_pagamento_venda)
        conversao_vista_para_prazo_pre = (
            pagamento_antigo_imediato_pre
            and not pagamento_novo_imediato_pre
            and _tipo_pagamento_a_prazo_texto(tipo_pagamento_venda)
        )
        contexto_quitada = _contexto_venda_quitada(venda_em_edicao)
        edicao_vista_para_vista_pre = (
            pagamento_antigo_imediato_pre
            and pagamento_novo_imediato_pre
        )
        edicao_consumo_proprio_pre = (
            _venda_consumo_proprio(venda_em_edicao)
            and _tipo_pagamento_consumo_proprio_texto(tipo_pagamento_venda)
        )
        if (
            edicao_vista_para_vista_pre
            and valores_origem_venda is not None
            and total_calculado.quantize(Decimal("0.01"))
            == (venda_em_edicao.total or Decimal("0.00")).quantize(Decimal("0.01"))
        ):
            raise ErroGravarVenda(
                (
                    "Esta venda ja possui pagamento/baixa financeira e nao pode "
                    "receber novamente a mesma origem financeira."
                ),
                status=409,
            )

        if (
            contexto_quitada.get("quitada")
            and not conversao_vista_para_prazo_pre
            and not edicao_vista_para_vista_pre
            and not edicao_consumo_proprio_pre
        ):
            raise ErroGravarVenda(
                (
                    "Esta venda ja possui pagamento/baixa financeira e nao pode ser "
                    "alterada por esta edicao simplificada."
                ),
                status=409,
            )

        try:
            with transaction.atomic():
                venda = (
                    Venda.objects
                    .select_for_update()
                    .get(pk=venda_em_edicao.pk, cancelada=False)
                )

                payload_ajuste_separacao = None
                if separacao_ajuste_edicao:
                    separacao_ajuste_edicao = (
                        SeparacaoVenda.objects
                        .select_for_update()
                        .get(pk=separacao_ajuste_edicao.pk, venda=venda)
                    )
                    payload_ajuste_separacao = _montar_payload_ajuste_separacao_venda(
                        separacao_ajuste_edicao
                    )

                pagamento_antigo_imediato = _venda_pagamento_imediato(venda.tipo_pagamento)
                pagamento_novo_imediato = _venda_pagamento_imediato(tipo_pagamento_venda)
                conversao_prazo_para_vista = (
                    not pagamento_antigo_imediato
                    and pagamento_novo_imediato
                )
                conversao_vista_para_prazo = (
                    pagamento_antigo_imediato
                    and not pagamento_novo_imediato
                    and _tipo_pagamento_a_prazo_texto(tipo_pagamento_venda)
                )
                contexto_quitada_atual = _contexto_venda_quitada(venda)
                edicao_vista_para_vista = (
                    pagamento_antigo_imediato
                    and pagamento_novo_imediato
                )
                edicao_consumo_proprio = (
                    _venda_consumo_proprio(venda)
                    and _tipo_pagamento_consumo_proprio_texto(tipo_pagamento_venda)
                )
                if (
                    contexto_quitada_atual.get("quitada")
                    and not conversao_vista_para_prazo
                    and not edicao_vista_para_vista
                    and not edicao_consumo_proprio
                ):
                    raise ValueError(
                        "Esta venda ja possui pagamento/baixa financeira e nao pode ser alterada por esta edicao simplificada."
                    )

                if conversao_prazo_para_vista and valores_origem_venda is None:
                    raise ValueError(
                        "Informe a origem do recebimento para converter a venda para A vista."
                    )
                if conversao_vista_para_prazo:
                    if not data_vencimento:
                        raise ValueError("Informe o vencimento para converter a venda para A prazo.")
                    _validar_itens_edicao_inalterados(venda, itens_validados)

                itens_atuais = list(
                    ItemVenda.objects
                    .select_for_update()
                    .filter(venda=venda)
                    .order_by("id")
                )
                itens_atuais_por_id = {item.id: item for item in itens_atuais}
                ids_recebidos = set()

                # Impede item_id de outra venda e IDs duplicados no payload.
                for item_novo in itens_validados:
                    item_id = item_novo.get("item_id")
                    if not item_id:
                        continue

                    if item_id in ids_recebidos:
                        raise ValueError(
                            f"Item #{item_id} foi enviado mais de uma vez na edicao."
                        )

                    if item_id not in itens_atuais_por_id:
                        raise ValueError(
                            f"Item #{item_id} nao pertence a esta venda."
                        )

                    ids_recebidos.add(item_id)

                total_anterior = (venda.total or Decimal("0.00")).quantize(Decimal("0.01"))
                cliente_anterior_id = venda.cliente_id
                data_anterior = venda.data_venda
                vencimento_anterior = venda.data_vencimento
                pagamento_anterior = venda.tipo_pagamento or ""
                operador_anterior = venda.operador or ""

                # ------------------------------------------------------------
                # Itens existentes: atualizar quantidade/preco/produto/unidade.
                # ------------------------------------------------------------
                for item_novo in itens_validados:
                    item_id = item_novo.get("item_id")
                    if not item_id:
                        continue

                    item_antigo = itens_atuais_por_id[item_id]
                    produto_antigo = item_antigo.produto
                    produto_novo = item_novo["produto"]

                    quantidade_antiga = Decimal(item_antigo.quantidade or "0").quantize(Decimal("0.001"))
                    quantidade_nova = Decimal(item_novo["quantidade"] or "0").quantize(Decimal("0.001"))

                    unidade_antiga = item_antigo.unidade or ""
                    unidade_nova = item_novo["unidade"] or ""

                    preco_antigo = Decimal(item_antigo.preco_unitario or "0").quantize(Decimal("0.01"))
                    preco_novo = Decimal(item_novo["preco_unitario"] or "0").quantize(Decimal("0.01"))

                    produto_nome_antigo = (
                        produto_antigo.nome if produto_antigo else "Produto nao identificado"
                    )
                    produto_nome_novo = produto_novo.nome

                    produto_mudou = item_antigo.produto_id != produto_novo.id
                    produto_ou_unidade_mudou = (
                        produto_mudou
                        or _normalizar_unidade_estoque(unidade_antiga)
                           != _normalizar_unidade_estoque(unidade_nova)
                    )
                    snapshot_estoque = None
                    snapshot_custo = None

                    if produto_ou_unidade_mudou:
                        if item_antigo.produto_id:
                            produtos_estoque_atualizados_ids.add(item_antigo.produto_id)
                            _devolver_estoque_produto(
                                item_antigo.produto_id,
                                quantidade_antiga,
                                produto_nome_antigo,
                                unidade_antiga,
                            )

                        produtos_estoque_atualizados_ids.add(produto_novo.id)
                        snapshot_estoque = _baixar_estoque_produto(
                            produto_novo.id,
                            quantidade_nova,
                            produto_nome_novo,
                            unidade_nova,
                        )

                        _registrar_evento_venda(
                            venda,
                            "item_removido_da_nota",
                            (
                                f"Item removido da nota: {produto_nome_antigo}, "
                                f"quantidade {quantidade_antiga} {unidade_antiga}."
                            ),
                            canal="sistema",
                            usuario=str(dados.get("operador") or "").strip() or venda.operador,
                        )
                        _registrar_evento_venda(
                            venda,
                            "item_adicionado_na_nota",
                            (
                                f"Item adicionado na nota: {produto_nome_novo}, "
                                f"quantidade {quantidade_nova} {unidade_nova}, "
                                f"preco unitario R$ {preco_novo}."
                            ),
                            canal="sistema",
                            usuario=str(dados.get("operador") or "").strip() or venda.operador,
                        )

                    elif quantidade_nova != quantidade_antiga:
                        _validar_quantidade_produto_unidade(
                            produto_novo,
                            quantidade_nova,
                            unidade_nova,
                        )
                        diferenca = (quantidade_nova - quantidade_antiga).quantize(Decimal("0.001"))

                        if diferenca > 0:
                            produtos_estoque_atualizados_ids.add(produto_novo.id)
                            _baixar_estoque_produto(
                                produto_novo.id,
                                diferenca,
                                produto_nome_novo,
                                unidade_nova,
                                validar_quantidade=False,
                            )
                        else:
                            produtos_estoque_atualizados_ids.add(produto_novo.id)
                            _devolver_estoque_produto(
                                produto_novo.id,
                                abs(diferenca),
                                produto_nome_novo,
                                unidade_nova,
                                validar_quantidade=False,
                            )

                        produto_novo.refresh_from_db(fields=["quantidade"])
                        snapshot_estoque = _snapshot_item_venda_quantidade_atual(
                            produto_novo,
                            quantidade_nova,
                            unidade_nova,
                        )

                        _registrar_evento_venda(
                            venda,
                            "quantidade_item_alterada",
                            (
                                f"Quantidade alterada na nota: {produto_nome_novo}. "
                                f"De {quantidade_antiga} {unidade_antiga} "
                                f"para {quantidade_nova} {unidade_nova}. "
                                f"Total sera recalculado ao final da edicao."
                            ),
                            canal="sistema",
                            usuario=str(dados.get("operador") or "").strip() or venda.operador,
                        )

                    if snapshot_estoque is not None:
                        if produto_mudou:
                            snapshot_custo = _snapshot_custo_item_venda(
                                produto_novo,
                                snapshot_estoque,
                                quantidade_nova,
                                unidade_nova,
                            )
                        else:
                            snapshot_custo = _snapshot_custo_item_venda_existente(
                                item_antigo,
                                produto_novo,
                                snapshot_estoque,
                                quantidade_nova,
                                unidade_nova,
                            )

                    if preco_novo != preco_antigo:
                        _registrar_evento_venda(
                            venda,
                            "cabecalho_nota_alterado",
                            (
                                f"Preco alterado na nota para {produto_nome_novo}: "
                                f"R$ {preco_antigo} -> R$ {preco_novo}."
                            ),
                            canal="sistema",
                            usuario=str(dados.get("operador") or "").strip() or venda.operador,
                        )

                    item_antigo.produto = produto_novo
                    item_antigo.quantidade = quantidade_nova
                    item_antigo.unidade = unidade_nova
                    item_antigo.preco_unitario = preco_novo
                    item_antigo.valor_total = (
                        quantidade_nova * preco_novo
                    ).quantize(Decimal("0.01"))
                    campos_item_alterados = [
                        "produto",
                        "quantidade",
                        "unidade",
                        "preco_unitario",
                        "valor_total",
                    ]
                    if snapshot_estoque is not None:
                        item_antigo.estoque_antes = snapshot_estoque["estoque_antes"]
                        item_antigo.estoque_movimentado = snapshot_estoque["estoque_movimentado"]
                        item_antigo.estoque_depois = snapshot_estoque["estoque_depois"]
                        item_antigo.estoque_unidade_snapshot = snapshot_estoque["estoque_unidade_snapshot"]
                        campos_item_alterados.extend([
                            "estoque_antes",
                            "estoque_movimentado",
                            "estoque_depois",
                            "estoque_unidade_snapshot",
                        ])
                    if snapshot_custo is not None:
                        item_antigo.custo_unitario_snapshot = snapshot_custo["custo_unitario_snapshot"]
                        item_antigo.custo_total_snapshot = snapshot_custo["custo_total_snapshot"]
                        item_antigo.custo_unidade_snapshot = snapshot_custo["custo_unidade_snapshot"]
                        item_antigo.custo_origem_snapshot = snapshot_custo["custo_origem_snapshot"]
                        campos_item_alterados.extend([
                            "custo_unitario_snapshot",
                            "custo_total_snapshot",
                            "custo_unidade_snapshot",
                            "custo_origem_snapshot",
                        ])

                    item_antigo.save(update_fields=campos_item_alterados)

                # ------------------------------------------------------------
                # Itens removidos da tela: devolver estoque e excluir.
                # ------------------------------------------------------------
                for item_antigo in itens_atuais:
                    if item_antigo.id in ids_recebidos:
                        continue

                    produto_nome = (
                        item_antigo.produto.nome
                        if item_antigo.produto
                        else "Produto nao identificado"
                    )

                    item_removido = ItemVendaRemovido.objects.create(
                        venda=venda,
                        produto=item_antigo.produto,
                        produto_nome_snapshot=produto_nome,
                        quantidade_snapshot=item_antigo.quantidade,
                        unidade_snapshot=item_antigo.unidade or "",
                        preco_unitario_snapshot=item_antigo.preco_unitario,
                        valor_total_snapshot=item_antigo.valor_total,
                        estoque_antes=item_antigo.estoque_antes,
                        estoque_movimentado=item_antigo.estoque_movimentado,
                        estoque_depois=item_antigo.estoque_depois,
                        estoque_unidade_snapshot=item_antigo.estoque_unidade_snapshot or "",
                        custo_unitario_snapshot=item_antigo.custo_unitario_snapshot,
                        custo_total_snapshot=item_antigo.custo_total_snapshot,
                        custo_unidade_snapshot=item_antigo.custo_unidade_snapshot or "",
                        custo_origem_snapshot=item_antigo.custo_origem_snapshot or "",
                        item_venda_original_id=item_antigo.id,
                        operador=str(dados.get("operador") or "").strip() or venda.operador,
                        observacao="Item removido pela edicao unificada da venda.",
                    )

                    if item_antigo.produto_id:
                        produtos_estoque_atualizados_ids.add(item_antigo.produto_id)
                    _devolver_estoque_item_removido(item_removido)

                    _registrar_evento_venda(
                        venda,
                        "item_removido_da_nota",
                        (
                            f"Item removido da nota: {produto_nome}, "
                            f"quantidade {item_antigo.quantidade} {item_antigo.unidade or ''}, "
                            f"valor abatido R$ {item_antigo.valor_total}."
                        ),
                        canal="sistema",
                        usuario=str(dados.get("operador") or "").strip() or venda.operador,
                    )

                    item_antigo.delete()

                # ------------------------------------------------------------
                # Itens novos: baixar estoque e criar.
                # ------------------------------------------------------------
                produtos_ja_presentes = set(
                    ItemVenda.objects
                    .filter(venda=venda)
                    .values_list("produto_id", flat=True)
                )

                for item_novo in itens_validados:
                    if item_novo.get("item_id"):
                        continue

                    produto_novo = item_novo["produto"]

                    if produto_novo.id in produtos_ja_presentes:
                        raise ValueError(
                            f'O produto "{produto_novo.nome}" ja existe nesta venda.'
                        )

                    produtos_estoque_atualizados_ids.add(produto_novo.id)
                    snapshot_estoque = _baixar_estoque_produto(
                        produto_novo.id,
                        item_novo["quantidade"],
                        produto_novo.nome,
                        item_novo["unidade"],
                    )

                    snapshot_custo = _snapshot_custo_item_venda(
                        produto_novo,
                        snapshot_estoque,
                        item_novo["quantidade"],
                        item_novo["unidade"],
                    )

                    item_criado = ItemVenda.objects.create(
                        venda=venda,
                        produto=produto_novo,
                        quantidade=item_novo["quantidade"],
                        unidade=item_novo["unidade"],
                        preco_unitario=item_novo["preco_unitario"],
                        valor_total=item_novo["valor_total"],
                        **snapshot_estoque,
                        **snapshot_custo,
                    )
                    produtos_ja_presentes.add(produto_novo.id)

                    _registrar_evento_venda(
                        venda,
                        "item_adicionado_na_nota",
                        (
                            f"Item adicionado na nota: {produto_novo.nome}, "
                            f"quantidade {item_novo['quantidade']} {item_novo['unidade'] or ''}, "
                            f"preco unitario R$ {item_novo['preco_unitario']}, "
                            f"valor acrescentado R$ {item_novo['valor_total']}."
                        ),
                        canal="sistema",
                        usuario=str(dados.get("operador") or "").strip() or venda.operador,
                    )

                if not ItemVenda.objects.filter(venda=venda).exists():
                    raise ValueError("A venda precisa permanecer com pelo menos um item.")

                # ------------------------------------------------------------
                # Cabecalho e total.
                # ------------------------------------------------------------
                venda.cliente = cliente
                venda.data_venda = data_venda
                venda.data_vencimento = data_vencimento
                venda.tipo_pagamento = tipo_pagamento_venda
                venda.operador = str(dados.get("operador") or "").strip()
                venda.total = _recalcular_total_venda_pelos_itens(venda)

                venda.save(update_fields=[
                    "cliente",
                    "data_venda",
                    "data_vencimento",
                    "tipo_pagamento",
                    "operador",
                    "total",
                    "atualizado_em",
                ])

                cabecalho_mudou = (
                    cliente_anterior_id != venda.cliente_id
                    or data_anterior != venda.data_venda
                    or vencimento_anterior != venda.data_vencimento
                    or pagamento_anterior != (venda.tipo_pagamento or "")
                    or operador_anterior != (venda.operador or "")
                )

                if cabecalho_mudou:
                    _registrar_evento_venda(
                        venda,
                        "cabecalho_nota_alterado",
                        (
                            "Cabecalho da nota alterado pela edicao unificada. "
                            f"Total anterior R$ {total_anterior}; novo total R$ {venda.total}."
                        ),
                        canal="sistema",
                        usuario=venda.operador,
                    )

                if conversao_prazo_para_vista:
                    _regularizar_conta_receber_conversao_venda_a_vista(venda)
                    _validar_origem_venda_a_vista(valores_origem_venda, venda.total)
                    _registrar_movimentos_venda_a_vista(venda, valores_origem_venda)
                elif conversao_vista_para_prazo:
                    _estornar_movimentos_venda_a_vista_para_prazo(venda)
                    _abrir_conta_receber_conversao_venda_a_prazo(venda)
                elif edicao_vista_para_vista:
                    _ajustar_movimentos_edicao_venda_a_vista(
                        venda,
                        total_anterior,
                        valores_origem_venda,
                    )
                elif edicao_consumo_proprio:
                    _sincronizar_despesas_consumo_proprio(venda)
                else:
                    _sincronizar_conta_receber(
                        venda,
                        "edicao unificada da venda",
                    )
                _recalcular_status_separacao_da_venda(venda, usuario)

                if (
                    separacao_ajuste_edicao
                    and payload_ajuste_separacao
                    and _ajustes_separacao_foram_aplicados(
                        venda,
                        payload_ajuste_separacao,
                    )
                ):
                    agora = timezone.now()
                    separacao_ajuste_edicao.revisao_pendente = False
                    separacao_ajuste_edicao.teve_revisao = True
                    separacao_ajuste_edicao.revisao_concluida_em = agora
                    separacao_ajuste_edicao.status = SeparacaoVenda.STATUS_SEPARADA
                    separacao_ajuste_edicao.save(
                        update_fields=[
                            "revisao_pendente",
                            "teve_revisao",
                            "revisao_concluida_em",
                            "status",
                            "atualizado_em",
                        ]
                    )

                    _registrar_evento_venda(
                        venda,
                        "alteracao_separacao_atendida",
                        "Ajustes da separacao aplicados pela edicao da venda.",
                        usuario=usuario,
                    )

        except ValueError as exc:
            raise ErroGravarVenda(str(exc))

        return ResultadoVenda(
            venda=venda,
            mensagem=f"Venda #{venda.id} atualizada com sucesso.",
            edicao=True,
            produtos_estoque_atualizados_ids=produtos_estoque_atualizados_ids,
        )

    pedido_pendencias_estoque = []
    produtos_estoque_atualizados_ids = set()
    try:
        with transaction.atomic():
            pedido_origem = None
            pedido_id = dados.get("pedido_id")
            if pedido_id:
                from ..models import Pedido

                pedido_origem = Pedido.objects.select_for_update().filter(pk=pedido_id).first()
                if not pedido_origem:
                    raise ErroGravarVenda("Pedido de origem nao foi encontrado.")
                if pedido_origem.status not in [Pedido.STATUS_ABERTO, Pedido.STATUS_PARCIAL]:
                    raise ErroGravarVenda("Pedido de origem nao esta aberto nem parcial.")

            itens_para_venda = itens_validados
            total_venda = total_calculado
            if pedido_origem:
                itens_para_venda = []
                total_venda = Decimal("0.00")
                for item in itens_validados:
                    produto_bloqueado = Produto.objects.select_for_update().get(pk=item["produto"].pk)
                    quantidade_base, _unidade_base = _quantidade_estoque_para_unidade_base(
                        produto_bloqueado,
                        item["quantidade"],
                        item["unidade"],
                    )
                    quantidade_necessaria = (
                        quantidade_base
                        if produto_bloqueado.vende_fracionado
                        else _quantidade_estoque_inteira(
                            quantidade_base,
                            produto_bloqueado.nome,
                            _unidade_base,
                        )
                    )
                    estoque_disponivel = max(
                        _quantidade_decimal_estoque(produto_bloqueado.quantidade),
                        Decimal("0.000"),
                    )
                    quantidade_vendida_base = min(quantidade_necessaria, estoque_disponivel)
                    quantidade_pendente_base = quantidade_necessaria - quantidade_vendida_base

                    if quantidade_vendida_base > 0:
                        if (
                            item["unidade"]
                            and produto_bloqueado.vende_fracionado
                            and _normalizar_unidade_estoque(item["unidade"])
                            == _normalizar_unidade_estoque(produto_bloqueado.unidade_venda_2)
                            and Decimal(produto_bloqueado.fator_conversao or 0) > 0
                        ):
                            quantidade_vendida = (
                                quantidade_vendida_base
                                * Decimal(produto_bloqueado.fator_conversao or 0)
                            ).quantize(Decimal("0.001"))
                        else:
                            quantidade_vendida = quantidade_vendida_base.quantize(Decimal("0.001"))
                        valor_total_vendido = (quantidade_vendida * item["preco_unitario"]).quantize(Decimal("0.01"))
                        itens_para_venda.append({
                            "produto": produto_bloqueado,
                            "quantidade": quantidade_vendida,
                            "unidade": item["unidade"],
                            "preco_unitario": item["preco_unitario"],
                            "valor_total": valor_total_vendido,
                            "estoque_antes": estoque_disponivel,
                            "estoque_movimentado": quantidade_vendida_base.quantize(Decimal("0.001")),
                            "estoque_depois": (
                                estoque_disponivel - quantidade_vendida_base
                            ).quantize(Decimal("0.001")),
                            "estoque_unidade_snapshot": _unidade_base or "",
                        })
                        total_venda += valor_total_vendido
                        produto_bloqueado.quantidade = (
                            estoque_disponivel - quantidade_vendida_base
                        ).quantize(Decimal("0.001"))
                        Produto.objects.filter(pk=produto_bloqueado.pk).update(
                            quantidade=produto_bloqueado.quantidade,
                            atualizado_em=timezone.now(),
                        )
                        produtos_estoque_atualizados_ids.add(produto_bloqueado.pk)

                    if quantidade_pendente_base > 0:
                        if (
                            item["unidade"]
                            and produto_bloqueado.vende_fracionado
                            and _normalizar_unidade_estoque(item["unidade"])
                            == _normalizar_unidade_estoque(produto_bloqueado.unidade_venda_2)
                            and Decimal(produto_bloqueado.fator_conversao or 0) > 0
                        ):
                            quantidade_pendente = (
                                quantidade_pendente_base
                                * Decimal(produto_bloqueado.fator_conversao or 0)
                            ).quantize(Decimal("0.001"))
                        else:
                            quantidade_pendente = quantidade_pendente_base.quantize(Decimal("0.001"))
                        pendencia = f"{produto_bloqueado.nome}: {_formatar_quantidade(quantidade_pendente)}"
                        if item["unidade"]:
                            pendencia = f"{pendencia} {item['unidade']}"
                        pedido_pendencias_estoque.append(pendencia)

                if not itens_para_venda:
                    mensagem = (
                        f"Nenhum item do Pedido #{pedido_origem.id} possui estoque disponivel "
                        "para gerar venda. Os itens continuam pendentes no pedido."
                    )
                    raise ErroGravarVenda(mensagem, extras={"toast_duracao_ms": 12000})
            else:
                snapshots_estoque = _baixar_estoque_movimentos(
                    (item["produto"], item["quantidade"], item["unidade"])
                    for item in itens_para_venda
                )
                for item, snapshot_estoque in zip(itens_para_venda, snapshots_estoque):
                    item.update(snapshot_estoque)
                    produtos_estoque_atualizados_ids.add(item["produto"].pk)

            for item in itens_para_venda:
                snapshot_custo = _snapshot_custo_item_venda(
                    item["produto"],
                    item,
                    item["quantidade"],
                    item["unidade"],
                )
                item.update(snapshot_custo)

            venda = Venda.objects.create(
                cliente=cliente,
                data_venda=data_venda,
                data_vencimento=data_vencimento,
                tipo_pagamento=tipo_pagamento_venda,
                operador=str(dados.get("operador") or "").strip(),
                total=total_venda.quantize(Decimal("0.01")),
            )

            ItemVenda.objects.bulk_create([
                ItemVenda(
                    venda=venda,
                    produto=item["produto"],
                    quantidade=item["quantidade"],
                    unidade=item["unidade"],
                    preco_unitario=item["preco_unitario"],
                    valor_total=item["valor_total"],
                    estoque_antes=item.get("estoque_antes"),
                    estoque_movimentado=item.get("estoque_movimentado"),
                    estoque_depois=item.get("estoque_depois"),
                    estoque_unidade_snapshot=item.get("estoque_unidade_snapshot", ""),
                    custo_unitario_snapshot=item.get("custo_unitario_snapshot"),
                    custo_total_snapshot=item.get("custo_total_snapshot"),
                    custo_unidade_snapshot=item.get("custo_unidade_snapshot", ""),
                    custo_origem_snapshot=item.get("custo_origem_snapshot", ""),
                )
                for item in itens_para_venda
            ])

            _registrar_evento_venda(
                venda,
                "venda_gravada",
                "Venda gravada com sucesso. Estoque baixado para os itens vendidos.",
                canal="sistema",
                usuario=venda.operador,
            )
            _sincronizar_conta_receber(venda, "venda gravada")
            _sincronizar_despesas_consumo_proprio(venda)
            if _venda_pagamento_imediato(venda.tipo_pagamento):
                if valores_origem_venda is None:
                    _registrar_movimento_venda_a_vista(venda)
                else:
                    _validar_origem_venda_a_vista(valores_origem_venda, venda.total)
                    _registrar_movimentos_venda_a_vista(venda, valores_origem_venda)

            if pedido_origem:
                if pedido_pendencias_estoque:
                    _atualizar_saldo_pendente_pedido(pedido_origem, itens_para_venda)
                pedido_origem.status = (
                    Pedido.STATUS_PARCIAL
                    if pedido_pendencias_estoque
                    else Pedido.STATUS_CONVERTIDO_EM_VENDA
                )
                pedido_origem.save(update_fields=["status", "atualizado_em"])
                if pedido_pendencias_estoque:
                    descricao_pedido_parcial = [
                        f"Venda gerada parcialmente a partir do Pedido #{pedido_origem.id}.",
                        "Itens pendentes do pedido:",
                    ]
                    descricao_pedido_parcial.extend(
                        f"- {pendencia}" for pendencia in pedido_pendencias_estoque
                    )
                    _registrar_evento_venda(
                        venda,
                        "pedido_parcial",
                        "\n".join(descricao_pedido_parcial),
                        canal="sistema",
                        usuario=venda.operador,
                    )
    except ValueError as exc:
        raise ErroGravarVenda(str(exc))

    mensagem = f"Venda #{venda.id} gravada com sucesso."
    if pedido_pendencias_estoque:
        mensagem = (
            f"Venda #{venda.id} gravada com itens disponiveis. "
            "Alguns itens ficaram pendentes por falta de estoque: "
            + "; ".join(pedido_pendencias_estoque)
            + "."
        )

    return ResultadoVenda(
        venda=venda, mensagem=mensagem, edicao=False,
        produtos_estoque_atualizados_ids=produtos_estoque_atualizados_ids,
    )
