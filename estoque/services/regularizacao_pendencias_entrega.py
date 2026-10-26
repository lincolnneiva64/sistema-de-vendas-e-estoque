"""Classificação administrativa reversível; nunca altera fatos comerciais/físicos."""
import hashlib
import hmac
import json
import re
from uuid import UUID, uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction
from django.utils import timezone
from django.utils.dateparse import parse_date

from estoque.models import EntregaChecklistItem, EntregaRota, EntregaRotaItem, EventoVenda, ItemVenda, Venda

REGULARIZADA = "entrega_pendencia_regularizada"
REVERTIDA = "entrega_pendencia_revertida"
TIPOS = (REGULARIZADA, REVERTIDA)


def _json(valor):
    return json.dumps(valor, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(valor):
    return hashlib.sha256(_json(valor).encode("utf-8")).hexdigest()


def _banco():
    return _hash({k: str(connection.settings_dict.get(k, "")) for k in ("ENGINE", "HOST", "PORT", "NAME")})


def identificar_ocorrencia(bloco, checklist=None):
    chave = {"tipo": "item" if checklist else "bloco", "rota_id": bloco.rota_id,
             "bloco_id": bloco.pk, "venda_id": bloco.venda_id}
    if checklist:
        chave.update(checklist_id=checklist.pk, item_venda_id=checklist.item_venda_id)
    identificador = ":".join(str(v) for v in chave.values())
    estado = {"bloco_criado": bloco.criado_em.isoformat(), "status": bloco.status,
              "conferido": bloco.conferido_cliente, "concluida": bloco.entrega_concluida,
              "origem": bloco.origem_pendencia_id, "reentrega": bloco.is_pendencia,
              "observacao": bloco.observacao, "venda_cancelada": bloco.venda.cancelada}
    checklists = [checklist] if checklist else list(bloco.checklist_itens.all())
    estado["checklists"] = [{"id": c.pk, "item": c.item_venda_id,
        "criado": c.criado_em.isoformat(), "atualizado": c.atualizado_em.isoformat(),
        "carregado": c.carregado, "entregue": c.entregue,
        "produto": c.item_venda.produto_id, "quantidade": str(c.item_venda.quantidade),
        "unidade": c.item_venda.unidade} for c in sorted(checklists, key=lambda c: c.pk)]
    # A versão protege a aprovação; a identidade delimita o fato regularizado.
    # Observação, status agregado e atualizações de timestamp não criam outro fato.
    identidade = {k: estado[k] for k in ("bloco_criado", "origem", "reentrega")}
    identidade["checklists"] = [{k: v for k, v in c.items() if k != "atualizado"}
                                 for c in estado["checklists"]]
    ocorrencia = {"chave": chave, "identificador": identificador,
                  "identidade": _hash(identidade), "versao": _hash(estado)}
    if checklist is None:
        # Cancelar o bloco pode trocar a linha por item pela linha geral.
        # Só cobrir essa apresentação se TODOS os itens físicos pendentes
        # continuarem sendo exatamente os itens administrativamente encerrados.
        ocorrencia["cobertura_itens"] = [identificar_ocorrencia(bloco, c)
                                         for c in checklists if not c.entregue]
    return ocorrencia


def _chave_regularizacao(ocorrencia):
    return ocorrencia["identificador"], ocorrencia["identidade"]


def _esta_regularizada(ocorrencia, ativas):
    if _chave_regularizacao(ocorrencia) in ativas:
        return True
    cobertura = ocorrencia.get("cobertura_itens", [])
    return bool(cobertura) and all(_chave_regularizacao(c) in ativas for c in cobertura)


def _ocorrencia_valida(o):
    try:
        c = o["chave"]
        campos = ("tipo", "rota_id", "bloco_id", "venda_id") + (("checklist_id", "item_venda_id") if c["tipo"] == "item" else ())
        return (c["tipo"] in {"item", "bloco"} and set(c) == set(campos)
            and all(type(c[k]) is int and c[k] > 0 for k in campos[1:])
            and o["identificador"] == ":".join(str(c[k]) for k in campos)
            and all(isinstance(o.get(k), str) and re.fullmatch(r"[0-9a-f]{64}", o[k]) is not None
                    for k in ("identidade", "versao")))
    except (KeyError, TypeError, AttributeError):
        return False


def _ler_evento(evento):
    try:
        dados = json.loads(evento.descricao)
        if (isinstance(dados, dict) and dados.get("schema") == 1
                and _ocorrencia_valida(dados.get("ocorrencia"))
                and dados["ocorrencia"]["chave"]["venda_id"] == evento.venda_id
                and all(isinstance(dados["ocorrencia"].get(k), str)
                        for k in ("cliente", "produto", "quantidade", "unidade", "situacao"))
                and isinstance(dados.get("justificativa"), str)):
            return dados
    except (ValueError, TypeError, AttributeError):
        pass
    return None


def _eventos(venda_ids=None):
    qs = EventoVenda.objects.filter(tipo_evento__in=TIPOS)
    if venda_ids is not None:
        qs = qs.filter(venda_id__in=venda_ids)
    return qs.order_by("id")


def _ativas(eventos):
    ativas = {}
    for evento in eventos:
        dados = _ler_evento(evento)
        if dados is None:
            continue
        o = dados["ocorrencia"]
        chave = _chave_regularizacao(o)
        if evento.tipo_evento == REGULARIZADA:
            ativas[chave] = evento
        elif chave in ativas and dados.get("regularizacao_id") == ativas[chave].pk:
            del ativas[chave]
    return ativas


def filtrar_regularizadas(pendencias):
    if not pendencias:
        return pendencias
    ativas = _ativas(_eventos({p["venda"].pk for p in pendencias}))
    return [p for p in pendencias if not _esta_regularizada(p["ocorrencia_administrativa"], ativas)]


def consultar(identificadores=None):
    """Somente SELECT. Nenhuma suposição sobre quantidade, data ou venda."""
    from estoque.views import listar_pendencias_entrega
    pendencias = listar_pendencias_entrega(incluir_regularizadas=True)
    ativas = _ativas(_eventos({p["venda"].pk for p in pendencias}))
    selecionadas = set(identificadores or [])
    registros = []
    for p in pendencias:
        o = p["ocorrencia_administrativa"]
        if _esta_regularizada(o, ativas):
            continue
        if selecionadas and o["identificador"] not in selecionadas:
            continue
        registros.append({**o, "rota": p["rota"].pk, "venda": p["venda"].pk,
            "cliente": p["cliente"], "produto": p["produto"], "quantidade": str(p["quantidade"]),
            "unidade": p["unidade"], "situacao": p["status"]})
    if selecionadas - {r["identificador"] for r in registros}:
        raise ValidationError("Há identificadores que não estão abertos. Consulte novamente.")
    manifesto = {"schema": 1, "banco": _banco(), "lote": str(uuid4()),
        "gerado_em": timezone.now().isoformat(), "total": len(registros), "ocorrencias": registros}
    manifesto["hash"] = _hash(manifesto)
    return manifesto


def _autor(usuario, justificativa):
    autor = get_user_model().objects.filter(pk=getattr(usuario, "pk", None), is_active=True, is_superuser=True).first()
    if autor is None:
        raise PermissionDenied("Regularização administrativa exige administrador ativo.")
    motivo = str(justificativa or "").strip()
    if not motivo or len(motivo) > 4000:
        raise ValidationError("Informe uma justificativa de até 4000 caracteres.")
    return motivo, autor


def _validar_manifesto(manifesto, confirmacao):
    try:
        corpo = {k: v for k, v in manifesto.items() if k != "hash"}
        digest = _hash(corpo)
        if not hmac.compare_digest(digest, str(manifesto.get("hash", ""))):
            raise ValueError()
        if confirmacao is not None and not hmac.compare_digest(digest, str(confirmacao)):
            raise ValueError()
        UUID(manifesto["lote"])
        registros = manifesto["ocorrencias"]
        if (manifesto["schema"] != 1 or manifesto["banco"] != _banco()
                or not registros or manifesto["total"] != len(registros)
                or len({r["identificador"] for r in registros}) != len(registros)):
            raise ValueError()
        for r in registros:
            c = r["chave"]
            if (not _ocorrencia_valida(r) or r.get("rota") != c["rota_id"] or r.get("venda") != c["venda_id"]
                    or any(not isinstance(r.get(k), str) for k in ("cliente", "produto", "quantidade", "unidade", "situacao"))):
                raise ValueError()
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ValidationError("Manifesto inválido, alterado ou de outro banco. Consulte e confira novamente.")
    return registros


def _bloquear(registros):
    if connection.vendor == "postgresql":
        # A leitura do ledger após a espera precisa enxergar o commit anterior.
        with connection.cursor() as cursor:
            cursor.execute("SHOW transaction_isolation")
            if cursor.fetchone()[0] != "read committed":
                raise ValidationError("Operações administrativas exigem isolamento READ COMMITTED.")
    vendas = sorted({r["chave"]["venda_id"] for r in registros})
    blocos = sorted({r["chave"]["bloco_id"] for r in registros})
    if len(list(Venda.objects.select_for_update().filter(pk__in=vendas).order_by("pk"))) != len(vendas):
        raise ValidationError("Uma venda selecionada não existe mais.")
    list(ItemVenda.objects.select_for_update().filter(venda_id__in=vendas).order_by("pk"))
    if len(list(EntregaRotaItem.objects.select_for_update().filter(pk__in=blocos).order_by("pk"))) != len(blocos):
        raise ValidationError("Um bloco selecionado não existe mais.")
    list(EntregaChecklistItem.objects.select_for_update().filter(rota_item_id__in=blocos).order_by("pk"))


def validar_reprogramacao(checklist_ids):
    """Revalida sob os mesmos locks da regularização, antes de criar outra rota."""
    if not connection.in_atomic_block:
        raise ValidationError("Reprogramação deve ser validada dentro da transação.")
    ids = set(checklist_ids)
    if not ids:
        return True
    registros = [{"chave": {"venda_id": c.rota_item.venda_id, "bloco_id": c.rota_item_id}}
        for c in EntregaChecklistItem.objects.filter(pk__in=ids).select_related("rota_item")]
    if len(registros) != len(ids):
        return False
    try:
        _bloquear(registros)
    except ValidationError:
        return False
    from estoque.views import pendencias_checklist_validas
    return {c.pk for c in pendencias_checklist_validas(ids)} == ids


def regularizar(manifesto, *, usuario, justificativa, executar=False, confirmacao=None):
    motivo, usuario = _autor(usuario, justificativa)
    registros = _validar_manifesto(manifesto, confirmacao)
    if executar and confirmacao is None:
        raise ValidationError("A execução exige o hash aprovado do manifesto.")
    from estoque.views import listar_pendencias_entrega
    with transaction.atomic():
        if executar:
            _bloquear(registros)
        atuais = {p["ocorrencia_administrativa"]["identificador"]: p["ocorrencia_administrativa"]
                  for p in listar_pendencias_entrega(incluir_regularizadas=True)}
        ativas = _ativas(_eventos({r["chave"]["venda_id"] for r in registros}))
        lote_anterior = EventoVenda.objects.filter(tipo_evento=REGULARIZADA,
            descricao__contains=f'"lote":"{manifesto["lote"]}"')
        for evento in lote_anterior:
            dados = _ler_evento(evento)
            if dados is None or dados.get("manifesto_hash") != manifesto["hash"]:
                raise ValidationError("Identificador de lote já utilizado para outro manifesto.")
            o = dados["ocorrencia"]
            ativa = ativas.get(_chave_regularizacao(o))
            if ativa is None or ativa.pk != evento.pk:
                raise ValidationError("O lote já sofreu reversão. Uma nova aprovação exige novo manifesto.")
        resultado = []
        # Validação integral antes de registrar qualquer evento do lote.
        for r in registros:
            atual = atuais.get(r["identificador"])
            if (not atual or atual["chave"] != r["chave"]
                    or atual["identidade"] != r["identidade"] or atual["versao"] != r["versao"]):
                raise ValidationError("Uma ocorrência mudou ou deixou de existir. Nenhuma regularização foi aplicada.")
        for r in registros:
            anterior = ativas.get(_chave_regularizacao(r))
            evento_id = anterior.pk if anterior else None
            if executar and anterior is None:
                dados = {"schema": 1, "lote": manifesto["lote"], "manifesto_hash": manifesto["hash"],
                    "ocorrencia": r, "justificativa": motivo, "usuario_id": usuario.pk}
                evento = EventoVenda.objects.create(venda_id=r["chave"]["venda_id"], tipo_evento=REGULARIZADA,
                    descricao=_json(dados), canal="administrativo", usuario=usuario.get_username()[:120])
                evento_id = evento.pk
            resultado.append({"identificador": r["identificador"], "evento_id": evento_id,
                "situacao": "já regularizada" if anterior else "regularizada" if executar else "prevista"})
    return {"executado": executar, "lote": manifesto["lote"], "ocorrencias": resultado}


def reverter(evento_ids, *, usuario, justificativa, executar=False, confirmacao=None):
    motivo, usuario = _autor(usuario, justificativa)
    if not evento_ids or any(type(pk) is not int or pk <= 0 for pk in evento_ids):
        raise ValidationError("Selecione IDs de eventos de regularização válidos.")
    ids = sorted(set(evento_ids))
    digest = _hash({"banco": _banco(), "eventos": ids})
    if executar and (confirmacao is None or not hmac.compare_digest(digest, str(confirmacao))):
        raise ValidationError("A reversão exige o hash da simulação dos eventos selecionados.")
    with transaction.atomic():
        originais = list(EventoVenda.objects.filter(pk__in=ids, tipo_evento=REGULARIZADA).order_by("pk"))
        dados = [_ler_evento(e) for e in originais]
        if len(originais) != len(ids) or any(d is None for d in dados):
            raise ValidationError("Evento de regularização inexistente ou inválido.")
        registros = [d["ocorrencia"] for d in dados]
        if executar:
            _bloquear(registros)
        ativas = _ativas(_eventos({e.venda_id for e in originais}))
        resultado = []
        for evento, d in zip(originais, dados):
            o = d["ocorrencia"]
            ativa = ativas.get(_chave_regularizacao(o))
            pendente = ativa is not None and ativa.pk == evento.pk
            if executar and pendente:
                EventoVenda.objects.create(venda_id=evento.venda_id, tipo_evento=REVERTIDA,
                    descricao=_json({"schema": 1, "regularizacao_id": evento.pk, "ocorrencia": o,
                        "justificativa": motivo, "usuario_id": usuario.pk}),
                    canal="administrativo", usuario=usuario.get_username()[:120])
            resultado.append({"evento_id": evento.pk, "reversivel": pendente})
    return {"executado": executar, "confirmacao": digest, "eventos": resultado}


def descricao_evento(descricao, tipo):
    try:
        dados = json.loads(descricao)
        identificador = dados["ocorrencia"]["identificador"]
        acao = "Regularização administrativa de pendência de entrega" if tipo == REGULARIZADA else "Reversão administrativa de pendência de entrega"
        return f"{acao}: {identificador}. Justificativa: {dados['justificativa']}"
    except (ValueError, TypeError, KeyError):
        return "Registro administrativo de pendência de entrega."


def listar_regularizadas(filtros=None):
    """Histórico administrativo na consulta existente, sem fingir remoção da nota."""
    filtros = filtros or {}
    eventos = _ativas(_eventos().select_related("venda__cliente")).values()
    rotas = EntregaRota.objects.in_bulk({json.loads(e.descricao)["ocorrencia"]["chave"]["rota_id"] for e in eventos})
    resultado = []
    inicial = parse_date(filtros.get("data_inicial") or "")
    final = parse_date(filtros.get("data_final") or "")
    for evento in eventos:
        dados = _ler_evento(evento)
        o = dados["ocorrencia"]
        cliente = evento.venda.cliente.nome if evento.venda.cliente else "Consumidor"
        if filtros.get("cliente", "").casefold() not in cliente.casefold():
            continue
        if filtros.get("produto", "").casefold() not in o.get("produto", "").casefold():
            continue
        venda_filtro = filtros.get("venda", "")
        if venda_filtro.isdigit() and int(venda_filtro) != evento.venda_id:
            continue
        data = timezone.localtime(evento.criado_em).date()
        if (inicial and data < inicial) or (final and data > final):
            continue
        rota_id = o["chave"]["rota_id"]
        resultado.append({"id": evento.pk, "venda": evento.venda, "cliente": cliente,
            "rota": rotas.get(rota_id), "rota_id": rota_id, "data": evento.criado_em,
            "produto": o.get("produto", ""), "quantidade": o.get("quantidade", ""),
            "unidade": o.get("unidade", ""), "status": "Regularizada administrativamente",
            "resumo_resolucao": dados["justificativa"], "administrativa": True})
    return resultado
