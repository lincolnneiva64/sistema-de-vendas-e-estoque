"""Resoluções operacionais por item, mantendo os snapshots da separação."""
import json
from collections import Counter
from decimal import Decimal, InvalidOperation

from estoque.models import EventoVenda, SeparacaoVendaItem

TIPO_RESOLUCAO = "separacao_quantidade_ajustada"


def historico_separacao(separacao):
    if not hasattr(separacao, "_historico_operacional") or not hasattr(separacao, "_resolucoes_quantidade"):
        from .separacao_vendas import TIPOS_EVENTO_ALTERACAO_FISICA
        separacao._historico_operacional = list(EventoVenda.objects.filter(
            venda_id=separacao.venda_id,
            tipo_evento__in=TIPOS_EVENTO_ALTERACAO_FISICA | {TIPO_RESOLUCAO}
        ).only("id", "tipo_evento", "descricao", "criado_em").order_by("criado_em", "id"))
    return separacao._historico_operacional


def descricao_resolucao(descricao):
    try:
        dados = json.loads(descricao)
        return (f"Quantidade ajustada na venda — resolvido. {dados['produto']}: "
                f"solicitado originalmente {dados['solicitada']} {dados['unidade']}; "
                f"separado na conferência {dados['separada']} {dados['unidade']}; "
                f"nota ajustada para {dados['quantidade_nota']} {dados['unidade']}.")
    except (ValueError, TypeError, KeyError):
        return "Registro de ajuste de quantidade da separação."


def ler_resolucoes(separacao):
    cache = getattr(separacao, "_resolucoes_quantidade", None)
    if cache is not None:
        return cache
    registros = []
    for evento in historico_separacao(separacao):
        if evento.tipo_evento != TIPO_RESOLUCAO:
            continue
        try:
            dados = json.loads(evento.descricao)
        except (ValueError, TypeError):
            continue
        if not isinstance(dados, dict) or dados.get("versao") != 1 or dados.get("separacao_id") != separacao.pk:
            continue
        if not isinstance(dados.get("item_id"), int) or not isinstance(dados.get("eventos_atendidos"), list):
            continue
        registros.append(dados)
    separacao._resolucoes_quantidade = registros
    return registros


def _assinatura(item):
    atual = item.item_venda
    if not atual or not atual.produto_id:
        return None
    return {"item_id": item.pk, "item_venda_id": atual.pk, "produto_id": atual.produto_id,
            "produto": item.produto_nome_snapshot, "unidade": item.unidade_snapshot,
            "solicitada": str(item.quantidade_solicitada), "separada": str(item.quantidade_separada),
            "quantidade_nota": str(atual.quantidade)}


def _compativel(item, registro=None):
    atual = item.item_venda
    separada = item.quantidade_separada
    return bool(item.status == SeparacaoVendaItem.STATUS_QUANTIDADE_INSUFICIENTE
        and atual and atual.produto_id and (atual.produto.nome == item.produto_nome_snapshot
            or (registro and registro.get("produto_id") == atual.produto_id))
        and atual.unidade == item.unidade_snapshot and not item.observacao
        and separada is not None and separada > 0 and separada < item.quantidade_solicitada
        and atual.quantidade == separada)


def _evidencias_legadas(separacao, itens):
    """Prova conservadora desde a ocorrência física, independente de atendimento global."""
    from .separacao_vendas import (TIPOS_EVENTO_ALTERACAO_FISICA,
        _dados_evento_quantidade_alterada, _dados_evento_item_adicionado,
        _dados_evento_item_removido, _dados_evento_remocao_desfeita)
    from estoque.views import _quantidade_item_checklist_separacao_valida
    ocorrencias = Counter((i.produto_nome_snapshot, i.unidade_snapshot) for i in itens)
    provas = {}
    for item in itens:
        chave = (item.produto_nome_snapshot, item.unidade_snapshot)
        if not _compativel(item) or ocorrencias[chave] != 1:
            continue
        if not _quantidade_item_checklist_separacao_valida(item, item.quantidade_separada):
            continue
        quantidade = item.quantidade_solicitada
        eventos = []
        seguro = True
        for evento in historico_separacao(separacao):
            if evento.criado_em <= item.atualizado_em or evento.tipo_evento not in TIPOS_EVENTO_ALTERACAO_FISICA:
                continue
            if evento.tipo_evento == "quantidade_item_alterada":
                dados = _dados_evento_quantidade_alterada(evento)
                if dados is None:
                    seguro = False
                    break
                if dados["produto"] != chave[0]:
                    continue
                try:
                    anterior = Decimal(dados["quantidade_anterior"].replace(",", "."))
                    nova = Decimal(dados["quantidade_nova"].replace(",", "."))
                except (InvalidOperation, ValueError):
                    seguro = False
                    break
                if (dados["unidade_anterior"] != chave[1] or dados["unidade_nova"] != chave[1]
                        or anterior != quantidade or not 0 < nova < anterior):
                    seguro = False
                    break
                quantidade = nova
                eventos.append(evento.pk)
            else:
                parser = (_dados_evento_item_removido if evento.tipo_evento == "item_removido_da_nota"
                    else _dados_evento_remocao_desfeita if evento.tipo_evento == "remocao_item_desfeita"
                    else _dados_evento_item_adicionado)
                dados = parser(evento)
                if dados is None or dados["produto"] == chave[0]:
                    seguro = False
                    break
        if seguro and eventos and quantidade == item.quantidade_separada:
            provas[item.pk] = eventos
    return provas


def preparar_pendencias(separacao, itens):
    ultimas = {r["item_id"]: r for r in ler_resolucoes(separacao)}
    provas = _evidencias_legadas(separacao, itens)
    separacao._eventos_legados_resolvidos = {pk for ids in provas.values() for pk in ids}
    for item in itens:
        registro = ultimas.get(item.pk)
        assinatura = _assinatura(item)
        item.falta_resolvida = bool(registro and _compativel(item, registro)
            and all(registro.get(k) == v for k, v in assinatura.items()))
        item.resolucao_registrada = item.falta_resolvida
        item.falta_resolvida = item.falta_resolvida or item.pk in provas
        item.eventos_comprovacao = provas.get(item.pk, [])
        item.resolucao_texto = "Quantidade ajustada na venda — resolvido" if item.falta_resolvida else ""
        atual = item.item_venda
        item.quantidades_comparaveis = bool(atual and atual.produto_id
            and (atual.produto.nome == item.produto_nome_snapshot or (registro and registro.get("produto_id") == atual.produto_id))
            and atual.unidade == item.unidade_snapshot)
        item.quantidade_atual_nota = atual.quantidade if atual else None
        item.quantidade_faltante_atual = max(Decimal("0"), (atual.quantidade if atual else item.quantidade_solicitada)
                                             - (item.quantidade_separada or Decimal("0")))
    return itens


def eventos_resolvidos(separacao):
    if not hasattr(separacao, "_eventos_legados_resolvidos"):
        preparar_pendencias(separacao, list(separacao.itens.select_related("item_venda__produto")))
    return ({pk for r in ler_resolucoes(separacao) for pk in r["eventos_atendidos"] if type(pk) is int}
            | separacao._eventos_legados_resolvidos)


def classificar_separacao(separacao, itens=None):
    """Projeção operacional compartilhada. Nunca sincroniza ou grava registros."""
    from .separacao_vendas import (eventos_fisicos_apos_separacao, alteracoes_fisicas_apos_separacao,
                                  item_separacao_tem_pendencia, item_separacao_registra_peso_real)
    # A projeção pode ser solicitada novamente após uma escrita na mesma instância.
    # Não reutilizar provas de uma leitura anterior como se fossem dados atuais.
    for atributo in ("_historico_operacional", "_resolucoes_quantidade", "_eventos_legados_resolvidos"):
        separacao.__dict__.pop(atributo, None)
    if itens is None:
        cache = getattr(separacao, "_prefetched_objects_cache", {}).get("itens")
        itens = list(cache if cache is not None else separacao.itens.select_related("item_venda__produto"))
    preparar_pendencias(separacao, itens)
    eventos = eventos_fisicos_apos_separacao(separacao)
    alteracoes = alteracoes_fisicas_apos_separacao(separacao, eventos)
    motivos = {}
    global_motivo = ""
    interpretados = {a["evento_id"] for a in alteracoes}
    for evento in eventos:
        if evento.pk not in interpretados:
            global_motivo = "Há alteração sem identificação segura. Conferir a nota e os produtos do checklist."
    for alteracao in alteracoes:
        candidatos = [i for i in itens if i.produto_nome_snapshot == alteracao["produto"]
            and alteracao.get("unidade", alteracao.get("unidade_anterior")) in
                {i.unidade_snapshot, i.item_venda.unidade if i.item_venda else None}]
        if len(candidatos) != 1:
            global_motivo = "Há produto acrescentado ou alteração sem vínculo seguro com o checklist. Conferir a nota."
        else:
            motivos[candidatos[0].pk] = "Conferir alteração de quantidade, produto ou unidade registrada após a separação."
    if separacao.revisao_pendente and not eventos:
        global_motivo = "A nota possui revisão pendente sem comprovação de atendimento. Conferir as alterações."
    for item in itens:
        item.motivo_operacional = global_motivo or motivos.get(item.pk, "")
        if (not item.motivo_operacional and item_separacao_registra_peso_real(item)
                and item.item_venda and item.quantidade_separada is not None
                and item.item_venda.quantidade != item.quantidade_separada):
            item.motivo_operacional = "O peso separado difere da quantidade da nota. Ajustar a venda ao peso registrado."
        item.pendencia_operacional = (item.status == SeparacaoVendaItem.STATUS_PENDENTE
            or item_separacao_tem_pendencia(item) or bool(item.motivo_operacional))
        item.situacao_operacional = "conferido"
        falta_ativa = (item_separacao_tem_pendencia(item) and item.quantidades_comparaveis
                       and item.quantidade_faltante_atual > 0)
        if item.motivo_operacional and not falta_ativa:
            item.situacao_operacional = "revisao"
            item.texto_operacional = "Necessita conferência"
        elif item.falta_resolvida:
            item.situacao_operacional = "ajustado"
            item.texto_operacional = item.resolucao_texto
        elif item.status == SeparacaoVendaItem.STATUS_PENDENTE:
            item.situacao_operacional = "pendente"
            item.texto_operacional = "Necessita conferência"
            item.motivo_operacional = "Produto ainda não conferido."
        elif item_separacao_tem_pendencia(item):
            item.situacao_operacional = "falta"
            falta = item.quantidade_faltante_atual
            if item.quantidades_comparaveis and falta > 0:
                from estoque.views import _formatar_quantidade
                item.texto_operacional = f"Quantidade insuficiente — falta {_formatar_quantidade(falta)}"
            else:
                item.texto_operacional = "Necessita conferência"
                item.motivo_operacional = "O histórico não comprova o ajuste deste produto e unidade."
        else:
            item.texto_operacional = "Conferido"
    pendencias = sum(i.pendencia_operacional for i in itens)
    concluida = bool(itens) and pendencias == 0 and not global_motivo and not separacao.venda.cancelada
    # 'enviada' e 'em_separacao' não passam a concluídas apenas por uma projeção vazia.
    concluida = concluida and bool(separacao.finalizado_em or separacao.teve_revisao
        or separacao.revisao_concluida_em or separacao.status == "separada")
    return {"itens": itens, "produtos": len(itens), "resolvidos": len(itens) - pendencias,
        "pendencias": pendencias, "concluida": concluida, "motivo_global": global_motivo,
        "ajustada": concluida and (any(i.falta_resolvida for i in itens) or separacao.teve_revisao),
        "mensagem": "Separação concluída — nota pronta" if concluida else "Separação com pendências",
        "status": "separada" if concluida else "com_pendencia" if pendencias or global_motivo else separacao.status}


def registrar_ajustes_compativeis(venda, identidades_antes, eventos_quantidade, usuario=None):
    """Chamado na transação de salvar a nota, após validações comerciais."""
    from estoque.models import SeparacaoVenda
    separacao = SeparacaoVenda.objects.select_for_update().filter(venda=venda).first()
    if not separacao or not separacao.finalizado_em or venda.cancelada:
        return
    itens = list(separacao.itens.select_for_update(of=("self",)).select_related("item_venda__produto"))
    if not itens:
        return
    preparar_pendencias(separacao, itens)
    from estoque.views import _quantidade_item_checklist_separacao_valida
    for item in itens:
        atual = item.item_venda
        if not _compativel(item):
            continue
        if not _quantidade_item_checklist_separacao_valida(item, item.quantidade_separada):
            continue
        if identidades_antes.get(atual.pk) != (atual.produto_id, atual.produto.nome, atual.unidade):
            continue
        eventos = sorted(set(eventos_quantidade.get(atual.pk, []) + item.eventos_comprovacao))
        # Um reenvio sem alteração não cria prova onde não existe histórico.
        if item.resolucao_registrada or not eventos:
            continue
        if not item.eventos_comprovacao:
            # O salvamento atual fornece IDs por item; não inferir pelo nome em
            # uma nota com linhas homônimas. Ainda bloquear trocas anteriores.
            from .separacao_vendas import (_dados_evento_item_adicionado,
                _dados_evento_item_removido, _dados_evento_remocao_desfeita)
            troca = False
            for evento in historico_separacao(separacao):
                if evento.criado_em <= item.atualizado_em or evento.tipo_evento not in {
                        "item_adicionado_na_nota", "produto_adicionado_na_nota",
                        "item_removido_da_nota", "remocao_item_desfeita"}:
                    continue
                parser = (_dados_evento_item_removido if evento.tipo_evento == "item_removido_da_nota"
                    else _dados_evento_remocao_desfeita if evento.tipo_evento == "remocao_item_desfeita"
                    else _dados_evento_item_adicionado)
                dados_troca = parser(evento)
                if dados_troca is None or dados_troca["produto"] == item.produto_nome_snapshot:
                    troca = True
                    break
            if not eventos_quantidade.get(atual.pk) or troca:
                continue
        dados = {"versao": 1, "separacao_id": separacao.pk, **_assinatura(item),
                 "eventos_atendidos": eventos, "motivo": "quantidade_ajustada_na_venda"}
        EventoVenda.objects.create(venda=venda, tipo_evento=TIPO_RESOLUCAO,
            descricao=json.dumps(dados, ensure_ascii=False, sort_keys=True), canal="sistema",
            usuario=str(usuario or "sistema")[:120])
