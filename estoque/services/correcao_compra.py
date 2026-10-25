"""Explicit, previewed purchase corrections; never infer a cash refund from a price delta."""
from decimal import Decimal, InvalidOperation
from django.core import signing
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_date
from estoque.models import (Compra, ItemCompra, Produto, ContaPagar, PagamentoContaPagar,
    MovimentoFinanceiro, ContaFinanceira, EdicaoCompra, ConciliacaoCompra, ListaCompraFornecedor)
from estoque.services.edicao_compra import estado_compra, _hash, _json, SALT as EDIT_SALT, _tipo_formulario
from estoque.services.precos_vinculados import bloquear_catalogo

SALT = "estoque.correcao-compra.v1"
TIPOS = {"erro_lancamento": "Erro de lançamento", "devolucao": "Devolução ao fornecedor", "diferenca": "Diferença de preço ou quantidade"}
ZERO = Decimal("0.00")


def dinheiro(raw, nome):
    try:
        value = Decimal(str(raw).strip().replace(",", "."))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"Informe {nome} com no máximo duas casas decimais.")
    if not value.is_finite() or value < 0 or value >= Decimal("10000000000") or value != value.quantize(Decimal("0.01")):
        raise ValueError(f"Informe {nome} válido, positivo ou zero, com até duas casas decimais.")
    return value.quantize(Decimal("0.01"))


def payload_hash(post):
    return _hash({k: post.getlist(k) for k in post if k not in {
        "csrfmiddlewaretoken", "edicao_token", "previa_token", "acao_compra"}})


def _estado(compra, produtos):
    from estoque.models import ProdutoFornecedor
    produtos = list(produtos)
    return {"compra": estado_compra(compra), "produtos": list(produtos),
        "fornecedores":list(ProdutoFornecedor.objects.filter(
            produto_id__in=[p['id'] for p in produtos],fornecedor_id=compra.fornecedor_id).order_by('pk').values()),
        "conciliacao": list(ConciliacaoCompra.objects.filter(compra=compra).values())}


def _financeiro(compra):
    """Return proved principal payments/refunds, excluding bank interest and canceled payments."""
    estado = estado_compra(compra)
    contas = list(ContaPagar.objects.filter(compra=compra).order_by("numero_parcela", "pk"))
    pagamentos = list(PagamentoContaPagar.objects.filter(conta__compra=compra).order_by("pk"))
    movimentos = list(MovimentoFinanceiro.objects.filter(
        Q(compra=compra) | Q(pagamento_conta_pagar__conta__compra=compra)).distinct().order_by("pk"))
    if {m.pk for m in movimentos} != {m["id"] for m in estado["movimentos"]}:
        raise ValueError("Vínculo financeiro legado ambíguo: nenhum ajuste foi aplicado. Identifique o vínculo comprovado.")
    if compra.cartao_id or estado["cartao"]:
        raise ValueError("Compra com cartão/fatura requer conciliação específica; pagamentos não foram alterados.")
    pagos = {}; total = ZERO; devolvido = ZERO
    por_pagamento = {}
    for m in movimentos:
        if not m.valor.is_finite() or m.valor <= 0 or m.compra_id not in {None, compra.pk}:
            raise ValueError("Movimento financeiro com valor ou vínculo incompatível.")
        if m.pagamento_conta_pagar_id:
            por_pagamento.setdefault(m.pagamento_conta_pagar_id, []).append(m)
        elif m.compra_id == compra.pk and m.tipo == "saida" and m.origem in {"compra_a_vista", "compra_correcao_item", "compra_correcao_origem"} and not m.conta_destino_id:
            total += m.valor
        elif m.compra_id == compra.pk and m.tipo == "entrada" and m.origem == "compra_devolucao_correcao" and not m.conta_destino_id:
            devolvido += m.valor
        else:
            raise ValueError("Movimento financeiro não reconhecido para conciliação automática; nenhum ajuste foi aplicado.")
    for p in pagamentos:
        ms = por_pagamento.get(p.pk, [])
        if any(m.tipo not in {"entrada", "saida"} or m.conta_destino_id for m in ms):
            raise ValueError("Pagamento com vínculo incompatível para conciliação.")
        liquido = sum((m.valor if m.tipo == "saida" else -m.valor for m in ms), ZERO)
        esperado = ZERO if p.cancelado else p.valor + p.juros_bancarios
        if liquido != esperado:
            raise ValueError("Pagamento e movimentos divergem; nenhum pagamento foi refeito ou alterado.")
        if not p.cancelado:
            pagos[p.conta_id] = pagos.get(p.conta_id, ZERO) + p.valor
            total += p.valor
    for c in contas:
        pago = pagos.get(c.pk, ZERO)
        if c.fornecedor_id != compra.fornecedor_id or c.data_emissao != compra.data_compra:
            raise ValueError("Conta a pagar diverge do fornecedor/data da compra.")
        if c.status == ContaPagar.STATUS_CANCELADA:
            if pago:
                raise ValueError("Conta cancelada com pagamento ativo requer conciliação específica.")
        elif c.valor_original - c.valor_em_aberto != pago or c.valor_em_aberto < 0:
            raise ValueError("Saldo da conta a pagar não corresponde aos pagamentos comprovados.")
    if total < 0 or devolvido > total:
        raise ValueError("Os registros de pagamento e devolução são inconsistentes.")
    return total.quantize(Decimal("0.01")), devolvido.quantize(Decimal("0.01")), contas, pagos


def _planejar(request, compra, automatica=False):
    from estoque.views import _dados_compra_post, _quantidade_estoque_para_unidade_base, _normalizar_unidade_estoque
    if not request.user.is_authenticated or not request.user.has_perm("estoque.change_compra"):
        raise ValueError("Usuário sem permissão para corrigir compras.")
    tipo = "erro_lancamento" if automatica else request.POST.get("tipo_correcao", "")
    motivo = str(request.POST.get("motivo_edicao") or "").strip()
    if tipo not in TIPOS or not motivo or len(motivo) > 1000:
        if automatica:
            raise ValueError("Informe uma justificativa de até 1000 caracteres para salvar a correção.")
        raise ValueError("Selecione o motivo da correção e informe uma justificativa de até 1000 caracteres.")
    if compra.cancelada or compra.status != Compra.STATUS_FINALIZADA or not compra.estoque_entrada_realizada:
        raise ValueError("Somente compra finalizada, não cancelada e com estoque confirmado pode ser corrigida.")
    if not compra.fornecedor_id:
        raise ValueError("Compra sem fornecedor comprovado.")
    if any(k.startswith(("atualizar_custo_", "atualizar_preco_venda_", "novo_preco_venda_")) for k in request.POST):
        raise ValueError("A correção não altera preços ou custos do catálogo.")
    dados = _dados_compra_post(request, exigir_itens=tipo != "devolucao", compra_edicao=compra)
    if (dados["fornecedor"].pk != compra.fornecedor_id or dados["data_compra"] != compra.data_compra
            or _tipo_formulario(dados["tipo_pagamento"]) != _tipo_formulario(compra.tipo_pagamento)
            or (dados["cartao"].pk if dados["cartao"] else None) != compra.cartao_id):
        raise ValueError("A correção de itens preserva fornecedor, data, origem e forma de pagamento.")
    if any(request.POST.get(k) and dinheiro(request.POST.get(k), "origem") != 0 for k in ("origem_caixa", "origem_banco", "origem_reserva")):
        raise ValueError("A correção preserva o pagamento: não informe novas saídas de dinheiro.")
    if compra.observacao and "Lista de Compras #" in compra.observacao and not compra.lista_fornecedor_id:
        raise ValueError("A origem por lista deve ter vínculo comprovado antes da correção.")
    antigos = list(compra.itens.select_related("produto").order_by("pk"))
    produtos_ids = {i.produto_id for i in antigos} | {x["produto"].pk for x in dados["itens"]}
    if None in produtos_ids:
        raise ValueError("Produto histórico sem cadastro: vínculo precisa ser comprovado.")
    produtos = {p.pk: p for p in Produto.objects.filter(pk__in=produtos_ids).order_by("pk")}
    if len(produtos) != len(produtos_ids):
        raise ValueError("Produto não encontrado.")
    deltas = {}; por_id = {i.pk:i for i in antigos}; usados = set(); linhas = []; substituicoes = []
    # Historical finalization wrote raw item.quantity into stock. Once corrected,
    # use the exact recorded base quantity, never reconvert a historical posting.
    anterior_conciliacao = ConciliacaoCompra.objects.filter(compra=compra).first()
    unidades_anteriores = {x.get("item_corrigido_id"): x.get("unidade_base")
        for x in (anterior_conciliacao.efeitos.get("itens",[]) if anterior_conciliacao else [])}
    for item in antigos:
        unidade_anterior = unidades_anteriores.get(item.pk)
        unidade_atual = item.produto.unidade_venda_1 or item.produto.unidade_compra or ""
        if item.estoque_base_registrado is not None and unidade_anterior and _normalizar_unidade_estoque(unidade_anterior) != _normalizar_unidade_estoque(unidade_atual):
            raise ValueError("A unidade base do cadastro mudou após a última correção; não é seguro reinterpretar o estoque histórico.")
        registrado = item.estoque_base_registrado if item.estoque_base_registrado is not None else item.quantidade
        deltas[item.produto_id] = deltas.get(item.produto_id, ZERO) - registrado
    for linha in dados["itens"]:
        pk = linha["item_id"]
        if pk is not None and (pk not in por_id or pk in usados):
            raise ValueError("ID de item inválido, duplicado ou pertencente a outra compra.")
        if pk is not None:
            usados.add(pk)
        item = por_id.get(pk)
        produto = produtos[linha["produto"].pk]
        base, unidade_base = _quantidade_estoque_para_unidade_base(produto, linha["quantidade"], linha["unidade"])
        if base <= 0 or base >= Decimal("1000000000") or linha["preco_unitario"] >= Decimal("10000000000"):
            raise ValueError("Quantidade, conversão ou preço fora do limite permitido.")
        if tipo == "devolucao" and (not item or item.produto_id != produto.pk or linha["unidade"] != item.unidade or linha["preco_unitario"] != item.preco_unitario or linha["quantidade"] > item.quantidade):
            raise ValueError("Devolução física só pode reduzir/remover itens originais, preservando produto, unidade e preço.")
        deltas[produto.pk] = deltas.get(produto.pk, ZERO) + base
        linhas.append({**linha, "base":base})
        substituicoes.append({"item_id":pk, "produto_anterior_id":item.produto_id if item else None,
            "produto_corrigido_id":produto.pk, "nome_anterior":item.produto.nome if item else None,
            "nome_corrigido":produto.nome, "quantidade":str(linha["quantidade"]), "unidade":linha["unidade"],
            "estoque_base":str(base), "unidade_base":unidade_base, "preco":str(linha["preco_unitario"])})
    removidos = [i for i in antigos if i.pk not in usados]
    if tipo == "devolucao" and not any(v < 0 for v in deltas.values()):
        raise ValueError("Informe a mercadoria efetivamente devolvida reduzindo ou removendo o item.")
    for pk, delta in deltas.items():
        if not ZERO <= Decimal(produtos[pk].quantidade or 0) + delta < Decimal("1000000000"):
            raise ValueError(f"A correção deixaria o estoque de {produtos[pk].nome} negativo ou fora do limite.")
    pago, ja_devolvido, contas, pagos = _financeiro(compra)
    pago_registrado = pago
    # Preserve the historical freight/discount adjustment when only items change.
    ajuste_preservado = Decimal(compra.ajuste_total or ZERO) if automatica else ZERO
    total_corrigido = dados["total_produtos"] + ajuste_preservado
    if automatica:
        # Only the original, unlinked automatic posting represents a correctable
        # bookkeeping amount. Actual account payments are immutable evidence.
        movimentos = list(MovimentoFinanceiro.objects.filter(compra=compra))
        conciliacao = ConciliacaoCompra.objects.filter(compra=compra).first()
        anterior_automatico = bool(conciliacao and conciliacao.efeitos.get("registro_automatico_retificavel"))
        automatico = (not contas and not ja_devolvido and len(movimentos) == 1
            and movimentos[0].origem == "compra_a_vista" and movimentos[0].tipo == "saida"
            and not movimentos[0].pagamento_conta_pagar_id and not movimentos[0].conta_destino_id
            and movimentos[0].valor == compra.total
            and (not conciliacao or anterior_automatico))
        if movimentos and not contas and total_corrigido != compra.total and not automatico:
            raise ValueError("Não foi possível identificar um lançamento automático único para retificar. Os movimentos financeiros precisam de conciliação; nenhuma alteração foi salva.")
        pago_real = total_corrigido if automatico else pago
        if pago_real > 0 and pago_real != pago and not request.user.has_perm("estoque.change_movimentofinanceiro"):
            raise ValueError("Usuário sem permissão para retificar o lançamento financeiro automático. Nenhuma alteração foi salva.")
        if automatico and total_corrigido <= 0:
            raise ValueError("O total corrigido deve ser positivo para retificar o lançamento automático.")
        if pago_real - ja_devolvido > total_corrigido:
            raise ValueError("O total corrigido é menor que o pagamento comprovado. Confirme a destinação da diferença na conciliação financeira; nenhum crédito ou devolução foi criado e nenhuma alteração foi salva.")
        pagamento_real_confirmado = False
    else:
        pago_real = dinheiro(request.POST.get("valor_pago_real"), "o valor efetivamente pago (sem juros bancários)")
        pagamento_real_confirmado = request.POST.get("pagamento_real_confirmado") == "1"
    if not automatica and not pagamento_real_confirmado:
        raise ValueError("Confirme o valor efetivamente pago consultando a nota e o comprovante. O valor lançado no sistema não comprova pagamento real.")
    ajuste_movimento = None
    if pago_real != pago:
        candidatos = list(MovimentoFinanceiro.objects.filter(compra=compra).select_related("conta"))
        elegivel = (tipo == "erro_lancamento" and pago_real > 0 and not contas and not ja_devolvido
            and (automatica or not ConciliacaoCompra.objects.filter(compra=compra).exists()) and len(candidatos) == 1
            and candidatos[0].origem == "compra_a_vista" and candidatos[0].tipo == "saida"
            and not candidatos[0].pagamento_conta_pagar_id and not candidatos[0].conta_destino_id
            and _tipo_formulario(compra.tipo_pagamento) in {"avista", "cartao_debito"})
        if not elegivel:
            raise ValueError("O valor pago informado diverge dos pagamentos comprovados. Pagamentos registrados e múltiplas origens não serão sobrescritos.")
        if not request.user.has_perm("estoque.change_movimentofinanceiro"):
            raise ValueError("Usuário sem permissão para corrigir o registro financeiro original.")
        movimento = candidatos[0]
        if any(rel.related_model._base_manager.filter(**{rel.field.name: movimento}).exists() for rel in movimento._meta.related_objects):
            raise ValueError("Registro financeiro possui outros vínculos; pagamentos reais não serão alterados.")
        ajuste_movimento = {"id":movimento.pk, "conta":movimento.conta.nome,
            "natureza":"retificacao_automatica" if automatica else "valor_real_informado",
            "valor_anterior":str(movimento.valor), "valor_real_informado":str(pago_real)}
        pago = pago_real
    devolver = dinheiro("0" if automatica else request.POST.get("valor_devolvido_real") or "0", "o valor efetivamente devolvido nesta correção")
    total = total_corrigido
    if not total.is_finite() or total < ZERO or total >= Decimal("10000000000"):
        raise ValueError("Total corrigido fora do limite permitido.")
    disponivel = max(ZERO, pago - ja_devolvido - total)
    if devolver > disponivel or (tipo == "erro_lancamento" and devolver):
        raise ValueError("Devolução financeira excede o crédito comprovado ou não corresponde ao motivo informado.")
    conta_refund = None; data_refund = None
    if devolver:
        if not request.user.has_perm("estoque.add_movimentofinanceiro"):
            raise ValueError("Usuário sem permissão para registrar devolução financeira.")
        conta_refund = ContaFinanceira.objects.filter(pk=request.POST.get("conta_devolucao"), ativo=True).first()
        data_refund = parse_date(request.POST.get("data_devolucao") or "")
        if not conta_refund or not data_refund or not compra.data_compra <= data_refund <= timezone.localdate():
            raise ValueError("Informe a conta ativa e a data real da devolução financeira (entre a compra e hoje).")
    credito = max(ZERO, pago - ja_devolvido - devolver - total)
    if credito and request.POST.get("credito_real_confirmado") != "1":
        raise ValueError("Confirme que o pagamento real supera o total corrigido e que a diferença não foi devolvida. Se o valor apenas foi lançado incorretamente, informe o valor realmente pago; nenhum crédito será presumido.")
    aberto = max(ZERO, total - pago + ja_devolvido + devolver)
    planejadas = []; restante = aberto
    ativas = [c for c in contas if c.status != ContaPagar.STATUS_CANCELADA]
    abertas = [c for c in ativas if c.valor_em_aberto > 0]
    for indice,c in enumerate(abertas):
        valor = restante if indice == len(abertas)-1 else min(restante,c.valor_em_aberto)
        planejadas.append({"conta_id":c.pk,"aberto":str(valor),"pago_preservado":str(pagos.get(c.pk,ZERO))})
        restante -= valor
    criar_conta = restante > 0
    # Locked snapshot also includes product units/conversion and stock.
    estado = _estado(compra, [p for p in Produto.objects.filter(pk__in=produtos_ids).order_by("pk").values()])
    resumo = _json({"tipo":tipo,"motivo":motivo,"total_anterior":compra.total,"total_corrigido":total,
        "total_produtos_corrigido":dados["total_produtos"],"ajuste_preservado":ajuste_preservado,
        "registro_automatico_retificavel":bool(automatica and automatico),
        "diferenca":total-compra.total,"pago_registrado_antes":pago_registrado,
        "modo_automatico":automatica,"pagamento_real_confirmado":pagamento_real_confirmado,"credito_real_confirmado":bool(credito),"pago_preservado":pago,"devolvido_antes":ja_devolvido,
        "devolvido_agora":devolver,"credito_fornecedor":credito,"saldo_a_pagar":aberto,
        "deltas_estoque":[{"produto_id":pk,"produto":produtos[pk].nome,"delta":v} for pk,v in sorted(deltas.items()) if v],
        "itens":substituicoes,"removidos":[{"item_id":i.pk,"produto_id":i.produto_id,"nome":i.produto.nome} for i in removidos],
        "contas":planejadas,"criar_conta":criar_conta,"correcao_registro_financeiro":ajuste_movimento,
        "conta_devolucao":conta_refund.pk if conta_refund else None,
        "data_devolucao":data_refund})
    return {"dados":dados,"linhas":linhas,"removidos":removidos,"produtos":produtos,"deltas":deltas,
        "resumo":resumo,"estado":estado,"contas":contas,"pagos":pagos}


def _token_edicao(request, compra):
    try:
        token = signing.loads(request.POST.get("edicao_token", ""), salt=EDIT_SALT, max_age=7200)
    except signing.BadSignature as exc:
        raise ValueError("Edição expirada; recarregue a compra antes de corrigir.") from exc
    if not isinstance(token,dict) or token.get("compra") != compra.pk or token.get("usuario") != request.user.pk or not token.get("operacao"):
        raise ValueError("Identificação da edição inválida.")
    return token


def prever(request, compra):
    token = _token_edicao(request, compra)
    if _hash(estado_compra(compra)) != token.get("versao"):
        raise ValueError("A compra ou pagamentos mudaram. Recarregue para gerar nova prévia.")
    plano = _planejar(request,compra)
    dados = {"compra":compra.pk,"usuario":request.user.pk,"operacao":token["operacao"],
        "payload":payload_hash(request.POST),"estado":_hash(plano["estado"]),"resumo":_hash(plano["resumo"])}
    return {"sucesso":True,"previa":plano["resumo"],"previa_token":signing.dumps(dados,salt=SALT)}


def etapa_correcao(nome):
    """Fault-injection seam, no operational side effects."""


def aplicar(request, compra_id, automatica=False):
    if not request.user.is_authenticated or not request.user.has_perm("estoque.change_compra"):
        raise ValueError("Usuário sem permissão para corrigir compras.")
    if automatica:
        compra = Compra.objects.get(pk=compra_id)
        token = _token_edicao(request, compra)
        token["payload"] = payload_hash(request.POST)
    else:
        try:
            token = signing.loads(request.POST.get("previa_token", ""), salt=SALT, max_age=900)
        except signing.BadSignature as exc:
            raise ValueError("Confirmação expirada ou inválida. Gere novamente a prévia.") from exc
        if not isinstance(token,dict) or token.get("compra") != compra_id or token.get("usuario") != request.user.pk or token.get("payload") != payload_hash(request.POST):
            raise ValueError("Os dados mudaram após a prévia; revise e confirme novamente.")
    with transaction.atomic(), bloquear_catalogo():
        compra = Compra.objects.select_for_update().get(pk=compra_id)
        anterior = EdicaoCompra.objects.filter(operacao=token["operacao"]).first()
        if anterior:
            if anterior.compra_id != compra_id or anterior.usuario_id != request.user.pk or anterior.payload_hash != token["payload"]:
                raise ValueError("Identificação de correção já utilizada com outros dados.")
            return anterior,False
        list(ItemCompra.objects.select_for_update().filter(compra=compra).order_by("pk"))
        contas = list(ContaPagar.objects.select_for_update().filter(compra=compra).order_by("pk"))
        list(PagamentoContaPagar.objects.select_for_update().filter(conta__in=contas).order_by("pk"))
        if compra.lista_fornecedor_id:
            ListaCompraFornecedor.objects.select_for_update().get(pk=compra.lista_fornecedor_id)
        list(MovimentoFinanceiro.objects.select_for_update(of=("self",)).filter(Q(compra=compra)|Q(pagamento_conta_pagar__conta__compra=compra)).order_by("pk"))
        ids = set(compra.itens.values_list("produto_id",flat=True)) | {int(v) for v in request.POST.getlist("produto_id[]") if v}
        list(Produto.objects.select_for_update().filter(pk__in=ids).order_by("pk"))
        if automatica and _hash(estado_compra(compra)) != token.get("versao"):
            raise ValueError("A compra ou pagamentos mudaram. Recarregue antes de corrigir; nenhuma alteração foi salva.")
        plano = _planejar(request,compra,automatica=automatica)
        if not automatica and (_hash(plano["estado"]) != token["estado"] or _hash(plano["resumo"]) != token["resumo"]):
            raise ValueError("Estoque, cadastro, compra ou pagamentos mudaram após a prévia. Gere nova confirmação.")
        antes = plano["estado"]; resumo=plano["resumo"]
        afetados_catalogo = []
        itens_anteriores = {i['id']:i for i in antes['compra']['itens']}
        for pk, delta in plano["deltas"].items():
            if delta:
                p = plano["produtos"][pk]; velho=Decimal(p.quantidade or 0); novo=velho+delta
                Produto.objects.filter(pk=pk).update(quantidade=novo,atualizado_em=timezone.now())
                from inventario.rastreabilidade import identificar
                identificar(pk,velho,novo,"edicao_compra",f"Correção {token['operacao']} - Compra #{compra.pk}")
        etapa_correcao("estoque")
        for indice, linha in enumerate(plano["linhas"]):
            item = ItemCompra.objects.get(pk=linha["item_id"],compra=compra) if linha["item_id"] is not None else ItemCompra(compra=compra)
            for campo in ("quantidade","unidade","preco_unitario","valor_total","observacao"):
                setattr(item,campo,linha[campo])
            if item.pk and item.produto_id != linha["produto"].pk:
                # Original provenance remains in EdicaoCompra. Never attribute the
                # old SKU's catalog-price snapshots to the replacement SKU.
                item.alteracoes_precos={}; item.alteracoes_precos_vinculados=[]
                item.preco_compra_anterior=None; item.revisao_preco_concluida=False
            item.produto=linha["produto"]; item.estoque_base_registrado=linha["base"]; item.save()
            resumo["itens"][indice]["item_corrigido_id"]=item.pk
            original = itens_anteriores.get(item.pk)
            if not original or any(str(getattr(item,c)) != str(original[c]) for c in (
                'produto_id','preco_unitario','unidade')):
                afetados_catalogo.append(item.pk)
        for item in plano["removidos"]:
            item.delete()
        etapa_correcao("itens")
        for parte in resumo["contas"]:
            c=next(c for c in contas if c.pk==parte["conta_id"])
            c.valor_em_aberto=Decimal(parte["aberto"]); pago=Decimal(parte["pago_preservado"])
            c.valor_original=pago+c.valor_em_aberto
            c.status=(ContaPagar.STATUS_PARCIAL if pago else ContaPagar.STATUS_ABERTA) if c.valor_em_aberto else (ContaPagar.STATUS_PAGA if pago else ContaPagar.STATUS_CANCELADA)
            c.save(update_fields=["valor_original","valor_em_aberto","status","atualizado_em"])
        if resumo["criar_conta"]:
            ContaPagar.objects.create(compra=compra,fornecedor_id=compra.fornecedor_id,data_emissao=compra.data_compra,
                data_vencimento=None,valor_original=Decimal(resumo["saldo_a_pagar"]),
                valor_em_aberto=Decimal(resumo["saldo_a_pagar"]),numero_parcela=max([c.numero_parcela or 0 for c in contas]+[0])+1,
                observacao=f"Complemento da correção {token['operacao']}; não representa novo pagamento.")
        movimento=None
        ajuste = resumo["correcao_registro_financeiro"]
        if ajuste:
            MovimentoFinanceiro.objects.filter(pk=ajuste["id"],compra=compra).update(valor=Decimal(ajuste["valor_real_informado"]))
        if Decimal(resumo["devolvido_agora"]):
            conta=ContaFinanceira.objects.select_for_update().get(pk=resumo["conta_devolucao"],ativo=True)
            movimento=MovimentoFinanceiro.objects.create(compra=compra,conta=conta,tipo="entrada",valor=Decimal(resumo["devolvido_agora"]),
                data=resumo["data_devolucao"],origem="compra_devolucao_correcao",operador=request.user.get_username(),
                descricao=f"Devolução efetivamente recebida - Compra #{compra.pk} - {token['operacao']}")
        etapa_correcao("financeiro")
        compra.total=Decimal(resumo["total_corrigido"])
        compra.total_produtos=Decimal(resumo["total_produtos_corrigido"])
        compra.ajuste_total=Decimal(resumo["ajuste_preservado"])
        compra.conciliacao_automatica=True
        compra.observacao=f"{compra.observacao or ''}\nCorreção {token['operacao']}: {TIPOS[resumo['tipo']]}. {resumo['motivo']}".strip()
        compra.save(update_fields=["total","total_produtos","ajuste_total","observacao","conciliacao_automatica","atualizado_em"])
        from .catalogo_correcao_compra import sincronizar
        resumo['catalogo'] = sincronizar(compra, afetados_catalogo, anteriores=antes['compra']['itens'])
        etapa_correcao('catalogo')
        resumo["movimento_devolucao_id"]=movimento.pk if movimento else None
        edicao=EdicaoCompra.objects.create(compra=compra,operacao=token["operacao"],usuario=request.user,motivo=resumo["motivo"],
            payload_hash=token["payload"],antes=_json(antes),depois={})
        ConciliacaoCompra.objects.update_or_create(compra=compra,defaults={"edicao":edicao,"tipo":resumo["tipo"],
            "valor_pago":Decimal(resumo["pago_preservado"]),"valor_devolvido":Decimal(resumo["devolvido_antes"])+Decimal(resumo["devolvido_agora"]),
            "credito_fornecedor":Decimal(resumo["credito_fornecedor"]),"saldo_a_pagar":Decimal(resumo["saldo_a_pagar"]),"efeitos":resumo})
        edicao.depois=_json({"estado":_estado(compra,list(Produto.objects.filter(pk__in=ids).order_by("pk").values())),"ajustes":resumo})
        edicao.save(update_fields=["depois"])
        etapa_correcao("auditoria")
        return edicao,True
