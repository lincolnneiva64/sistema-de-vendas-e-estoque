"""Resoluções operacionais por item, mantendo os snapshots da separação."""
import json
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation

from estoque.models import EventoVenda, SeparacaoVendaItem

TIPO_RESOLUCAO = "separacao_quantidade_ajustada"


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
    for evento in EventoVenda.objects.filter(venda_id=separacao.venda_id, tipo_evento=TIPO_RESOLUCAO).order_by("id"):
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


def preparar_pendencias(separacao, itens):
    ultimas = {r["item_id"]: r for r in ler_resolucoes(separacao)}
    for item in itens:
        registro = ultimas.get(item.pk)
        assinatura = _assinatura(item)
        item.falta_resolvida = bool(registro and _compativel(item, registro)
            and all(registro.get(k) == v for k, v in assinatura.items()))
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
    return {pk for r in ler_resolucoes(separacao) for pk in r["eventos_atendidos"] if type(pk) is int}


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
    from .separacao_vendas import (eventos_fisicos_apos_separacao, _dados_evento_quantidade_alterada,
                                  _dados_evento_item_adicionado, _dados_evento_item_removido)
    eventos_anteriores = eventos_fisicos_apos_separacao(separacao)
    trocas = []
    for evento in EventoVenda.objects.filter(venda_id=venda.pk,
            tipo_evento__in=["item_adicionado_na_nota", "produto_adicionado_na_nota", "item_removido_da_nota", "remocao_item_desfeita"],
            criado_em__gt=min(i.atualizado_em for i in itens)):
        dados_troca = (_dados_evento_item_removido(evento) if evento.tipo_evento == "item_removido_da_nota"
                       else _dados_evento_item_adicionado(evento))
        trocas.append((evento.criado_em, dados_troca))
    ocorrencias = Counter((i.produto_nome_snapshot, i.unidade_snapshot) for i in itens)
    anteriores_por_produto = defaultdict(list)
    for evento in eventos_anteriores:
        dados_evento = _dados_evento_quantidade_alterada(evento) if evento.tipo_evento == "quantidade_item_alterada" else None
        if not dados_evento or dados_evento["unidade_anterior"] != dados_evento["unidade_nova"]:
            continue
        try:
            nova = Decimal(dados_evento["quantidade_nova"].replace(",", "."))
            anterior = Decimal(dados_evento["quantidade_anterior"].replace(",", "."))
        except (InvalidOperation, ValueError):
            continue
        if anterior >= nova:
            anteriores_por_produto[(dados_evento["produto"], dados_evento["unidade_nova"])].append((nova, evento.pk))
    from estoque.views import _quantidade_item_checklist_separacao_valida
    for item in itens:
        atual = item.item_venda
        if not _compativel(item):
            continue
        if any(data > item.atualizado_em and (dados is None
                or (dados["produto"] == item.produto_nome_snapshot and dados["unidade"] == item.unidade_snapshot))
                for data, dados in trocas):
            continue  # O snapshot legado não possui ID do produto: não inferir uma troca anterior.
        if not _quantidade_item_checklist_separacao_valida(item, item.quantidade_separada):
            continue
        antes = identidades_antes.get(atual.pk)
        if antes != (atual.produto_id, atual.produto.nome, atual.unidade):
            continue  # Troca de produto/unidade exige conferência física.
        eventos = list(eventos_quantidade.get(atual.pk, []))
        # Correções salvas antes desta versão também podem ser reconhecidas.
        # Descrição ambígua ou produtos homônimos não são atendidos por inferência.
        chave = (item.produto_nome_snapshot, item.unidade_snapshot)
        if ocorrencias[chave] == 1:
            eventos.extend(pk for nova, pk in anteriores_por_produto[chave] if nova == item.quantidade_separada)
        eventos = sorted(set(eventos))
        if item.falta_resolvida and not eventos:
            continue  # Reenvio sem mudança não duplica resolução.
        dados = {"versao": 1, "separacao_id": separacao.pk, **_assinatura(item),
                 "eventos_atendidos": eventos, "motivo": "quantidade_ajustada_na_venda"}
        EventoVenda.objects.create(venda=venda, tipo_evento=TIPO_RESOLUCAO,
            descricao=json.dumps(dados, ensure_ascii=False, sort_keys=True), canal="sistema",
            usuario=str(usuario or "sistema")[:120])
