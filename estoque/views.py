import copy
import json
import logging
import mimetypes
import os
import re
import sys
import textwrap
import time
import unicodedata
import ipaddress
from difflib import SequenceMatcher
from io import BytesIO
from decimal import Decimal, InvalidOperation
from pathlib import Path
from datetime import date, timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q, Sum, Max, Prefetch
from django.db.models.functions import Coalesce
from urllib.parse import quote, urlencode
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.db.models import Case, When, Value, IntegerField, F, Count, DecimalField, ExpressionWrapper
from .forms import CategoriaForm, ClienteForm, FornecedorContatoFormSet, FornecedorForm, FuncionarioForm, MeioPagamentoForm, PixRecebidoCorrecaoForm, PixRecebidoForm, ProdutoForm, UnidadeForm
from .models import AjusteItemVendaQuitada, Categoria, Cliente, ContaFinanceira, ContaPagar, ContaReceber, CreditoCliente, DespesaDiaria, EmprestimoDivida, EmprestimoRapido, EntregaChecklistItem, EntregaRota, EntregaRotaItem, EventoVenda, Fornecedor, FornecedorContato, Funcionario, MeioPagamento, MovimentoFinanceiro, Compra, ItemCompra, ItemVenda, ItemVendaRemovido, PagamentoContaPagar, PagamentoEmprestimoDivida, PixRecebido, Produto, ProdutoFornecedor, RecebimentoContaReceber, Unidade, Venda
from .utils_pix import OCR_RENDER_MODO_LEVE, analisar_comprovante_pix, analisar_comprovante_pix_google_vision
from django.contrib import messages
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.utils.dateparse import parse_date, parse_datetime
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST
from PIL import Image, ImageDraw, ImageFont
from uuid import uuid4
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

PIX_OCR_PENDENTE_MOBILE = (
    "OCR nao executado automaticamente no envio mobile para evitar timeout. "
    "Use processamento manual posteriormente."
)
PIX_OCR_MANUAL_ERRO_RENDER = (
    "[OCR erro]\n"
    "OCR nao conseguiu ler todos os dados. Confira manualmente.\n"
    "Comprovante preservado para conferencia manual."
)

MENSAGEM_CLIENTE_DUPLICADO = (
    "Ja existe um cliente parecido cadastrado. Verifique antes de cadastrar novamente."
)


def _tem_pix_em_atencao():
    return PixRecebido.objects.filter(
        status__in=[
            PixRecebido.STATUS_PENDENTE,
            PixRecebido.STATUS_NAO_IDENTIFICADO,
            PixRecebido.STATUS_POSSIVEL_DUPLICADO,
        ]
    ).exists()


def _pix_envio_url_padrao(request):
    return request.build_absolute_uri(reverse("estoque:central_pix_enviar_comprovante"))


def _mobile_url_configurada(nome_config, fallback):
    return (getattr(settings, nome_config, "") or fallback or "").strip()


def _host_configurado(url):
    try:
        return urlparse(url).netloc.lower()
    except ValueError:
        return ""


def _ambiente_envio_pix(request):
    host_atual = request.get_host().split(":")[0].strip().lower()
    host_local = _host_configurado(getattr(settings, "PIX_LOCAL_URL", "")).split(":")[0]
    host_online = _host_configurado(getattr(settings, "PIX_ONLINE_URL", "")).split(":")[0]
    try:
        ip_atual = ipaddress.ip_address(host_atual)
    except ValueError:
        ip_atual = None
    if host_atual in {"localhost", "127.0.0.1"} or (ip_atual and (ip_atual.is_private or ip_atual.is_loopback)):
        return "LOCAL / Wi-Fi"
    if host_local and host_atual == host_local:
        return "LOCAL / Wi-Fi"
    if host_atual.endswith(".onrender.com") or host_atual == "onrender.com":
        return "ONLINE / Render"
    if host_online and host_atual == host_online:
        return "ONLINE / Render"
    return "AMBIENTE NÃO IDENTIFICADO"


def _diagnostico_storage_seguro():
    access_key_id = getattr(settings, "CLOUDFLARE_R2_ACCESS_KEY_ID", "") or ""
    secret_access_key = getattr(settings, "CLOUDFLARE_R2_SECRET_ACCESS_KEY", "") or ""
    return {
        "access_key_id_exists": bool(access_key_id),
        "access_key_id_length": len(access_key_id),
        "secret_access_key_exists": bool(secret_access_key),
        "secret_access_key_length": len(secret_access_key),
        "access_key_id_stripped": getattr(settings, "CLOUDFLARE_R2_ACCESS_KEY_ID_STRIPPED", False),
        "secret_access_key_stripped": getattr(settings, "CLOUDFLARE_R2_SECRET_ACCESS_KEY_STRIPPED", False),
        "cloudflare_r2_bucket": getattr(settings, "CLOUDFLARE_R2_BUCKET_NAME", ""),
        "cloudflare_r2_endpoint": getattr(settings, "CLOUDFLARE_R2_ENDPOINT_URL", ""),
        "cloudflare_r2_region": getattr(settings, "CLOUDFLARE_R2_REGION_NAME", ""),
    }


def _nome_arquivo_seguro(arquivo):
    return Path(getattr(arquivo, "name", "") or "").name


def _salvar_falha_ocr_manual(pix, resumo=""):
    resumo = str(resumo or "").strip()
    texto = PIX_OCR_MANUAL_ERRO_RENDER
    if resumo:
        texto = f"{texto}\nResumo: {resumo[:180]}"
    pix.texto_ocr_bruto = texto
    pix.save(update_fields=["texto_ocr_bruto", "atualizado_em"])


def _pix_ocr_local_fallback_permitido():
    return _bool_config_ativa(os.getenv("PIX_PERMITIR_OCR_LOCAL_FALLBACK", ""))


def _resultado_google_vision_indisponivel(motivo):
    detalhe = str(motivo or "Google Vision nao configurado").strip()
    texto = f"[Google Vision indisponivel]\n{detalhe[:500]}"
    return {
        "ok": False,
        "pagador": "",
        "valor": "",
        "data_pagamento": "",
        "instituicao_pix": "",
        "debug_data_pagamento": "Data enviada ao frontend: nao reconhecida",
        "texto_extraido": "",
        "texto_ocr_bruto": texto,
        "mensagem": "Leitura automatica nao realizada. Configure o Google Vision ou preencha os dados manualmente.",
        "google_vision_erro": True,
        "google_vision_configurado": False,
    }


def _arquivo_pix_eh_texto(arquivo):
    nome = (getattr(arquivo, "name", "") or "").lower()
    content_type = (getattr(arquivo, "content_type", "") or "").lower()
    return content_type.startswith("text/") or nome.endswith(".txt")


def _analisar_comprovante_pix_principal(arquivo, debug_prefix=None):
    if _arquivo_pix_eh_texto(arquivo):
        logger.info("Leitura automatica Pix usando texto enviado, sem OCR local de imagem.")
        return analisar_comprovante_pix(arquivo, debug_prefix=debug_prefix)

    usar_vision = pix_google_vision_habilitado()
    if not usar_vision:
        if _pix_ocr_local_fallback_permitido():
            logger.warning("OCR local fallback permitido por PIX_PERMITIR_OCR_LOCAL_FALLBACK; usando OCR local.")
            return analisar_comprovante_pix(arquivo, debug_prefix=debug_prefix)
        logger.warning("Leitura automatica Pix nao realizada: Google Vision nao configurado e fallback local desativado.")
        return _resultado_google_vision_indisponivel("Google Vision nao configurado e fallback local desativado.")

    logger.info("Leitura automatica Pix usando Google Vision.")
    dados = analisar_comprovante_pix_google_vision(arquivo)
    if dados.get("google_vision_erro") and _pix_ocr_local_fallback_permitido():
        logger.warning("Google Vision falhou; usando OCR local fallback permitido por PIX_PERMITIR_OCR_LOCAL_FALLBACK.")
        return analisar_comprovante_pix(arquivo, debug_prefix=debug_prefix)
    if dados.get("google_vision_erro"):
        logger.warning("Leitura automatica Pix nao realizada por falha do Google Vision; fallback local desativado.")
    return dados


def _bool_config_ativa(valor):
    if isinstance(valor, bool):
        return valor
    return str(valor or "").strip().lower() in {"1", "true", "sim", "yes", "on"}


def _mensagem_unica(request, nivel, texto):
    mensagens_pendentes = list(messages.get_messages(request))
    for mensagem in mensagens_pendentes:
        if str(mensagem) != texto:
            messages.add_message(request, mensagem.level, str(mensagem), extra_tags=mensagem.extra_tags)
    messages.add_message(request, nivel, texto)


def pix_google_vision_habilitado():
    valor_settings = getattr(settings, "PIX_USAR_GOOGLE_VISION", None)
    if valor_settings is not None:
        return _bool_config_ativa(valor_settings)
    if "test" in sys.argv:
        return False
    valor_env = os.getenv("PIX_USAR_GOOGLE_VISION")
    if valor_env is not None:
        return _bool_config_ativa(valor_env)
    return any(
        str(os.getenv(nome, "")).strip()
        for nome in (
            "GOOGLE_APPLICATION_CREDENTIALS_JSON",
            "GOOGLE_APPLICATION_CREDENTIALS_BASE64",
            "GOOGLE_APPLICATION_CREDENTIALS",
        )
    )


def _pix_duplicado_pendente(dados):
    nome_pagador = normalizar_texto_cliente(dados.get("nome_pagador"))
    valor = dados.get("valor")
    data_pagamento = dados.get("data_pagamento")
    if not (nome_pagador and valor and data_pagamento):
        return None

    inicio = data_pagamento - timedelta(minutes=5)
    fim = data_pagamento + timedelta(minutes=5)
    candidatos = PixRecebido.objects.filter(
        status=PixRecebido.STATUS_PENDENTE,
        valor=valor,
        data_pagamento__range=(inicio, fim),
    ).only("id", "nome_pagador", "valor", "data_pagamento")
    for pix in candidatos:
        if normalizar_texto_cliente(pix.nome_pagador) == nome_pagador:
            return pix
    return None


def _pix_duplicado_baixado(dados, excluir_pix_id=None, texto_ocr_bruto=""):
    identificador = _identificador_pix_texto(texto_ocr_bruto or dados.get("texto_ocr_bruto"))
    candidatos = PixRecebido.objects.filter(status=PixRecebido.STATUS_BAIXADO).order_by("-criado_em", "-id")
    if excluir_pix_id:
        candidatos = candidatos.exclude(pk=excluir_pix_id)

    if identificador:
        for pix in candidatos.only("id", "texto_ocr_bruto", "criado_em"):
            if _identificador_pix_texto(pix.texto_ocr_bruto) == identificador:
                return pix

    nome_pagador = dados.get("nome_pagador") or dados.get("pagador") or ""
    valor = dados.get("valor")
    data_pagamento = dados.get("data_pagamento")
    instituicao_pix = dados.get("instituicao_pix") or ""
    if not (nome_pagador and valor and data_pagamento):
        return None

    for pix in candidatos.filter(valor=valor).only(
        "id",
        "nome_pagador",
        "valor",
        "data_pagamento",
        "instituicao_pix",
        "criado_em",
    ):
        if not _mesmo_minuto(data_pagamento, pix.data_pagamento):
            continue
        if not _instituicao_pix_compativel(instituicao_pix, pix.instituicao_pix):
            continue
        if (
            normalizar_texto_cliente(nome_pagador) == normalizar_texto_cliente(pix.nome_pagador)
            or textos_parecidos_cliente(nome_pagador, pix.nome_pagador, minimo=0.90)
        ):
            return pix
    return None


def _tokens_nome_pix(valor):
    texto = normalizar_texto_cliente(valor)
    texto = re.sub(r"[^a-z0-9\s]", " ", texto)
    tokens = []
    equivalencias = {"jr": "junior", "jrn": "junior"}
    ignorar = {"de", "da", "do", "das", "dos", "e"}
    for token in texto.split():
        token = equivalencias.get(token, token)
        if token and token not in ignorar:
            tokens.append(token)
    return tokens


def _termos_nome_parecidos(termo_a, termo_b):
    return termo_a == termo_b or SequenceMatcher(None, termo_a, termo_b).ratio() >= 0.90


def _nome_cliente_parece_pagador_pix(nome_pagador, nome_cliente):
    tokens_pagador = _tokens_nome_pix(nome_pagador)
    tokens_cliente = _tokens_nome_pix(nome_cliente)
    if len(tokens_pagador) < 2 or len(tokens_cliente) < 2:
        return False

    termos_encontrados = 0
    for termo_pagador in tokens_pagador:
        if any(_termos_nome_parecidos(termo_pagador, termo_cliente) for termo_cliente in tokens_cliente):
            termos_encontrados += 1

    if termos_encontrados >= 3:
        return True

    if _termos_nome_parecidos(tokens_pagador[0], tokens_cliente[0]):
        termos_cliente_restantes = tokens_cliente[1:]
        termos_pagador_restantes = tokens_pagador[1:]
        termos_restantes_encontrados = 0
        for termo_cliente in termos_cliente_restantes:
            if any(_termos_nome_parecidos(termo_cliente, termo_pagador) for termo_pagador in termos_pagador_restantes):
                termos_restantes_encontrados += 1
        return termos_restantes_encontrados >= 1

    return False


def _sugerir_cliente_por_pagador(nome_pagador):
    pagador_normalizado = normalizar_texto_cliente(nome_pagador)
    if len(_tokens_nome_pix(nome_pagador)) < 2:
        return None, "baixa", ""

    clientes_parecidos = []
    clientes = Cliente.objects.filter(ativo=True).only("id", "nome", "apelido_nome_conhecido").order_by("nome")
    for cliente in clientes:
        nome_parecido = _nome_cliente_parece_pagador_pix(pagador_normalizado, cliente.nome)
        apelido_parecido = bool(cliente.apelido_nome_conhecido) and _nome_cliente_parece_pagador_pix(
            pagador_normalizado,
            cliente.apelido_nome_conhecido,
        )
        if nome_parecido or apelido_parecido:
            clientes_parecidos.append(cliente)
            if len(clientes_parecidos) > 1:
                return None, "ambigua", "Encontramos mais de um cliente parecido. Use a busca para escolher manualmente."

    if clientes_parecidos:
        return clientes_parecidos[0], "alta", ""

    return None, "baixa", ""


def _normalizar_nome_pix_exato(valor):
    texto = normalizar_texto_cliente(valor)
    texto = re.sub(r"[^a-z0-9\s]", " ", texto)
    return " ".join(texto.split())


def _cliente_exato_por_pagador_pix(nome_pagador):
    pagador_normalizado = _normalizar_nome_pix_exato(nome_pagador)
    if len(_tokens_nome_pix(nome_pagador)) < 2:
        return None

    encontrados = []
    clientes = Cliente.objects.filter(ativo=True).only("id", "nome", "apelido_nome_conhecido").order_by("nome")
    for cliente in clientes:
        nomes_cliente = [
            _normalizar_nome_pix_exato(cliente.nome),
            _normalizar_nome_pix_exato(cliente.apelido_nome_conhecido),
        ]
        if pagador_normalizado and pagador_normalizado in nomes_cliente:
            encontrados.append(cliente)
            if len(encontrados) > 1:
                return None

    return encontrados[0] if encontrados else None


def _decimal_pix_lido(valor):
    try:
        return Decimal(str(valor or "0").replace(",", ".")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return Decimal("0.00")


def _data_pix_lida(valor):
    data = parse_datetime(valor or "")
    if data is None:
        return None
    if timezone.is_naive(data):
        data = timezone.make_aware(data, timezone.get_current_timezone())
    return data


def _nome_envio_pix_mobile(post):
    enviado_por = (post.get("enviado_por") or "").strip()
    if enviado_por in {"Lincoln", "Roseli"}:
        return enviado_por
    if enviado_por == "Outro":
        outro_nome = " ".join((post.get("enviado_por_outro") or "").strip().split())
        return outro_nome[:80] or "Outro"
    return ""


def _identificador_pix_texto(texto):
    texto_normalizado = str(texto or "").upper()
    encontrados = re.findall(r"\bE\d{8}[A-Z0-9]{10,40}\b", texto_normalizado)
    return encontrados[0] if encontrados else ""


def _mesmo_minuto(data_a, data_b):
    if not data_a or not data_b:
        return False
    data_a = timezone.localtime(data_a) if timezone.is_aware(data_a) else data_a
    data_b = timezone.localtime(data_b) if timezone.is_aware(data_b) else data_b
    return data_a.replace(second=0, microsecond=0) == data_b.replace(second=0, microsecond=0)


def _instituicao_pix_compativel(nova_instituicao, instituicao_existente):
    nova = normalizar_texto_cliente(nova_instituicao)
    existente = normalizar_texto_cliente(instituicao_existente)
    return not nova or not existente or nova == existente


def _detectar_pix_duplicado_comprovante(dados, valor, data_pagamento, texto_ocr_bruto):
    identificador = _identificador_pix_texto(texto_ocr_bruto)
    candidatos = PixRecebido.objects.exclude(status=PixRecebido.STATUS_IGNORADO).annotate(
        prioridade_duplicidade=Case(
            When(status=PixRecebido.STATUS_BAIXADO, then=Value(0)),
            default=Value(1),
            output_field=IntegerField(),
        )
    ).order_by("prioridade_duplicidade", "-criado_em", "-id")
    if identificador:
        for pix in candidatos.only("id", "texto_ocr_bruto", "criado_em"):
            if _identificador_pix_texto(pix.texto_ocr_bruto) == identificador:
                return pix

    pagador = dados.get("pagador") or ""
    if not (pagador and valor > 0 and data_pagamento):
        return None

    candidatos = candidatos.filter(valor=valor)
    for pix in candidatos.only("id", "nome_pagador", "valor", "data_pagamento", "instituicao_pix", "criado_em"):
        if not _mesmo_minuto(data_pagamento, pix.data_pagamento):
            continue
        if not _instituicao_pix_compativel(dados.get("instituicao_pix"), pix.instituicao_pix):
            continue
        if (
            normalizar_texto_cliente(pagador) == normalizar_texto_cliente(pix.nome_pagador)
            or textos_parecidos_cliente(pagador, pix.nome_pagador, minimo=0.90)
        ):
            return pix
    return None


def normalizar_texto_cliente(valor):
    texto = " ".join(str(valor or "").strip().lower().split())
    texto = unicodedata.normalize("NFD", texto)
    return "".join(caractere for caractere in texto if unicodedata.category(caractere) != "Mn")


def normalizar_documento_cliente(valor):
    return "".join(caractere for caractere in str(valor or "") if caractere.isdigit())


def _valor_total_recebimento_cliente(recebimento):
    if isinstance(recebimento, dict):
        valor_recebimento = recebimento.get("valor") or Decimal("0.00")
        observacao = recebimento.get("observacao") or ""
    else:
        valor_recebimento = recebimento.valor or Decimal("0.00")
        observacao = recebimento.observacao or ""
    if "Total recebido:" not in observacao:
        return valor_recebimento.quantize(Decimal("0.01"))

    trecho_total = observacao.split("Total recebido:", 1)[1]
    trecho_total = trecho_total.split("Aplicado nesta conta:", 1)[0].strip().rstrip(".")
    try:
        return _decimal_do_front(trecho_total or valor_recebimento, "0.01")
    except ValueError:
        return valor_recebimento.quantize(Decimal("0.01"))


def _contas_receber_abertas_cliente_qs(cliente_id, hoje, bloquear=False):
    qs = ContaReceber.objects
    if bloquear:
        qs = qs.select_for_update()
    return (
        qs.filter(
            cliente_id=cliente_id,
            status__in=[ContaReceber.STATUS_ABERTA, ContaReceber.STATUS_PARCIAL],
            valor_em_aberto__gt=Decimal("0.00"),
        )
        .only(
            "id",
            "venda_id",
            "cliente_id",
            "data_emissao",
            "data_vencimento",
            "valor_original",
            "valor_em_aberto",
            "status",
        )
        .annotate(
            ordem_vencida=Case(
                When(data_vencimento__lt=hoje, then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            ),
            ordem_sem_vencimento=Case(
                When(data_vencimento__isnull=True, then=Value(1)),
                default=Value(0),
                output_field=IntegerField(),
            ),
        )
        .order_by("ordem_vencida", "ordem_sem_vencimento", "data_vencimento", "data_emissao", "id")
    )


def textos_parecidos_cliente(valor_a, valor_b, minimo=0.88):
    texto_a = normalizar_texto_cliente(valor_a)
    texto_b = normalizar_texto_cliente(valor_b)
    if not texto_a or not texto_b:
        return False
    return SequenceMatcher(None, texto_a, texto_b).ratio() >= minimo


def detectar_cliente_duplicado(cliente):
    candidatos = Cliente.objects.all()
    if cliente.pk:
        candidatos = candidatos.exclude(pk=cliente.pk)

    documento = normalizar_documento_cliente(cliente.cpf_cnpj)
    if documento:
        duplicado = None
        for candidato in candidatos.only("id", "nome", "cpf_cnpj"):
            if normalizar_documento_cliente(candidato.cpf_cnpj) == documento:
                duplicado = candidato
                break
        if duplicado:
            return duplicado, "cpf_cnpj"

    whatsapp_normalizado = Cliente.normalizar_whatsapp(cliente.whatsapp)
    if whatsapp_normalizado:
        duplicado = candidatos.filter(whatsapp_normalizado=whatsapp_normalizado).first()
        if not duplicado:
            for candidato in candidatos.only("id", "nome", "whatsapp"):
                if Cliente.normalizar_whatsapp(candidato.whatsapp) == whatsapp_normalizado:
                    duplicado = candidato
                    break
        if duplicado:
            return duplicado, "whatsapp"

    if documento or whatsapp_normalizado:
        return None, ""

    nome = normalizar_texto_cliente(cliente.nome)
    apelido = normalizar_texto_cliente(cliente.apelido_nome_conhecido)
    localidade = normalizar_texto_cliente(cliente.bairro or cliente.cidade)
    if not nome:
        return None

    for candidato in candidatos.only("id", "nome", "apelido_nome_conhecido", "bairro", "cidade"):
        candidato_nome = normalizar_texto_cliente(candidato.nome)
        if candidato_nome == nome:
            return candidato, "nome"

        if not localidade:
            continue

        candidato_localidade = candidato.bairro or candidato.cidade
        if not textos_parecidos_cliente(localidade, candidato_localidade, minimo=0.84):
            continue

        nome_parecido = textos_parecidos_cliente(nome, candidato_nome, minimo=0.88)
        apelido_parecido = bool(apelido) and textos_parecidos_cliente(
            apelido,
            candidato.apelido_nome_conhecido,
            minimo=0.86,
        )
        if nome_parecido and (not apelido or apelido_parecido):
            return candidato, "nome"

    return None, ""


def encontrar_cliente_duplicado(cliente):
    duplicado, _campo = detectar_cliente_duplicado(cliente)
    return duplicado


def montar_checklist_url(request, rota_id):
    checklist_path = reverse("estoque:entrega_rota_checklist", kwargs={"pk": rota_id})
    checklist_base_url = getattr(settings, "CHECKLIST_BASE_URL", "").rstrip("/")
    if checklist_base_url:
        return f"{checklist_base_url}{checklist_path}"
    return f"{request.scheme}://{request.get_host()}{checklist_path}"


def montar_checklist_cliente_url(request, rota_id, venda_id, rota_item_id=None):
    if rota_item_id:
        checklist_path = reverse(
            "estoque:entrega_rota_checklist_item",
            kwargs={"rota_id": rota_id, "rota_item_id": rota_item_id},
        )
    else:
        checklist_path = reverse(
            "estoque:entrega_rota_checklist_cliente",
            kwargs={"rota_id": rota_id, "venda_id": venda_id},
        )
    checklist_base_url = getattr(settings, "CHECKLIST_BASE_URL", "").rstrip("/")
    if checklist_base_url:
        return f"{checklist_base_url}{checklist_path}"
    return f"{request.scheme}://{request.get_host()}{checklist_path}"


def montar_url_publica(request, path):
    base_url = getattr(settings, "CHECKLIST_BASE_URL", "").rstrip("/")
    if base_url:
        return f"{base_url}{path}"
    return request.build_absolute_uri(path)


CHECKLIST_FASE_MARKERS = {
    "carregamento": "[checklist_carregamento_salvo]",
    "entrega": "[checklist_entrega_salva]",
}


def checklist_fase_salva(item_rota, fase):
    marker = CHECKLIST_FASE_MARKERS.get(fase)
    return bool(marker and marker in (item_rota.observacao or ""))


def marcar_checklist_fase_salva(item_rota, fase):
    marker = CHECKLIST_FASE_MARKERS.get(fase)
    if not marker or checklist_fase_salva(item_rota, fase):
        return

    observacao = (item_rota.observacao or "").strip()
    item_rota.observacao = f"{observacao}\n{marker}".strip() if observacao else marker
    item_rota.save(update_fields=["observacao"])


def observacao_bloqueia_exclusao(item_rota):
    observacao = (item_rota.observacao or "").strip()
    if not observacao:
        return False
    if any(marker in observacao for marker in CHECKLIST_FASE_MARKERS.values()):
        return True
    if item_rota.is_pendencia and observacao.startswith("Pendencia incluida da rota #"):
        return False
    return True


def checklist_item_processado(checklist):
    return checklist.carregado or checklist.entregue


def rota_tem_evento_checklist_processado(rota):
    venda_ids = [item_rota.venda_id for item_rota in rota.itens.all()]
    if not venda_ids:
        return False
    return EventoVenda.objects.filter(
        venda_id__in=venda_ids,
        canal="whatsapp_checklist",
        descricao__icontains=f"rota/entrega #{rota.id}",
    ).exists()


def rota_pode_ser_excluida(rota):
    if rota.status != EntregaRota.STATUS_ABERTA:
        return False

    for item_rota in rota.itens.all():
        if (
            item_rota.status != EntregaRotaItem.STATUS_PENDENTE
            or item_rota.conferido_cliente
            or item_rota.entrega_concluida
            or observacao_bloqueia_exclusao(item_rota)
        ):
            return False

        for checklist in item_rota.checklist_itens.all():
            if checklist_item_processado(checklist):
                return False

    return not rota_tem_evento_checklist_processado(rota)


def formatar_nome_rota(valor):
    texto = " ".join(str(valor or "").strip().split())
    if not texto:
        return ""

    palavras_minusculas = {"da", "de", "do", "das", "dos", "e"}
    partes = []
    for indice, palavra in enumerate(texto.lower().split()):
        partes.append(palavra if indice > 0 and palavra in palavras_minusculas else palavra.capitalize())
    return " ".join(partes)


def localidade_principal_rota(vendas_ordenadas):
    contagem = {}
    nomes = {}

    for _, __, venda in vendas_ordenadas:
        cliente = venda.cliente
        localidade = ""
        if cliente:
            localidade = (cliente.bairro or "").strip() or (cliente.cidade or "").strip()
        localidade = formatar_nome_rota(localidade)
        if not localidade:
            continue

        chave = localidade.lower()
        contagem[chave] = contagem.get(chave, 0) + 1
        nomes.setdefault(chave, localidade)

    if not contagem:
        return ""

    maior_contagem = max(contagem.values())
    for _, __, venda in vendas_ordenadas:
        cliente = venda.cliente
        localidade = ""
        if cliente:
            localidade = (cliente.bairro or "").strip() or (cliente.cidade or "").strip()
        chave = formatar_nome_rota(localidade).lower()
        if contagem.get(chave) == maior_contagem:
            return nomes[chave]

    return ""


def listar_pendencias_entrega(limite=None):
    pendencias = []
    pendencias_reprogramadas = set(
        EntregaChecklistItem.objects.filter(
            rota_item__is_pendencia=True,
            rota_item__origem_pendencia_id__isnull=False,
        ).values_list("rota_item__origem_pendencia_id", "item_venda_id")
    )
    itens_rota = (
        EntregaRotaItem.objects.select_related("rota", "venda", "venda__cliente", "origem_pendencia")
        .prefetch_related("checklist_itens__item_venda__produto")
        .order_by("-rota__data", "-rota_id", "ordem_entrega", "id")
    )

    for item_rota in itens_rota:
        checklists = checklists_validos_rota_item(item_rota)
        itens_pendentes = [checklist for checklist in checklists if not checklist.entregue]
        entrega_salva = checklist_fase_salva(item_rota, "entrega")
        entrega_processada = (
            entrega_salva
            or item_rota.conferido_cliente
            or item_rota.entrega_concluida
            or item_rota.status in {
                EntregaRotaItem.STATUS_PARCIAL,
                EntregaRotaItem.STATUS_CANCELADA,
            }
        )
        if not entrega_processada and not (item_rota.is_pendencia and itens_pendentes):
            continue

        if not itens_pendentes and item_rota.status not in {
            EntregaRotaItem.STATUS_PARCIAL,
            EntregaRotaItem.STATUS_CANCELADA,
        }:
            continue

        if itens_pendentes:
            for checklist in itens_pendentes:
                item_venda = checklist.item_venda
                if (
                    not item_rota.is_pendencia
                    and (item_rota.id, item_venda.id if item_venda else None) in pendencias_reprogramadas
                ):
                    continue
                cliente = item_rota.venda.cliente
                produto = item_venda.produto.nome if item_venda and item_venda.produto else "Produto nao identificado"
                pendencias.append({
                    "id": checklist.id,
                    "item_rota_id": item_rota.id,
                    "item_venda_id": item_venda.id if item_venda else "",
                    "cliente": cliente.nome if cliente else "Consumidor",
                    "venda": item_rota.venda,
                    "rota": item_rota.rota,
                    "data": item_rota.rota.data,
                    "produto": produto,
                    "quantidade": item_venda.quantidade if item_venda else "",
                    "unidade": item_venda.unidade if item_venda else "",
                    "localidade": (
                        f"{cliente.bairro or ''} {cliente.cidade or ''}".strip()
                        if cliente
                        else ""
                    ),
                    "origem": f"Venda #{item_rota.venda_id} - Rota #{item_rota.rota_id}",
                    "status": "Item nao entregue",
                })
        else:
            cliente = item_rota.venda.cliente
            pendencias.append({
                "id": "",
                "item_rota_id": item_rota.id,
                "item_venda_id": "",
                "cliente": cliente.nome if cliente else "Consumidor",
                "venda": item_rota.venda,
                "rota": item_rota.rota,
                "data": item_rota.rota.data,
                "produto": "",
                "quantidade": "",
                "unidade": "",
                "localidade": (
                    f"{cliente.bairro or ''} {cliente.cidade or ''}".strip()
                    if cliente
                    else ""
                ),
                "origem": f"Venda #{item_rota.venda_id} - Rota #{item_rota.rota_id}",
                "status": item_rota.get_status_display(),
            })

        if limite and len(pendencias) >= limite:
            return pendencias[:limite]

    return pendencias


def pendencias_sugeriveis_entrega():
    return [pendencia for pendencia in listar_pendencias_entrega() if pendencia.get("id")]


def listar_pendencias_resolvidas_entrega(limite=None, filtros=None):
    filtros = filtros or {}
    eventos = (
        EventoVenda.objects.filter(tipo_evento="pendencia_removida_da_nota")
        .select_related("venda", "venda__cliente")
        .order_by("-criado_em", "-id")
    )
    cliente_filtro = (filtros.get("cliente") or "").strip()
    venda_filtro = (filtros.get("venda") or "").strip()
    produto_filtro = (filtros.get("produto") or "").strip()
    data_inicial = parse_date((filtros.get("data_inicial") or "").strip())
    data_final = parse_date((filtros.get("data_final") or "").strip())

    if cliente_filtro:
        eventos = eventos.filter(venda__cliente__nome__icontains=cliente_filtro)
    if venda_filtro.isdigit():
        eventos = eventos.filter(venda_id=int(venda_filtro))
    if produto_filtro:
        eventos = eventos.filter(descricao__icontains=produto_filtro)
    if data_inicial:
        eventos = eventos.filter(criado_em__date__gte=data_inicial)
    if data_final:
        eventos = eventos.filter(criado_em__date__lte=data_final)

    if limite:
        eventos = eventos[:limite]

    rota_ids = []
    dados_eventos = []
    for evento in eventos:
        rota_match = re.search(r"rota #(\d+)", evento.descricao or "", flags=re.IGNORECASE)
        rota_id = int(rota_match.group(1)) if rota_match else None
        if rota_id:
            rota_ids.append(rota_id)
        dados_eventos.append((evento, rota_id))

    rotas = EntregaRota.objects.in_bulk(rota_ids)
    pendencias = []
    for evento, rota_id in dados_eventos:
        descricao = evento.descricao or ""
        item_match = re.search(
            r"Item removido:\s*(?P<produto>.*?)\s*-\s*(?P<quantidade>[\d.,]+)\s*(?P<unidade>[^\(]*?)\s*\(",
            descricao,
            flags=re.IGNORECASE,
        )
        venda = evento.venda
        cliente = venda.cliente if venda else None
        resolucao = (
            "Resolvida removendo item da nota pela edicao da venda"
            if "edicao da nota" in descricao.lower() or "edicao da venda" in descricao.lower()
            else "Resolvida removendo item da nota"
        )
        resumo_resolucao = (
            "Item removido da nota - venda anulada porque ficou sem itens"
            if venda and venda.cancelada
            else "Item removido da nota - venda continuou ativa"
        )
        pendencias.append({
            "id": evento.id,
            "cliente": cliente.nome if cliente else "Consumidor",
            "venda": venda,
            "rota": rotas.get(rota_id),
            "rota_id": rota_id,
            "data": evento.criado_em,
            "produto": item_match.group("produto").strip() if item_match else "",
            "quantidade": item_match.group("quantidade").strip() if item_match else "",
            "unidade": item_match.group("unidade").strip() if item_match else "",
            "status": "Resolvida",
            "resolucao": resolucao,
            "resumo_resolucao": resumo_resolucao,
        })

    return pendencias


def contexto_pendencia_resolvida_nota(request, venda):
    if request.GET.get("origem") != "pendencias_resolvidas":
        return None

    evento_id = request.GET.get("evento")
    evento = (
        EventoVenda.objects.filter(
            pk=evento_id,
            venda=venda,
            tipo_evento="pendencia_removida_da_nota",
        )
        .first()
        if str(evento_id or "").isdigit()
        else None
    )
    if not evento:
        evento = (
            EventoVenda.objects.filter(venda=venda, tipo_evento="pendencia_removida_da_nota")
            .order_by("-criado_em", "-id")
            .first()
        )

    descricao = evento.descricao if evento else ""
    item_match = re.search(
        r"Item removido:\s*(?P<produto>.*?)\s*-\s*(?P<quantidade>[\d.,]+)\s*(?P<unidade>[^\(]*?)\s*\(",
        descricao,
        flags=re.IGNORECASE,
    )
    totais_match = re.search(
        r"Total alterado de\s*(?P<total_anterior>R\$\s*[\d.,]+)\s*para\s*(?P<total_novo>R\$\s*[\d.,]+)",
        descricao,
        flags=re.IGNORECASE,
    )
    rota_match = re.search(r"rota #(\d+)", descricao, flags=re.IGNORECASE)

    return {
        "evento": evento,
        "produto": item_match.group("produto").strip() if item_match else "",
        "quantidade": item_match.group("quantidade").strip() if item_match else "",
        "unidade": item_match.group("unidade").strip() if item_match else "",
        "rota_id": rota_match.group(1) if rota_match else "",
        "total_anterior": totais_match.group("total_anterior") if totais_match else "",
        "total_novo": totais_match.group("total_novo") if totais_match else "",
        "venda_cancelada": venda.cancelada,
        "descricao": descricao,
    }


def pendencias_checklist_validas(checklist_ids):
    ids = [int(checklist_id) for checklist_id in checklist_ids if str(checklist_id).isdigit()]
    if not ids:
        return []
    ids_selecionados = set(ids)

    checklists = (
        EntregaChecklistItem.objects.filter(pk__in=ids, entregue=False)
        .select_related(
            "item_venda",
            "item_venda__produto",
            "rota_item",
            "rota_item__rota",
            "rota_item__venda",
            "rota_item__venda__cliente",
        )
        .order_by("rota_item__rota__data", "rota_item_id", "item_venda_id")
    )
    pendencias_disponiveis = {
        pendencia["id"]
        for pendencia in pendencias_sugeriveis_entrega()
    }
    return [
        checklist
        for checklist in checklists
        if checklist.id in ids_selecionados and checklist.id in pendencias_disponiveis
    ]


def resumo_pendencia_rota_item(item_rota):
    if not item_rota.is_pendencia:
        return ""

    partes = []
    checklists = getattr(item_rota, "checklists_ordenados", None)
    if checklists is None:
        checklists = checklists_validos_rota_item(item_rota)

    for checklist in checklists:
        item_venda = checklist.item_venda
        produto = item_venda.produto.nome if item_venda and item_venda.produto else "Produto nao identificado"
        quantidade = item_venda.quantidade if item_venda else ""
        unidade = item_venda.unidade if item_venda else ""
        partes.append(f"{produto} / {quantidade} {unidade}".strip())

    return "; ".join(partes)


def item_venda_ids_pendencia_origem(item_rota):
    if not item_rota.is_pendencia or not item_rota.origem_pendencia_id:
        return None

    origem = item_rota.origem_pendencia
    if not origem.is_pendencia:
        return None

    ids_origem = item_venda_ids_pendencia_origem(origem)
    if ids_origem is not None:
        return ids_origem

    return set(origem.checklist_itens.values_list("item_venda_id", flat=True))


def checklists_validos_rota_item(item_rota):
    checklists = list(item_rota.checklist_itens.all())
    item_venda_ids_validos = item_venda_ids_pendencia_origem(item_rota)
    if item_venda_ids_validos is None:
        return checklists

    return [
        checklist
        for checklist in checklists
        if checklist.item_venda_id in item_venda_ids_validos
    ]


def calcular_total_itens_venda(venda, excluir_item_id=None):
    total = sum(
        (item.valor_total or Decimal("0.00"))
        for item in venda.itens.all()
        if excluir_item_id is None or item.id != excluir_item_id
    )
    return Decimal(total).quantize(Decimal("0.01"))


def recalcular_total_venda(venda):
    venda.total = calcular_total_itens_venda(venda)
    venda.save(update_fields=["total", "atualizado_em"])
    return venda.total


def descricao_pendencia_removida_da_nota(
    rota_id,
    produto_nome,
    quantidade,
    unidade,
    valor_removido,
    total_anterior,
    total_novo,
    origem="resolucao de pendencia de entrega",
):
    quantidade_texto = _formatar_quantidade(quantidade)
    unidade_texto = (unidade or "").strip()
    quantidade_unidade = f"{quantidade_texto} {unidade_texto}".strip()
    return (
        f"Pendencia resolvida por {origem}: removido {quantidade_unidade} de {produto_nome} da nota "
        f"(motivo: item nao entregue, rota #{rota_id}). "
        f"Item removido: {produto_nome} - {quantidade_unidade} ({_formatar_moeda(valor_removido)}). "
        f"Total alterado de {_formatar_moeda(total_anterior)} para {_formatar_moeda(total_novo)}."
    )


def _anular_venda_sem_itens_por_remocao_pendencia(venda):
    if venda.cancelada or ItemVenda.objects.filter(venda=venda).exists():
        return False

    motivo = "Remocao de pendencia deixou a nota sem itens."
    venda.cancelada = True
    venda.cancelada_em = timezone.now()
    venda.motivo_cancelamento = motivo
    venda.save(update_fields=["cancelada", "cancelada_em", "motivo_cancelamento", "atualizado_em"])
    _registrar_evento_venda(
        venda,
        "venda_anulada_sem_itens_por_pendencia",
        "Venda anulada porque a remocao da pendencia deixou a nota sem itens.",
        canal="sistema",
        usuario=venda.operador,
    )
    return True


def chave_cliente_entrega(venda):
    if venda and venda.cliente_id:
        return f"cliente:{venda.cliente_id}"
    return f"venda:{venda.id if venda else ''}"


def checklists_pendencia_unicos(checklists):
    checklists_unicos = []
    item_venda_ids = set()
    for checklist in checklists:
        if checklist.item_venda_id in item_venda_ids:
            continue
        checklists_unicos.append(checklist)
        item_venda_ids.add(checklist.item_venda_id)
    return checklists_unicos


def checklists_pendencia_selecionados(checklists, ids_selecionados):
    return [
        checklist
        for checklist in checklists_pendencia_unicos(checklists)
        if checklist.id in ids_selecionados and not checklist.entregue
    ]


def resolver_entregas_sem_pendencias_ativas(rota_item_ids):
    ids = [rota_item_id for rota_item_id in rota_item_ids if rota_item_id]
    if not ids:
        return

    itens_rota = (
        EntregaRotaItem.objects.filter(pk__in=ids)
        .select_related("origem_pendencia")
        .prefetch_related("checklist_itens")
    )
    for item_rota in itens_rota:
        checklists = checklists_validos_rota_item(item_rota)
        if any(not checklist.entregue for checklist in checklists):
            continue
        if item_rota.status == EntregaRotaItem.STATUS_ENTREGUE and item_rota.entrega_concluida:
            continue
        item_rota.status = EntregaRotaItem.STATUS_ENTREGUE
        item_rota.entrega_concluida = True
        item_rota.save(update_fields=["status", "entrega_concluida"])


def ordem_postada(valor, padrao=9999):
    try:
        return int(valor or padrao)
    except (TypeError, ValueError):
        return padrao


def home(request):
    produto_edicao = None

    # POST: criar/editar/excluir
    if request.method == "POST":
        acao = request.POST.get("acao")

        # EXCLUIR
        if acao == "excluir":
            excluir_id = request.POST.get("excluir_id")
            if excluir_id:
                produto = Produto.objects.filter(id=excluir_id).first()
                if produto:
                    produto.excluido = True
                    produto.save()
            return redirect("estoque:home")
        # CRIAR / EDITAR
        produto_id = request.POST.get("produto_id")
        if produto_id:
            produto_edicao = get_object_or_404(Produto, id=produto_id)
            form = ProdutoForm(request.POST, instance=produto_edicao)
        else:
            form = ProdutoForm(request.POST)

        if form.is_valid():
            produto = form.save()
            return redirect(f"{reverse('estoque:home')}?produto_destacado={produto.id}")

    # GET: carregar formulário de edição
    editar_id = request.GET.get("edit")
    if editar_id:
        produto_edicao = get_object_or_404(Produto, id=editar_id)
        form = ProdutoForm(instance=produto_edicao)
    else:
        form = ProdutoForm()

        # Busca + Filtro
    q = request.GET.get("q", "").strip()
    filtro = request.GET.get("f", "todos").strip().lower()

    produtos_base = Produto.objects.filter(excluido=False).annotate(
        prioridade=Case(
            When(quantidade=0, then=Value(0)),
            When(quantidade__gt=0, quantidade__lte=F("estoque_minimo"), then=Value(1)),
            When(
                quantidade__gt=F("estoque_minimo"),
                quantidade__lte=F("estoque_minimo") + Value(5),
                then=Value(2),
            ),
            default=Value(3),
            output_field=IntegerField(),
        )
    ).order_by("nome")

    produtos = produtos_base

    if q:
        produtos = produtos.filter(
            Q(nome__icontains=q) |
            Q(codigo__icontains=q) |
            Q(categoria__icontains=q)
        )

    if filtro == "zerado":
        produtos = produtos.filter(quantidade=0)
    elif filtro == "critico":
        produtos = produtos.filter(
            quantidade__gt=0,
            quantidade__lte=F("estoque_minimo"),
        )
    elif filtro == "limite":
        produtos = produtos.filter(
            quantidade__gt=F("estoque_minimo"),
            quantidade__lte=F("estoque_minimo") + Value(5),
        )
    elif filtro == "normal":
        produtos = produtos.filter(
            quantidade__gt=F("estoque_minimo") + Value(5),
        )

    # Contadores
    total_produtos = produtos_base.count()
    zerado_count = produtos_base.filter(quantidade=0).count()
    criticos_count = produtos_base.filter(
        quantidade__gt=0,
        quantidade__lte=F("estoque_minimo"),
    ).count()
    limite_count = produtos_base.filter(
        quantidade__gt=F("estoque_minimo"),
        quantidade__lte=F("estoque_minimo") + Value(5),
    ).count()
    normal_count = produtos_base.filter(
        quantidade__gt=F("estoque_minimo") + Value(5),
    ).count()

    # Financeiro
    total_investido = sum((p.preco_compra or 0) * (p.quantidade or 0) for p in produtos)
    total_faturamento = sum((p.preco_venda or 0) * (p.quantidade or 0) for p in produtos)
    lucro_bruto = total_faturamento - total_investido
    margem_percent = (lucro_bruto / total_faturamento * 100) if total_faturamento else 0

    categorias_ativas = Categoria.objects.filter(ativa=True).order_by("nome")

    return render(
        request,
        "estoque/home.html",
        {
            "produtos": produtos,
            "produto_edicao": produto_edicao,
            "form": form,
            "q": q,
            "filtro": filtro,
            "categorias_ativas": categorias_ativas,
            "total_produtos": total_produtos,
            "zerado_count": zerado_count,
            "criticos_count": criticos_count,
            "limite_count": limite_count,
            "normal_count": normal_count,
            "total_investido": total_investido,
            "total_faturamento": total_faturamento,
            "lucro_bruto": lucro_bruto,
            "margem_percent": margem_percent,
        }
    )


def _financeiro_dinheiro(valor):
    return valor or Decimal("0.00")


def _financeiro_moeda_br(valor):
    return f"R$ {_financeiro_dinheiro(valor):.2f}".replace(".", ",")


def _parse_decimal_financeiro(valor):
    texto = str(valor or "").strip()
    if not texto:
        return None
    texto = texto.replace("R$", "").replace(" ", "")
    if "," in texto:
        texto = texto.replace(".", "").replace(",", ".")
    try:
        return Decimal(texto).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def _garantir_contas_financeiras_padrao():
    contas_padrao = [
        ("Caixa em espécie", ContaFinanceira.TIPO_CAIXA, ["Caixa em espécie", "Caixa em especie"]),
        ("Sangria / Reserva em mãos", ContaFinanceira.TIPO_CAIXA, ["Sangria / Reserva em mãos", "Sangria / Reserva em maos", "Reserva em mãos", "Reserva em maos"]),
        ("Banco/Pix", ContaFinanceira.TIPO_BANCO, ["Banco/Pix"]),
    ]
    for nome, tipo, aliases in contas_padrao:
        conta = ContaFinanceira.objects.filter(nome__in=aliases, tipo=tipo).order_by("id").first()
        if conta:
            campos_atualizados = []
            if conta.nome != nome:
                conta.nome = nome
                campos_atualizados.append("nome")
            if not conta.ativo:
                conta.ativo = True
                campos_atualizados.append("ativo")
            if campos_atualizados:
                conta.save(update_fields=[*campos_atualizados, "atualizado_em"])
            continue
        ContaFinanceira.objects.create(nome=nome, tipo=tipo, saldo_inicial=Decimal("0.00"), ativo=True)


def _conta_financeira_padrao(nome):
    _garantir_contas_financeiras_padrao()
    aliases = {
        "caixa": ["Caixa em espécie", "Caixa em especie"],
        "reserva": ["Sangria / Reserva em mãos", "Sangria / Reserva em maos", "Reserva em mãos", "Reserva em maos"],
        "banco": ["Banco/Pix"],
    }.get(nome, [nome])
    return ContaFinanceira.objects.filter(ativo=True, nome__in=aliases).order_by("id").first()


def _saldo_conta_financeira(conta):
    saldo_inicial = _financeiro_dinheiro(conta.saldo_inicial)
    entradas = _financeiro_dinheiro(
        MovimentoFinanceiro.objects
        .filter(conta=conta, tipo=MovimentoFinanceiro.TIPO_ENTRADA)
        .aggregate(total=Sum("valor"))["total"]
    )
    ajustes = _financeiro_dinheiro(
        MovimentoFinanceiro.objects
        .filter(conta=conta, tipo=MovimentoFinanceiro.TIPO_AJUSTE)
        .aggregate(total=Sum("valor"))["total"]
    )
    saidas = _financeiro_dinheiro(
        MovimentoFinanceiro.objects
        .filter(conta=conta, tipo=MovimentoFinanceiro.TIPO_SAIDA)
        .aggregate(total=Sum("valor"))["total"]
    )
    transferencias_enviadas = _financeiro_dinheiro(
        MovimentoFinanceiro.objects
        .filter(conta=conta, tipo=MovimentoFinanceiro.TIPO_TRANSFERENCIA)
        .aggregate(total=Sum("valor"))["total"]
    )
    transferencias_recebidas = _financeiro_dinheiro(
        MovimentoFinanceiro.objects
        .filter(conta_destino=conta, tipo=MovimentoFinanceiro.TIPO_TRANSFERENCIA)
        .aggregate(total=Sum("valor"))["total"]
    )
    return saldo_inicial + entradas + ajustes - saidas - transferencias_enviadas + transferencias_recebidas


def _saldo_contas_financeiras(tipo_conta):
    total = Decimal("0.00")
    for conta in ContaFinanceira.objects.filter(ativo=True, tipo=tipo_conta):
        total += _saldo_conta_financeira(conta)
    return total


def _contas_financeiras_com_saldo():
    contas = list(ContaFinanceira.objects.filter(ativo=True).order_by("tipo", "nome", "id"))
    for conta in contas:
        conta.saldo_atual = _saldo_conta_financeira(conta)
        conta.saldo_atual_texto = _financeiro_moeda_br(conta.saldo_atual)
    return contas


def _texto_sem_acentos(texto):
    return "".join(
        char for char in unicodedata.normalize("NFKD", str(texto or ""))
        if not unicodedata.combining(char)
    )


def _conta_financeira_por_forma_pagamento(forma_pagamento):
    _garantir_contas_financeiras_padrao()
    forma = _texto_sem_acentos(forma_pagamento).lower().strip()
    forma_compacta = re.sub(r"\s+", "", forma)
    if any(termo in forma_compacta for termo in {"dinheiro", "especie"}):
        return ContaFinanceira.objects.filter(
            ativo=True,
            tipo=ContaFinanceira.TIPO_CAIXA,
            nome__in=["Caixa em espécie", "Caixa em especie"],
        ).order_by("id").first()
    # Cartao pode ganhar controle proprio em etapa futura.
    if any(
        termo in forma_compacta
        for termo in {
            "pix",
            "banco",
            "transferencia",
            "boleto",
            "deposito",
            "cartao",
            "debito",
            "credito",
        }
    ):
        return ContaFinanceira.objects.filter(
            ativo=True,
            tipo=ContaFinanceira.TIPO_BANCO,
            nome="Banco/Pix",
        ).order_by("id").first()
    return ContaFinanceira.objects.filter(
        ativo=True,
        tipo=ContaFinanceira.TIPO_BANCO,
        nome="Banco/Pix",
    ).order_by("id").first()


def _registrar_movimento_recebimento_cliente(cliente, valor_recebido, data_recebimento, forma_pagamento):
    valor = _financeiro_dinheiro(valor_recebido).quantize(Decimal("0.01"))
    if valor <= Decimal("0.00"):
        return None
    conta_financeira = _conta_financeira_por_forma_pagamento(forma_pagamento)
    if not conta_financeira:
        return None
    return MovimentoFinanceiro.objects.create(
        conta=conta_financeira,
        tipo=MovimentoFinanceiro.TIPO_ENTRADA,
        valor=valor,
        data=data_recebimento or timezone.localdate(),
        descricao=f"Recebimento de cliente: {getattr(cliente, 'nome', '') or 'Cliente nao informado'}",
        origem="recebimento_cliente",
    )


def _descricao_movimento_despesa_diaria(despesa):
    descricao = f"Despesa diaria: {despesa.get_categoria_display()}"
    observacao = (despesa.observacao or "").strip()
    if observacao:
        descricao = f"{descricao} - {observacao[:80]}"
    return descricao


def _registrar_movimento_despesa_diaria(despesa, conta_financeira=None):
    valor = _financeiro_dinheiro(despesa.valor).quantize(Decimal("0.01"))
    if valor <= Decimal("0.00"):
        return None
    if not conta_financeira:
        conta_financeira = _conta_financeira_por_forma_pagamento(despesa.forma_pagamento)
    if not conta_financeira:
        return None
    data_despesa = timezone.localtime(despesa.data_hora).date() if despesa.data_hora else timezone.localdate()
    return MovimentoFinanceiro.objects.create(
        conta=conta_financeira,
        tipo=MovimentoFinanceiro.TIPO_SAIDA,
        valor=valor,
        data=data_despesa,
        descricao=_descricao_movimento_despesa_diaria(despesa),
        operador=(despesa.operador or ""),
        origem="despesa_diaria",
    )


def _movimento_despesa_diaria_correspondente(despesa):
    if not despesa:
        return None
    data_despesa = timezone.localtime(despesa.data_hora).date() if despesa.data_hora else None
    if not data_despesa:
        return None
    return (
        MovimentoFinanceiro.objects
        .filter(
            origem="despesa_diaria",
            tipo=MovimentoFinanceiro.TIPO_SAIDA,
            valor=despesa.valor,
            data=data_despesa,
            descricao=_descricao_movimento_despesa_diaria(despesa),
        )
        .select_related("conta")
        .order_by("-id")
        .first()
    )


def _compra_pagamento_a_prazo(tipo_pagamento):
    forma = normalizar_texto_cliente(tipo_pagamento)
    forma_compacta = re.sub(r"\s+", "", forma)
    return forma in {"a prazo", "prazo"} or forma_compacta in {"aprazo", "prazo"}


def _compra_pagamento_imediato(tipo_pagamento):
    tipo_pagamento = (tipo_pagamento or "").strip()
    return bool(tipo_pagamento) and not _compra_pagamento_a_prazo(tipo_pagamento)


def _descricao_compra_a_vista(compra, conta=None, complemento=""):
    fornecedor_nome = compra.fornecedor.nome if compra.fornecedor else "Fornecedor nao informado"
    descricao = f"Pagamento da compra #{compra.id} - Fornecedor: {fornecedor_nome}"
    if conta:
        descricao = f"{descricao} - {conta.nome}"
    if complemento:
        descricao = f"{descricao} - {complemento}"
    return descricao[:255]


def _movimentos_financeiros_compra(compra):
    movimentos_vinculados = list(
        MovimentoFinanceiro.objects
        .select_related("conta", "conta_destino")
        .filter(compra=compra)
        .order_by("id")
    )
    movimentos_por_id = {movimento.id: movimento for movimento in movimentos_vinculados}

    fornecedor_nome = compra.fornecedor.nome if compra.fornecedor else ""
    filtros_por_id = Q(origem__in=["compra_a_vista", "compra_correcao_item", "compra_correcao_origem"]) & (
        Q(descricao__icontains=f"Compra #{compra.id}") |
        Q(descricao__icontains=f"Compra {compra.id}")
    )
    movimentos_legados_por_id = list(
        MovimentoFinanceiro.objects
        .select_related("conta", "conta_destino")
        .filter(filtros_por_id)
        .order_by("id")
    )
    for movimento in movimentos_legados_por_id:
        movimentos_por_id.setdefault(movimento.id, movimento)
    if movimentos_por_id:
        return sorted(movimentos_por_id.values(), key=lambda movimento: movimento.id)

    # Compras criadas pelo fluxo atual sempre possuem token e movimentos ligados
    # pela FK. O fallback por fornecedor/data existe apenas para registros legados;
    # usá-lo aqui confundiria duas compras novas do mesmo fornecedor no mesmo dia.
    if compra.fechamento_token:
        return []

    if not fornecedor_nome:
        return []

    # Compatibilidade com compras antigas gravadas antes de a descricao conter o id.
    movimentos_legados_por_fornecedor = list(
        MovimentoFinanceiro.objects
        .select_related("conta", "conta_destino")
        .filter(
            origem="compra_a_vista",
            data=compra.data_compra,
            descricao__icontains=fornecedor_nome,
        )
        .order_by("id")
    )
    for movimento in movimentos_legados_por_fornecedor:
        movimentos_por_id.setdefault(movimento.id, movimento)
    return sorted(movimentos_por_id.values(), key=lambda movimento: movimento.id)


def _alocacao_financeira_compra(compra):
    contas = {
        "caixa": _conta_financeira_padrao("caixa"),
        "reserva": _conta_financeira_padrao("reserva"),
        "banco": _conta_financeira_padrao("banco"),
    }
    alocacao = {chave: Decimal("0.00") for chave in contas}
    conta_para_chave = {conta.pk: chave for chave, conta in contas.items() if conta}

    for movimento in _movimentos_financeiros_compra(compra):
        chave = conta_para_chave.get(movimento.conta_id)
        if not chave:
            continue
        valor = _financeiro_dinheiro(movimento.valor).quantize(Decimal("0.01"))
        if movimento.tipo == MovimentoFinanceiro.TIPO_SAIDA:
            alocacao[chave] += valor
        elif movimento.tipo in {MovimentoFinanceiro.TIPO_ENTRADA, MovimentoFinanceiro.TIPO_AJUSTE}:
            alocacao[chave] -= valor

    return alocacao


def _valores_origem_compra_post(request):
    valores = {}
    for chave, campo in {
        "caixa": "origem_caixa",
        "reserva": "origem_reserva",
        "banco": "origem_banco",
    }.items():
        texto = str(request.POST.get(campo) or "").strip()
        valor = _parse_decimal_financeiro(texto) if texto else Decimal("0.00")
        if valor is None:
            raise ValueError("Informe valores monetarios validos nas origens do pagamento.")
        valores[chave] = valor.quantize(Decimal("0.01"))
    if any(valor < Decimal("0.00") for valor in valores.values()):
        raise ValueError("Os valores de origem do dinheiro nao podem ser negativos.")
    return valores


def _validar_origem_compra_a_vista(valores, total):
    total = _financeiro_dinheiro(total).quantize(Decimal("0.01"))
    soma = sum(valores.values(), Decimal("0.00")).quantize(Decimal("0.01"))
    if total > Decimal("0.00") and not any(valor > Decimal("0.00") for valor in valores.values()):
        raise ValueError("Informe pelo menos uma origem do dinheiro com valor maior que zero.")
    if soma != total:
        raise ValueError(
            "Distribua o total da compra entre Caixa, Sangria e Banco/Pix. "
            f"Já distribuído: {_financeiro_moeda_br(soma)}. Total: {_financeiro_moeda_br(total)}."
        )


def _registrar_movimentos_compra_a_vista(compra, valores_origem=None):
    total = _financeiro_dinheiro(compra.total).quantize(Decimal("0.01"))
    if total <= Decimal("0.00"):
        return []

    movimentos_existentes = list(_movimentos_financeiros_compra(compra))
    if movimentos_existentes:
        return movimentos_existentes

    contas = {
        "caixa": _conta_financeira_padrao("caixa"),
        "reserva": _conta_financeira_padrao("reserva"),
        "banco": _conta_financeira_padrao("banco"),
    }
    contas_ausentes = (
        [
            chave
            for chave, valor in valores_origem.items()
            if valor > Decimal("0.00") and not contas.get(chave)
        ]
        if valores_origem
        else []
    )
    if contas_ausentes:
        raise ValueError("Nao foi possivel localizar todas as contas financeiras do pagamento.")
    if valores_origem is None:
        conta = _conta_financeira_por_forma_pagamento(compra.tipo_pagamento)
        chave = "banco"
        if conta and contas["caixa"] and conta.pk == contas["caixa"].pk:
            chave = "caixa"
        elif conta and contas["reserva"] and conta.pk == contas["reserva"].pk:
            chave = "reserva"
        valores_origem = {"caixa": Decimal("0.00"), "reserva": Decimal("0.00"), "banco": Decimal("0.00")}
        valores_origem[chave] = total

    movimentos = []
    for chave, valor in valores_origem.items():
        valor = _financeiro_dinheiro(valor).quantize(Decimal("0.01"))
        if valor <= Decimal("0.00"):
            continue
        conta_financeira = contas.get(chave)
        if not conta_financeira:
            continue
        movimentos.append(MovimentoFinanceiro.objects.create(
            conta=conta_financeira,
            tipo=MovimentoFinanceiro.TIPO_SAIDA,
            valor=valor,
            data=compra.data_compra or timezone.localdate(),
            descricao=_descricao_compra_a_vista(compra, conta_financeira),
            operador=compra.operador or "",
            origem="compra_a_vista",
            compra=compra,
        ))
    return movimentos


def _registrar_movimento_compra_a_vista(compra):
    movimentos = _registrar_movimentos_compra_a_vista(compra)
    return movimentos[0] if movimentos else None


def _venda_pagamento_imediato(tipo_pagamento):
    forma = normalizar_texto_cliente(tipo_pagamento)
    forma_compacta = re.sub(r"\s+", "", forma)
    if forma in {"a prazo", "carteira", "fiado"} or forma_compacta in {"aprazo"}:
        return False
    return forma in {
        "a vista",
        "dinheiro",
        "especie",
        "pix",
        "banco",
        "transferencia",
        "cartao",
        "debito",
        "credito",
        "boleto",
        "deposito",
    } or forma_compacta in {"avista"}


def _conta_financeira_venda_a_vista(tipo_pagamento):
    forma = normalizar_texto_cliente(tipo_pagamento)
    forma_compacta = re.sub(r"\s+", "", forma)
    if forma in {"a vista", "dinheiro", "especie"} or forma_compacta in {"avista"}:
        return _conta_financeira_padrao("caixa")
    return _conta_financeira_por_forma_pagamento(tipo_pagamento)


def _descricao_venda_a_vista(venda, conta=None):
    cliente_nome = venda.cliente.nome if venda.cliente else "Cliente nao informado"
    descricao = f"Venda a vista #{venda.id} - {cliente_nome}"
    if conta:
        if conta.nome == "Banco/Pix":
            descricao = f"{descricao} - Banco/Pix"
        else:
            descricao = f"{descricao} - Dinheiro/Caixa"
    return descricao[:255]


def _movimentos_financeiros_venda(venda):
    return (
        MovimentoFinanceiro.objects
        .select_related("conta", "conta_destino")
        .filter(
            tipo=MovimentoFinanceiro.TIPO_ENTRADA,
            origem="venda",
            descricao__startswith=f"Venda a vista #{venda.id}",
        )
        .order_by("id")
    )


def _alocacao_financeira_venda(venda):
    contas = {
        "caixa": _conta_financeira_padrao("caixa"),
        "banco": _conta_financeira_padrao("banco"),
    }
    alocacao = {chave: Decimal("0.00") for chave in contas}
    conta_para_chave = {conta.pk: chave for chave, conta in contas.items() if conta}

    for movimento in _movimentos_financeiros_venda(venda):
        chave = conta_para_chave.get(movimento.conta_id)
        if not chave:
            continue
        alocacao[chave] += _financeiro_dinheiro(movimento.valor).quantize(Decimal("0.01"))

    return alocacao


def _valores_origem_venda_post(dados):
    origem = dados.get("origem_recebimento") or {}
    valores = {
        "caixa": (_parse_decimal_financeiro(origem.get("caixa")) or Decimal("0.00")).quantize(Decimal("0.01")),
        "banco": (_parse_decimal_financeiro(origem.get("banco")) or Decimal("0.00")).quantize(Decimal("0.01")),
    }
    if any(valor < Decimal("0.00") for valor in valores.values()):
        raise ValueError("Os valores de origem do recebimento nao podem ser negativos.")
    if not any(valor > Decimal("0.00") for valor in valores.values()):
        raise ValueError("Informe pelo menos uma origem do recebimento com valor maior que zero.")
    return valores


def _validar_origem_venda_a_vista(valores, total):
    total = _financeiro_dinheiro(total).quantize(Decimal("0.01"))
    soma = sum(valores.values(), Decimal("0.00")).quantize(Decimal("0.01"))
    if soma != total:
        raise ValueError(
            f"A soma das origens precisa bater com o total da venda. Soma: {_financeiro_moeda_br(soma)}. Total: {_financeiro_moeda_br(total)}."
        )


def _registrar_movimentos_venda_a_vista(venda, valores_origem=None):
    if not _venda_pagamento_imediato(venda.tipo_pagamento):
        return []

    valor = _financeiro_dinheiro(venda.total).quantize(Decimal("0.01"))
    if valor <= Decimal("0.00"):
        return []

    movimentos_existentes = list(_movimentos_financeiros_venda(venda))
    if movimentos_existentes:
        return movimentos_existentes

    contas = {
        "caixa": _conta_financeira_padrao("caixa"),
        "banco": _conta_financeira_padrao("banco"),
    }
    if valores_origem is None:
        conta = _conta_financeira_venda_a_vista(venda.tipo_pagamento)
        chave = "banco"
        if conta and contas["caixa"] and conta.pk == contas["caixa"].pk:
            chave = "caixa"
        valores_origem = {"caixa": Decimal("0.00"), "banco": Decimal("0.00")}
        valores_origem[chave] = valor

    movimentos = []
    for chave, valor_origem in valores_origem.items():
        valor_origem = _financeiro_dinheiro(valor_origem).quantize(Decimal("0.01"))
        if valor_origem <= Decimal("0.00"):
            continue
        conta_financeira = contas.get(chave)
        if not conta_financeira:
            continue
        movimentos.append(MovimentoFinanceiro.objects.create(
            conta=conta_financeira,
            tipo=MovimentoFinanceiro.TIPO_ENTRADA,
            valor=valor_origem,
            data=venda.data_venda or timezone.localdate(),
            descricao=_descricao_venda_a_vista(venda, conta_financeira),
            operador=venda.operador or "",
            origem="venda",
        ))
    return movimentos


def _registrar_movimento_venda_a_vista(venda):
    movimentos = _registrar_movimentos_venda_a_vista(venda)
    return movimentos[0] if movimentos else None


def _registrar_movimento_conta_pagar_fornecedor(conta, valor_pago, data_pagamento, forma_pagamento):
    valor = _financeiro_dinheiro(valor_pago).quantize(Decimal("0.01"))
    if valor <= Decimal("0.00"):
        return None
    conta_financeira = _conta_financeira_por_forma_pagamento(forma_pagamento)
    if not conta_financeira:
        return None
    fornecedor_nome = conta.fornecedor.nome if conta.fornecedor else ""
    descricao = (
        f"Pagamento de fornecedor: {fornecedor_nome}"
        if fornecedor_nome
        else "Pagamento de conta a pagar"
    )
    return MovimentoFinanceiro.objects.create(
        conta=conta_financeira,
        tipo=MovimentoFinanceiro.TIPO_SAIDA,
        valor=valor,
        data=data_pagamento or timezone.localdate(),
        descricao=descricao,
        origem="conta_pagar_fornecedor",
    )


def painel_financeiro(request):
    _garantir_contas_financeiras_padrao()
    hoje = timezone.localdate()
    fim_7 = hoje + timedelta(days=7)
    fim_30 = hoje + timedelta(days=30)
    inicio_mes = hoje.replace(day=1)

    def dinheiro(valor):
        return valor or Decimal("0.00")

    def moeda_br(valor):
        return f"R$ {dinheiro(valor):.2f}".replace(".", ",")

    contas_receber_abertas = ContaReceber.objects.filter(
        status__in=[ContaReceber.STATUS_ABERTA, ContaReceber.STATUS_PARCIAL],
        valor_em_aberto__gt=0,
    )
    contas_pagar_abertas = ContaPagar.objects.filter(
        status__in=[ContaPagar.STATUS_ABERTA, ContaPagar.STATUS_PARCIAL],
        valor_em_aberto__gt=0,
    )

    receber_hoje = dinheiro(contas_receber_abertas.filter(data_vencimento=hoje).aggregate(total=Sum("valor_em_aberto"))["total"])
    pagar_hoje = dinheiro(contas_pagar_abertas.filter(data_vencimento=hoje).aggregate(total=Sum("valor_em_aberto"))["total"])

    def saldo_contas_financeiras(tipo_conta):
        contas = ContaFinanceira.objects.filter(ativo=True, tipo=tipo_conta)
        contas_ids = list(contas.values_list("id", flat=True))
        saldo_inicial = dinheiro(contas.aggregate(total=Sum("saldo_inicial"))["total"])
        if not contas_ids:
            return saldo_inicial

        entradas = dinheiro(
            MovimentoFinanceiro.objects
            .filter(conta_id__in=contas_ids, tipo=MovimentoFinanceiro.TIPO_ENTRADA)
            .aggregate(total=Sum("valor"))["total"]
        )
        ajustes = dinheiro(
            MovimentoFinanceiro.objects
            .filter(conta_id__in=contas_ids, tipo=MovimentoFinanceiro.TIPO_AJUSTE)
            .aggregate(total=Sum("valor"))["total"]
        )
        saidas = dinheiro(
            MovimentoFinanceiro.objects
            .filter(conta_id__in=contas_ids, tipo=MovimentoFinanceiro.TIPO_SAIDA)
            .aggregate(total=Sum("valor"))["total"]
        )
        transferencias_enviadas = dinheiro(
            MovimentoFinanceiro.objects
            .filter(conta_id__in=contas_ids, tipo=MovimentoFinanceiro.TIPO_TRANSFERENCIA)
            .aggregate(total=Sum("valor"))["total"]
        )
        transferencias_recebidas = dinheiro(
            MovimentoFinanceiro.objects
            .filter(conta_destino_id__in=contas_ids, tipo=MovimentoFinanceiro.TIPO_TRANSFERENCIA)
            .aggregate(total=Sum("valor"))["total"]
        )
        return saldo_inicial + entradas + ajustes - saidas - transferencias_enviadas + transferencias_recebidas

    conta_caixa = _conta_financeira_padrao("caixa")
    conta_reserva = _conta_financeira_padrao("reserva")
    conta_banco = _conta_financeira_padrao("banco")
    saldo_caixa_especie = _saldo_conta_financeira(conta_caixa) if conta_caixa else Decimal("0.00")
    saldo_reserva = _saldo_conta_financeira(conta_reserva) if conta_reserva else Decimal("0.00")
    saldo_banco = _saldo_conta_financeira(conta_banco) if conta_banco else Decimal("0.00")
    saldo_caixa_total = saldo_caixa_especie + saldo_reserva
    saldo_caixa = saldo_caixa_especie
    total_disponivel = saldo_caixa_total + saldo_banco
    total_a_pagar_hoje_banco = pagar_hoje
    banco_suficiente_hoje = saldo_banco >= total_a_pagar_hoje_banco
    falta_banco_hoje = max(total_a_pagar_hoje_banco - saldo_banco, Decimal("0.00"))
    if banco_suficiente_hoje:
        situacao_banco_hoje = "Banco suficiente para os compromissos de hoje."
    else:
        valor_faltante = f"{falta_banco_hoje:.2f}".replace(".", ",")
        situacao_banco_hoje = f"Banco insuficiente. Falta R$ {valor_faltante} para os compromissos de hoje."

    receber_7 = dinheiro(contas_receber_abertas.filter(data_vencimento__range=(hoje, fim_7)).aggregate(total=Sum("valor_em_aberto"))["total"])
    pagar_7 = dinheiro(contas_pagar_abertas.filter(data_vencimento__range=(hoje, fim_7)).aggregate(total=Sum("valor_em_aberto"))["total"])

    receber_30 = dinheiro(contas_receber_abertas.filter(data_vencimento__range=(hoje, fim_30)).aggregate(total=Sum("valor_em_aberto"))["total"])
    pagar_30 = dinheiro(contas_pagar_abertas.filter(data_vencimento__range=(hoje, fim_30)).aggregate(total=Sum("valor_em_aberto"))["total"])

    clientes_vencidos = contas_receber_abertas.filter(data_vencimento__lt=hoje).aggregate(
        total=Sum("valor_em_aberto"),
        quantidade=Count("id"),
    )
    fornecedores_vencidos = contas_pagar_abertas.filter(data_vencimento__lt=hoje).aggregate(
        total=Sum("valor_em_aberto"),
        quantidade=Count("id"),
    )

    vendas_mes = dinheiro(
        Venda.objects
        .filter(data_venda__gte=inicio_mes, data_venda__lte=hoje, cancelada=False)
        .aggregate(total=Sum("total"))["total"]
    )
    compras_mes = dinheiro(
        Compra.objects
        .filter(data_compra__gte=inicio_mes, data_compra__lte=hoje, cancelada=False)
        .exclude(status=Compra.STATUS_CANCELADA)
        .aggregate(total=Sum("total"))["total"]
    )
    despesas_hoje = dinheiro(
        DespesaDiaria.objects
        .filter(data_hora__date=hoje)
        .aggregate(total=Sum("valor"))["total"]
    )
    despesas_7 = dinheiro(
        DespesaDiaria.objects
        .filter(data_hora__date__range=(hoje - timedelta(days=6), hoje))
        .aggregate(total=Sum("valor"))["total"]
    )
    despesas_mes = dinheiro(
        DespesaDiaria.objects
        .filter(data_hora__date__gte=inicio_mes, data_hora__date__lte=hoje)
        .aggregate(total=Sum("valor"))["total"]
    )

    valor_estoque_expr = ExpressionWrapper(
        Coalesce(F("quantidade"), 0) * Coalesce(F("preco_compra"), Decimal("0.00")),
        output_field=DecimalField(max_digits=14, decimal_places=2),
    )
    valor_estoque = dinheiro(
        Produto.objects
        .filter(excluido=False)
        .aggregate(total=Sum(valor_estoque_expr))["total"]
    )
    estoque_baixo_qtd = Produto.objects.filter(
        excluido=False,
        estoque_minimo__isnull=False,
        quantidade__isnull=False,
        quantidade__lte=F("estoque_minimo"),
    ).count()

    return render(
        request,
        "estoque/painel_financeiro.html",
        {
            "hoje": hoje,
            "fim_7": fim_7,
            "fim_30": fim_30,
            "caixa_banco_cards": [
                {"titulo": "Caixa em espécie", "valor": saldo_caixa, "valor_texto": moeda_br(saldo_caixa), "tipo": "neutro"},
                {"titulo": "Sangria / Reserva em mãos", "valor": saldo_reserva, "valor_texto": moeda_br(saldo_reserva), "tipo": "neutro"},
                {"titulo": "Banco/Pix", "valor": saldo_banco, "valor_texto": moeda_br(saldo_banco), "tipo": "neutro"},
                {"titulo": "Total disponivel", "valor": total_disponivel, "valor_texto": moeda_br(total_disponivel), "tipo": "destaque"},
                {
                    "titulo": "Situacao do banco hoje",
                    "valor": situacao_banco_hoje,
                    "tipo": "suficiente" if banco_suficiente_hoje else "insuficiente",
                    "moeda": False,
                },
            ],
            "hoje_cards": [
                {"titulo": "Total a receber hoje", "valor": receber_hoje, "tipo": "positivo"},
                {"titulo": "Total a pagar hoje", "valor": pagar_hoje, "tipo": "negativo"},
                {"titulo": "Saldo previsto do dia", "valor": receber_hoje - pagar_hoje, "tipo": "saldo"},
                {"titulo": "Saldo do dia apos despesas", "valor": receber_hoje - pagar_hoje - despesas_hoje, "tipo": "saldo"},
            ],
            "sete_dias_cards": [
                {"titulo": "A receber nos proximos 7 dias", "valor": receber_7, "tipo": "positivo"},
                {"titulo": "A pagar nos proximos 7 dias", "valor": pagar_7, "tipo": "negativo"},
                {"titulo": "Saldo previsto em 7 dias", "valor": receber_7 - pagar_7, "tipo": "saldo"},
            ],
            "trinta_dias_cards": [
                {"titulo": "A receber nos proximos 30 dias", "valor": receber_30, "tipo": "positivo"},
                {"titulo": "A pagar nos proximos 30 dias", "valor": pagar_30, "tipo": "negativo"},
                {"titulo": "Saldo previsto em 30 dias", "valor": receber_30 - pagar_30, "tipo": "saldo"},
            ],
            "atrasados_cards": [
                {"titulo": "Clientes vencidos", "valor": dinheiro(clientes_vencidos["total"]), "tipo": "vencido"},
                {"titulo": "Contas de clientes vencidas", "valor": clientes_vencidos["quantidade"] or 0, "tipo": "quantidade", "moeda": False},
                {"titulo": "Fornecedores vencidos", "valor": dinheiro(fornecedores_vencidos["total"]), "tipo": "vencido"},
                {"titulo": "Contas de fornecedores vencidas", "valor": fornecedores_vencidos["quantidade"] or 0, "tipo": "quantidade", "moeda": False},
            ],
            "mes_cards": [
                {"titulo": "Total vendido no mes", "valor": vendas_mes, "tipo": "positivo"},
                {"titulo": "Total comprado no mes", "valor": compras_mes, "tipo": "negativo"},
                {"titulo": "Vendas menos compras", "valor": vendas_mes - compras_mes, "tipo": "saldo"},
                {"titulo": "Vendas menos compras e despesas", "valor": vendas_mes - compras_mes - despesas_mes, "tipo": "saldo"},
            ],
            "despesas_cards": [
                {"titulo": "Despesas de hoje", "valor": despesas_hoje, "tipo": "negativo"},
                {"titulo": "Despesas dos ultimos 7 dias", "valor": despesas_7, "tipo": "negativo"},
                {"titulo": "Despesas do mes atual", "valor": despesas_mes, "tipo": "negativo"},
            ],
            "estoque_cards": [
                {"titulo": "Valor estimado total em estoque", "valor": valor_estoque, "tipo": "neutro"},
                {"titulo": "Produtos com estoque baixo", "valor": estoque_baixo_qtd, "tipo": "quantidade", "moeda": False},
            ],
            "campo_estoque_usado": "quantidade atual x preco_compra",
        },
    )


def caixa_banco(request):
    _garantir_contas_financeiras_padrao()
    conta_caixa = _conta_financeira_padrao("caixa")
    conta_reserva = _conta_financeira_padrao("reserva")
    conta_banco = _conta_financeira_padrao("banco")
    operadores_caixa = list(Funcionario.operadores_do_caixa().only("id", "nome"))
    operadores_caixa_por_id = {str(funcionario.id): funcionario for funcionario in operadores_caixa}

    painel_por_acao_caixa = {
        "abrir_caixa": "abrir-caixa",
        "ajustar_saldo": "corrigir-saldo",
        "ajuste_saldo": "corrigir-saldo",
        "ajuste_banco_pix": "corrigir-saldo",
        "fazer_sangria": "sangria",
        "depositar_reserva_banco": "deposito",
        "reforcar_caixa_reserva": "reforco",
        "pagar_com_reserva": "pagar-reserva",
        "entrada": "entrada",
        "saida": "saida",
        "entrada_avulsa": "entrada",
        "saida_avulsa": "saida",
        "transferencia": "transferencia",
    }

    erro_local_caixa = request.session.pop("caixa_banco_erro_operacao", {}) if request.method == "GET" else {}
    erro_operacao = erro_local_caixa.get("painel", "")
    erro_operacao_texto = erro_local_caixa.get("texto", "")

    def registrar_erro_caixa_local(acao_ou_painel, texto):
        painel = painel_por_acao_caixa.get(acao_ou_painel, acao_ou_painel)
        messages.error(request, texto)
        if painel:
            request.session["caixa_banco_erro_operacao"] = {
                "painel": painel,
                "texto": texto,
            }

    def operador_post(painel_erro=""):
        funcionario = operadores_caixa_por_id.get(request.POST.get("operador", "").strip())
        if not funcionario:
            registrar_erro_caixa_local(
                painel_erro,
                "Selecione um operador autorizado para movimentar Caixa/Banco.",
            )
            return None
        return funcionario.nome

    def criar_ajuste_para_saldo(conta, novo_saldo, descricao, origem, operador=""):
        saldo_atual = _saldo_conta_financeira(conta)
        diferenca = (novo_saldo - saldo_atual).quantize(Decimal("0.01"))
        return MovimentoFinanceiro.objects.create(
            conta=conta,
            tipo=MovimentoFinanceiro.TIPO_AJUSTE,
            valor=diferenca,
            data=timezone.localdate(),
            descricao=descricao,
            operador=operador,
            origem=origem,
        )

    def criar_transferencia(conta_origem, conta_destino, valor, descricao, origem, operador="", painel_erro=""):
        if not conta_origem or not conta_destino:
            registrar_erro_caixa_local(painel_erro, "Conta financeira nao encontrada.")
            return False
        if valor is None or valor <= 0:
            registrar_erro_caixa_local(painel_erro, "Informe um valor maior que zero.")
            return False
        if conta_origem.pk == conta_destino.pk:
            registrar_erro_caixa_local(painel_erro, "Origem e destino nao podem ser iguais.")
            return False
        if _saldo_conta_financeira(conta_origem) < valor:
            saldo_disponivel = _saldo_conta_financeira(conta_origem)
            texto_erro = f"Saldo insuficiente na conta de origem. Disponivel em {conta_origem.nome}: {_financeiro_moeda_br(saldo_disponivel)}."
            registrar_erro_caixa_local(painel_erro, texto_erro)
            return False
        MovimentoFinanceiro.objects.create(
            conta=conta_origem,
            conta_destino=conta_destino,
            tipo=MovimentoFinanceiro.TIPO_TRANSFERENCIA,
            valor=valor,
            data=timezone.localdate(),
            descricao=descricao,
            operador=operador,
            origem=origem,
        )
        return True

    if request.method == "POST":
        acao = request.POST.get("acao", "").strip()

        if acao in {"abrir_caixa", "ajustar_saldo", "ajuste_saldo"}:
            conta = ContaFinanceira.objects.filter(pk=request.POST.get("conta"), ativo=True).first()
            operador = operador_post(painel_por_acao_caixa.get(acao, ""))
            if operador is None:
                return redirect("estoque:caixa_banco")
            if acao == "abrir_caixa":
                conta = conta_caixa
                novo_saldo = _parse_decimal_financeiro(request.POST.get("valor") or request.POST.get("novo_saldo"))
                descricao = request.POST.get("descricao", "").strip() or "Troco inicial / abertura de caixa"
                origem = "abertura_caixa"
            else:
                novo_saldo = _parse_decimal_financeiro(request.POST.get("novo_saldo"))
                descricao = request.POST.get("descricao", "").strip() or "Conferência / ajuste de saldo"
                origem = "ajuste_saldo"

            if not conta or novo_saldo is None or novo_saldo < 0:
                registrar_erro_caixa_local(painel_por_acao_caixa.get(acao, ""), "Informe um saldo zero ou positivo.")
                return redirect("estoque:caixa_banco")

            with transaction.atomic():
                criar_ajuste_para_saldo(conta, novo_saldo, descricao, origem, operador)
            if acao == "abrir_caixa":
                messages.success(request, "Caixa aberto / troco inicial ajustado com sucesso.")
            else:
                messages.success(request, "Conferencia / ajuste de saldo registrado com sucesso.")
            return redirect("estoque:caixa_banco")

        if acao == "ajuste_banco_pix":
            operador = operador_post(painel_por_acao_caixa.get(acao, ""))
            if operador is None:
                return redirect("estoque:caixa_banco")
            valor = _parse_decimal_financeiro(request.POST.get("valor"))
            descricao = request.POST.get("descricao", "").strip() or "Conferencia / ajuste Banco/Pix"
            if not conta_banco:
                registrar_erro_caixa_local(painel_por_acao_caixa.get(acao, ""), "Conta Banco/Pix nao encontrada.")
                return redirect("estoque:caixa_banco")
            if valor is None or valor == 0:
                registrar_erro_caixa_local(painel_por_acao_caixa.get(acao, ""), "Informe um valor diferente de zero para ajustar Banco/Pix.")
                return redirect("estoque:caixa_banco")

            with transaction.atomic():
                MovimentoFinanceiro.objects.create(
                    conta=conta_banco,
                    tipo=MovimentoFinanceiro.TIPO_AJUSTE,
                    valor=valor,
                    data=timezone.localdate(),
                    descricao=descricao,
                    operador=operador,
                    origem="ajuste_banco_pix_conferencia",
                )
            messages.success(request, "Ajuste Banco/Pix registrado com sucesso.")
            return redirect("estoque:caixa_banco")

        if acao in {"fazer_sangria", "depositar_reserva_banco", "reforcar_caixa_reserva", "transferencia"}:
            operador = operador_post(painel_por_acao_caixa.get(acao, ""))
            if operador is None:
                return redirect("estoque:caixa_banco")
            if acao == "fazer_sangria":
                conta_origem = conta_caixa
                conta_destino = conta_reserva
                descricao = request.POST.get("descricao", "").strip() or "Sangria para reserva em mãos"
                origem = "sangria_reserva"
            elif acao == "depositar_reserva_banco":
                conta_origem = conta_reserva
                conta_destino = conta_banco
                descricao = request.POST.get("descricao", "").strip() or "Depósito da reserva no banco"
                origem = "deposito_reserva_banco"
            elif acao == "reforcar_caixa_reserva":
                conta_origem = conta_reserva
                conta_destino = conta_caixa
                descricao = request.POST.get("descricao", "").strip() or "Reforço de caixa com reserva"
                origem = "reforco_caixa_reserva"
            else:
                conta_origem = ContaFinanceira.objects.filter(pk=request.POST.get("conta_origem"), ativo=True).first()
                conta_destino = ContaFinanceira.objects.filter(pk=request.POST.get("conta_destino"), ativo=True).first()
                descricao = request.POST.get("descricao", "").strip() or "Transferencia entre contas"
                origem = "caixa_banco_manual"
            valor = _parse_decimal_financeiro(request.POST.get("valor"))

            with transaction.atomic():
                if not criar_transferencia(conta_origem, conta_destino, valor, descricao, origem, operador, painel_por_acao_caixa.get(acao, "transferencia")):
                    return redirect("estoque:caixa_banco")
            messages.success(request, "Transferencia registrada com sucesso.")
            return redirect("estoque:caixa_banco")

        if acao in {"entrada", "saida", "entrada_avulsa", "saida_avulsa", "pagar_com_reserva"}:
            conta = ContaFinanceira.objects.filter(pk=request.POST.get("conta"), ativo=True).first()
            operador = operador_post(painel_por_acao_caixa.get(acao, ""))
            if operador is None:
                return redirect("estoque:caixa_banco")
            if acao == "pagar_com_reserva":
                conta = conta_reserva
            valor = _parse_decimal_financeiro(request.POST.get("valor"))
            descricao = request.POST.get("descricao", "").strip()
            if acao == "entrada_avulsa":
                descricao = descricao or "Entrada avulsa"
                tipo_movimento = MovimentoFinanceiro.TIPO_ENTRADA
                origem = "entrada_avulsa"
            elif acao == "saida_avulsa":
                descricao = descricao or "Saída avulsa"
                tipo_movimento = MovimentoFinanceiro.TIPO_SAIDA
                origem = "saida_avulsa"
            elif acao == "pagar_com_reserva":
                descricao = descricao or "Pagamento/retirada usando reserva"
                tipo_movimento = MovimentoFinanceiro.TIPO_SAIDA
                origem = "saida_reserva"
            else:
                tipo_movimento = MovimentoFinanceiro.TIPO_ENTRADA if acao == "entrada" else MovimentoFinanceiro.TIPO_SAIDA
                origem = "caixa_banco_manual"

            if not conta or valor is None or valor <= 0:
                registrar_erro_caixa_local(painel_por_acao_caixa.get(acao, ""), "Informe um valor maior que zero.")
                return redirect("estoque:caixa_banco")
            if tipo_movimento == MovimentoFinanceiro.TIPO_SAIDA and _saldo_conta_financeira(conta) < valor:
                saldo_disponivel = _saldo_conta_financeira(conta)
                texto_erro = f"Saldo insuficiente na conta selecionada. Disponivel em {conta.nome}: {_financeiro_moeda_br(saldo_disponivel)}."
                registrar_erro_caixa_local(painel_por_acao_caixa.get(acao, ""), texto_erro)
                return redirect("estoque:caixa_banco")

            with transaction.atomic():
                MovimentoFinanceiro.objects.create(
                    conta=conta,
                    tipo=tipo_movimento,
                    valor=valor,
                    data=timezone.localdate(),
                    descricao=descricao,
                    operador=operador,
                    origem=origem,
                )
            messages.success(request, "Movimento registrado com sucesso.")
            return redirect("estoque:caixa_banco")

        messages.error(request, "Acao invalida.")
        return redirect("estoque:caixa_banco")

    contas = _contas_financeiras_com_saldo()
    saldo_caixa_especie = _saldo_conta_financeira(conta_caixa) if conta_caixa else Decimal("0.00")
    saldo_reserva = _saldo_conta_financeira(conta_reserva) if conta_reserva else Decimal("0.00")
    saldo_banco = _saldo_conta_financeira(conta_banco) if conta_banco else Decimal("0.00")
    saldo_caixa = saldo_caixa_especie + saldo_reserva
    total_disponivel = saldo_caixa + saldo_banco
    movimentos = list(
        MovimentoFinanceiro.objects
        .select_related("conta", "conta_destino")
        .order_by("-data", "-id")[:40]
    )
    for movimento in movimentos:
        movimento.valor_texto = _financeiro_moeda_br(movimento.valor)
        movimento.operador_texto = movimento.operador or "não informado"

    hoje = timezone.localdate()
    if conta_reserva:
        movimentos_reserva_qs = MovimentoFinanceiro.objects.select_related("conta", "conta_destino").filter(
            Q(conta=conta_reserva) | Q(conta_destino=conta_reserva)
        )
    else:
        movimentos_reserva_qs = MovimentoFinanceiro.objects.none()
    entradas_reserva_hoje = _financeiro_dinheiro(
        movimentos_reserva_qs.filter(
            Q(data=hoje, tipo=MovimentoFinanceiro.TIPO_ENTRADA, conta=conta_reserva) |
            Q(data=hoje, tipo=MovimentoFinanceiro.TIPO_TRANSFERENCIA, conta_destino=conta_reserva) |
            Q(data=hoje, tipo=MovimentoFinanceiro.TIPO_AJUSTE, conta=conta_reserva, valor__gt=0)
        ).aggregate(total=Sum("valor"))["total"]
    )
    saidas_reserva_diretas = _financeiro_dinheiro(
        movimentos_reserva_qs.filter(
            Q(data=hoje, tipo=MovimentoFinanceiro.TIPO_SAIDA, conta=conta_reserva) |
            Q(data=hoje, tipo=MovimentoFinanceiro.TIPO_TRANSFERENCIA, conta=conta_reserva)
        ).exclude(
            tipo=MovimentoFinanceiro.TIPO_TRANSFERENCIA,
            conta=conta_reserva,
            conta_destino=conta_banco,
        ).aggregate(total=Sum("valor"))["total"]
    )
    ajustes_reserva_negativos = _financeiro_dinheiro(
        movimentos_reserva_qs.filter(
            data=hoje,
            tipo=MovimentoFinanceiro.TIPO_AJUSTE,
            conta=conta_reserva,
            valor__lt=0,
        ).aggregate(total=Sum("valor"))["total"]
    )
    saidas_reserva_hoje = saidas_reserva_diretas + abs(ajustes_reserva_negativos)
    depositos_reserva_banco_hoje = _financeiro_dinheiro(
        movimentos_reserva_qs.filter(
            data=hoje,
            tipo=MovimentoFinanceiro.TIPO_TRANSFERENCIA,
            conta=conta_reserva,
            conta_destino=conta_banco,
        ).aggregate(total=Sum("valor"))["total"]
    )
    movimentos_reserva = list(movimentos_reserva_qs.order_by("-data", "-id")[:20])
    emprestimos_rapidos_abertos = EmprestimoRapido.objects.filter(status=EmprestimoRapido.STATUS_ABERTO)
    emprestimos_rapidos_total_aberto = _financeiro_dinheiro(
        emprestimos_rapidos_abertos.aggregate(total=Sum("valor"))["total"]
    )
    dividas_a_devolver = EmprestimoDivida.objects.filter(
        status__in=[EmprestimoDivida.STATUS_ABERTO, EmprestimoDivida.STATUS_PARCIAL],
        saldo_devedor__gt=0,
    )
    dividas_a_devolver_total = _financeiro_dinheiro(
        dividas_a_devolver.aggregate(total=Sum("saldo_devedor"))["total"]
    )
    total_considerando_retorno = total_disponivel + emprestimos_rapidos_total_aberto
    for movimento in movimentos_reserva:
        movimento.valor_texto = _financeiro_moeda_br(movimento.valor)
        movimento.operador_texto = movimento.operador or "não informado"

    return render(
        request,
        "estoque/caixa_banco.html",
        {
            "contas": contas,
            "conta_caixa": conta_caixa,
            "conta_reserva": conta_reserva,
            "erro_operacao": erro_operacao,
            "erro_operacao_texto": erro_operacao_texto,
            "conta_banco": conta_banco,
            "saldo_caixa": saldo_caixa,
            "saldo_caixa_texto": _financeiro_moeda_br(saldo_caixa),
            "saldo_caixa_especie": saldo_caixa_especie,
            "saldo_caixa_especie_texto": _financeiro_moeda_br(saldo_caixa_especie),
            "saldo_reserva": saldo_reserva,
            "saldo_reserva_texto": _financeiro_moeda_br(saldo_reserva),
            "saldo_banco": saldo_banco,
            "saldo_banco_texto": _financeiro_moeda_br(saldo_banco),
            "total_disponivel": total_disponivel,
            "total_disponivel_texto": _financeiro_moeda_br(total_disponivel),
            "movimentos": movimentos,
            "operadores_caixa": operadores_caixa,
            "entradas_reserva_hoje_texto": _financeiro_moeda_br(entradas_reserva_hoje),
            "saidas_reserva_hoje_texto": _financeiro_moeda_br(saidas_reserva_hoje),
            "depositos_reserva_banco_hoje_texto": _financeiro_moeda_br(depositos_reserva_banco_hoje),
            "movimentos_reserva": movimentos_reserva,
            "emprestimos_rapidos_abertos_qtd": emprestimos_rapidos_abertos.count(),
            "emprestimos_rapidos_total_aberto": emprestimos_rapidos_total_aberto,
            "emprestimos_rapidos_total_aberto_texto": _financeiro_moeda_br(emprestimos_rapidos_total_aberto),
            "dividas_a_devolver_qtd": dividas_a_devolver.count(),
            "dividas_a_devolver_total": dividas_a_devolver_total,
            "dividas_a_devolver_total_texto": _financeiro_moeda_br(dividas_a_devolver_total),
            "total_considerando_retorno_texto": _financeiro_moeda_br(total_considerando_retorno),
            "hoje": hoje,
        },
    )


def caixa_banco_editar_descricao_movimento(request, movimento_id):
    movimento = get_object_or_404(
        MovimentoFinanceiro.objects.select_related("conta", "conta_destino"),
        pk=movimento_id,
    )
    retorno_url = reverse("estoque:caixa_banco")

    if request.method == "POST":
        movimento.descricao = request.POST.get("descricao", "").strip()
        movimento.save(update_fields=["descricao"])
        messages.success(request, "Descricao do movimento atualizada com sucesso.")
        return redirect(retorno_url)

    movimento.valor_texto = _financeiro_moeda_br(movimento.valor)
    movimento.operador_texto = movimento.operador or "nao informado"
    return render(
        request,
        "estoque/caixa_banco_editar_descricao.html",
        {
            "movimento": movimento,
            "retorno_url": retorno_url,
        },
    )


def _conta_financeira_por_chave(chave):
    chave = (chave or "").strip().lower()
    if chave not in {"caixa", "reserva", "banco"}:
        return None
    return _conta_financeira_padrao(chave)


def _contas_emprestimo_rapido():
    return {
        "caixa": _conta_financeira_padrao("caixa"),
        "reserva": _conta_financeira_padrao("reserva"),
        "banco": _conta_financeira_padrao("banco"),
    }


def _conta_chave(conta):
    if not conta:
        return ""
    nome_normalizado = normalizar_texto_cliente(conta.nome)
    if "reserva" in nome_normalizado or "sangria" in nome_normalizado:
        return "reserva"
    if "banco" in nome_normalizado or "pix" in nome_normalizado:
        return "banco"
    return "caixa"


def _emprestimo_rapido_descricao(emprestimo, acao, conta):
    pessoa = emprestimo.pessoa_nome or "Pessoa nao informada"
    if acao == "saida":
        return f"Emprestimo rapido #{emprestimo.id} para {pessoa} - saida via {conta.nome}"[:255]
    return f"Devolucao de emprestimo rapido #{emprestimo.id} de {pessoa} - entrada via {conta.nome}"[:255]


def emprestimos_rapidos(request):
    _garantir_contas_financeiras_padrao()
    contas = _contas_emprestimo_rapido()
    operadores_caixa = list(Funcionario.operadores_do_caixa().only("id", "nome"))
    operadores_caixa_por_id = {str(funcionario.id): funcionario for funcionario in operadores_caixa}

    def redirect_apos_post():
        if request.POST.get("next") == "caixa_banco":
            return redirect("estoque:caixa_banco")
        return redirect("estoque:emprestimos_rapidos")

    def operador_post():
        funcionario = operadores_caixa_por_id.get(request.POST.get("operador", "").strip())
        if not funcionario:
            messages.error(request, "Selecione um operador autorizado para movimentar Caixa/Banco.")
            return None
        return funcionario.nome

    if request.method == "POST":
        acao = request.POST.get("acao", "").strip()

        if acao == "criar":
            pessoa_nome = (request.POST.get("pessoa_nome") or "").strip()
            valor = _parse_decimal_financeiro(request.POST.get("valor"))
            data_emprestimo = parse_date(request.POST.get("data_emprestimo") or "") or timezone.localdate()
            previsao_devolucao = parse_date(request.POST.get("previsao_devolucao") or "")
            conta_saida = _conta_financeira_por_chave(request.POST.get("conta_saida"))
            observacao = (request.POST.get("observacao") or "").strip()
            operador = operador_post()
            if operador is None:
                return redirect_apos_post()
            if not pessoa_nome:
                messages.error(request, "Informe a pessoa que recebeu o emprestimo.")
                return redirect_apos_post()
            if valor is None or valor <= 0:
                messages.error(request, "Informe um valor maior que zero.")
                return redirect_apos_post()
            if not conta_saida:
                messages.error(request, "Selecione a conta de saida.")
                return redirect_apos_post()
            if _saldo_conta_financeira(conta_saida) < valor:
                messages.error(request, f"Saldo insuficiente em {conta_saida.nome}.")
                return redirect_apos_post()

            with transaction.atomic():
                emprestimo = EmprestimoRapido.objects.create(
                    pessoa_nome=pessoa_nome,
                    valor=valor,
                    data_emprestimo=data_emprestimo,
                    previsao_devolucao=previsao_devolucao,
                    conta_saida=conta_saida,
                    observacao=observacao,
                    operador=operador,
                )
                MovimentoFinanceiro.objects.create(
                    conta=conta_saida,
                    tipo=MovimentoFinanceiro.TIPO_SAIDA,
                    valor=valor,
                    data=data_emprestimo,
                    descricao=_emprestimo_rapido_descricao(emprestimo, "saida", conta_saida),
                    operador=operador,
                    origem="emprestimo_rapido",
                )
            messages.success(request, f"Emprestimo rapido #{emprestimo.id} registrado com sucesso.")
            return redirect_apos_post()

        if acao == "quitar":
            operador = operador_post()
            if operador is None:
                return redirect_apos_post()
            valor_devolvido = _parse_decimal_financeiro(request.POST.get("valor_devolvido"))
            data_quitacao = parse_date(request.POST.get("data_quitacao") or "") or timezone.localdate()
            conta_entrada = _conta_financeira_por_chave(request.POST.get("conta_entrada_quitacao"))
            observacao_quitacao = (request.POST.get("observacao_quitacao") or "").strip()

            with transaction.atomic():
                emprestimo = get_object_or_404(
                    EmprestimoRapido.objects.select_for_update(),
                    pk=request.POST.get("emprestimo_id"),
                )
                if emprestimo.status == EmprestimoRapido.STATUS_QUITADO:
                    messages.error(request, "Este emprestimo rapido ja foi quitado.")
                    return redirect_apos_post()
                if valor_devolvido is None or valor_devolvido <= 0:
                    messages.error(request, "Informe um valor devolvido maior que zero.")
                    return redirect_apos_post()
                if valor_devolvido != emprestimo.valor:
                    messages.error(request, "Nesta primeira versao, a devolucao precisa ser igual ao valor emprestado.")
                    return redirect_apos_post()
                if not conta_entrada:
                    messages.error(request, "Selecione a conta de entrada da devolucao.")
                    return redirect_apos_post()

                MovimentoFinanceiro.objects.create(
                    conta=conta_entrada,
                    tipo=MovimentoFinanceiro.TIPO_ENTRADA,
                    valor=valor_devolvido,
                    data=data_quitacao,
                    descricao=_emprestimo_rapido_descricao(emprestimo, "entrada", conta_entrada),
                    operador=operador,
                    origem="emprestimo_rapido",
                )
                emprestimo.status = EmprestimoRapido.STATUS_QUITADO
                emprestimo.data_quitacao = data_quitacao
                emprestimo.conta_entrada_quitacao = conta_entrada
                emprestimo.valor_devolvido = valor_devolvido
                emprestimo.observacao_quitacao = observacao_quitacao
                emprestimo.operador_quitacao = operador
                emprestimo.save(update_fields=[
                    "status",
                    "data_quitacao",
                    "conta_entrada_quitacao",
                    "valor_devolvido",
                    "observacao_quitacao",
                    "operador_quitacao",
                    "atualizado_em",
                ])
            messages.success(request, f"Devolucao do emprestimo rapido #{emprestimo.id} registrada com sucesso.")
            return redirect_apos_post()

    hoje = timezone.localdate()
    emprestimos = (
        EmprestimoRapido.objects
        .select_related("conta_saida", "conta_entrada_quitacao")
        .order_by("status", "-data_emprestimo", "-id")
    )
    total_aberto = _financeiro_dinheiro(
        emprestimos.filter(status=EmprestimoRapido.STATUS_ABERTO).aggregate(total=Sum("valor"))["total"]
    )
    total_quitado_hoje = _financeiro_dinheiro(
        emprestimos
        .filter(status=EmprestimoRapido.STATUS_QUITADO, data_quitacao=hoje)
        .aggregate(total=Sum("valor_devolvido"))["total"]
    )
    quantidade_abertos = emprestimos.filter(status=EmprestimoRapido.STATUS_ABERTO).count()

    for emprestimo in emprestimos:
        emprestimo.conta_saida_chave = _conta_chave(emprestimo.conta_saida)

    return render(
        request,
        "estoque/emprestimos_rapidos.html",
        {
            "emprestimos": emprestimos,
            "contas": contas,
            "operadores_caixa": operadores_caixa,
            "hoje": hoje,
            "total_aberto": total_aberto,
            "total_quitado_hoje": total_quitado_hoje,
            "quantidade_abertos": quantidade_abertos,
        },
    )


def _parse_int_opcional(valor):
    texto = str(valor or "").strip()
    if not texto:
        return None
    try:
        numero = int(texto)
    except ValueError:
        return None
    return numero if numero > 0 else None


def _emprestimo_divida_post_data(request):
    valor_original = _parse_decimal_financeiro(request.POST.get("valor_original"))
    saldo_devedor = _parse_decimal_financeiro(request.POST.get("saldo_devedor"))
    valor_parcela = _parse_decimal_financeiro(request.POST.get("valor_parcela"))
    data_contratacao = parse_date(request.POST.get("data_contratacao") or "")
    data_vencimento = parse_date(request.POST.get("data_vencimento") or "")
    return {
        "tipo": request.POST.get("tipo") or EmprestimoDivida.TIPO_EMPRESTIMO_RECEBIDO,
        "credor": request.POST.get("credor", "").strip(),
        "descricao": request.POST.get("descricao", "").strip(),
        "valor_original": valor_original,
        "saldo_devedor": saldo_devedor if saldo_devedor is not None else valor_original,
        "data_contratacao": data_contratacao,
        "data_vencimento": data_vencimento,
        "quantidade_parcelas": _parse_int_opcional(request.POST.get("quantidade_parcelas")),
        "valor_parcela": valor_parcela,
        "observacao": request.POST.get("observacao", "").strip(),
    }


def _preparar_divida_template(divida):
    divida.valor_original_texto = _financeiro_moeda_br(divida.valor_original)
    divida.saldo_devedor_texto = _financeiro_moeda_br(divida.saldo_devedor)
    if divida.valor_parcela is not None:
        divida.valor_parcela_texto = _financeiro_moeda_br(divida.valor_parcela)
    else:
        divida.valor_parcela_texto = "-"
    return divida


def _preparar_pagamento_template(pagamento):
    pagamento.valor_texto = _financeiro_moeda_br(pagamento.valor)
    return pagamento


def emprestimos_dividas(request):
    hoje = timezone.localdate()
    fim_7 = hoje + timedelta(days=7)
    emprestimos_rapidos_abertos = (
        EmprestimoRapido.objects
        .select_related("conta_saida")
        .filter(status=EmprestimoRapido.STATUS_ABERTO)
        .order_by("-data_emprestimo", "-id")
    )
    total_a_receber = _financeiro_dinheiro(
        emprestimos_rapidos_abertos.aggregate(total=Sum("valor"))["total"]
    )
    quantidade_a_receber = emprestimos_rapidos_abertos.count()
    emprestimos_rapidos_abertos = list(emprestimos_rapidos_abertos)
    for emprestimo in emprestimos_rapidos_abertos:
        emprestimo.valor_texto = _financeiro_moeda_br(emprestimo.valor)

    dividas_base = EmprestimoDivida.objects.all()
    dividas_abertas_base = dividas_base.filter(
        status__in=[EmprestimoDivida.STATUS_ABERTO, EmprestimoDivida.STATUS_PARCIAL],
        saldo_devedor__gt=0,
    )

    total_aberto = _financeiro_dinheiro(dividas_abertas_base.aggregate(total=Sum("saldo_devedor"))["total"])
    total_vencido = _financeiro_dinheiro(
        dividas_abertas_base
        .filter(data_vencimento__lt=hoje)
        .aggregate(total=Sum("saldo_devedor"))["total"]
    )
    total_7 = _financeiro_dinheiro(
        dividas_abertas_base
        .filter(data_vencimento__range=(hoje, fim_7))
        .aggregate(total=Sum("saldo_devedor"))["total"]
    )
    quantidade_abertas = dividas_abertas_base.count()
    saldo_liquido_emprestimos = total_a_receber - total_aberto

    dividas = dividas_base
    status = request.GET.get("status", "abertas")
    credor = request.GET.get("credor", "").strip()
    vencidas = request.GET.get("vencidas") == "1"

    if status == "abertas":
        dividas = dividas.filter(status__in=[EmprestimoDivida.STATUS_ABERTO, EmprestimoDivida.STATUS_PARCIAL])
    elif status and status != "todas":
        dividas = dividas.filter(status=status)
    if credor:
        dividas = dividas.filter(credor__icontains=credor)
    if vencidas:
        dividas = dividas.filter(
            status__in=[EmprestimoDivida.STATUS_ABERTO, EmprestimoDivida.STATUS_PARCIAL],
            data_vencimento__lt=hoje,
        )

    dividas = list(
        dividas.annotate(
            prioridade=Case(
                When(status__in=[EmprestimoDivida.STATUS_ABERTO, EmprestimoDivida.STATUS_PARCIAL], data_vencimento__lt=hoje, then=Value(0)),
                When(status__in=[EmprestimoDivida.STATUS_ABERTO, EmprestimoDivida.STATUS_PARCIAL], then=Value(1)),
                default=Value(2),
                output_field=IntegerField(),
            )
        )
        .order_by("prioridade", "data_vencimento", "credor", "id")
    )
    dividas = [_preparar_divida_template(divida) for divida in dividas]

    return render(
        request,
        "estoque/emprestimos_dividas.html",
        {
            "dividas": dividas,
            "status": status,
            "credor": credor,
            "vencidas": vencidas,
            "status_choices": EmprestimoDivida.STATUS_CHOICES,
            "emprestimos_rapidos_abertos": emprestimos_rapidos_abertos,
            "total_a_receber_texto": _financeiro_moeda_br(total_a_receber),
            "quantidade_a_receber": quantidade_a_receber,
            "total_a_pagar_texto": _financeiro_moeda_br(total_aberto),
            "saldo_liquido_emprestimos": saldo_liquido_emprestimos,
            "saldo_liquido_emprestimos_texto": _financeiro_moeda_br(saldo_liquido_emprestimos),
            "total_aberto_texto": _financeiro_moeda_br(total_aberto),
            "total_vencido_texto": _financeiro_moeda_br(total_vencido),
            "total_7_texto": _financeiro_moeda_br(total_7),
            "quantidade_abertas": quantidade_abertas,
        },
    )


def emprestimo_divida_nova(request):
    _garantir_contas_financeiras_padrao()
    contas_financeiras = _contas_financeiras_com_saldo()
    hoje = timezone.localdate()
    if request.method == "POST":
        dados = _emprestimo_divida_post_data(request)
        lancar_entrada = request.POST.get("lancar_entrada_financeira") == "on"
        conta_entrada = None
        valor_entrada = None
        if lancar_entrada:
            conta_entrada = ContaFinanceira.objects.filter(pk=request.POST.get("conta_entrada"), ativo=True).first()
            valor_entrada = _parse_decimal_financeiro(request.POST.get("valor_entrada"))
            if valor_entrada is None:
                valor_entrada = dados["valor_original"]
        if not dados["credor"]:
            messages.error(request, "Informe o credor.")
        elif dados["valor_original"] is None or dados["valor_original"] <= 0:
            messages.error(request, "Informe um valor original valido.")
        elif not dados["data_contratacao"]:
            messages.error(request, "Informe uma data de contratacao valida.")
        elif lancar_entrada and not conta_entrada:
            messages.error(request, "Escolha a conta onde o dinheiro entrou.")
        elif lancar_entrada and (valor_entrada is None or valor_entrada <= 0):
            messages.error(request, "Informe um valor de entrada valido.")
        else:
            try:
                with transaction.atomic():
                    divida = EmprestimoDivida.objects.create(**dados)
                    if lancar_entrada:
                        MovimentoFinanceiro.objects.create(
                            conta=conta_entrada,
                            tipo=MovimentoFinanceiro.TIPO_ENTRADA,
                            valor=valor_entrada,
                            data=dados["data_contratacao"],
                            descricao=f"Entrada referente a emprestimo/divida: {divida.credor}",
                            origem="emprestimo_divida",
                        )
            except ValidationError as exc:
                messages.error(request, "; ".join(exc.messages))
            else:
                messages.success(request, "Divida cadastrada com sucesso.")
                return redirect("estoque:emprestimo_divida_detalhe", pk=divida.pk)

    return render(
        request,
        "estoque/emprestimo_divida_form.html",
        {
            "tipo_choices": EmprestimoDivida.TIPO_CHOICES,
            "contas_financeiras": contas_financeiras,
            "hoje": hoje,
        },
    )


def emprestimo_divida_detalhe(request, pk):
    divida = _preparar_divida_template(get_object_or_404(EmprestimoDivida, pk=pk))
    pagamentos = [
        _preparar_pagamento_template(pagamento)
        for pagamento in divida.pagamentos.all()
    ]
    return render(
        request,
        "estoque/emprestimo_divida_detalhe.html",
        {
            "divida": divida,
            "pagamentos": pagamentos,
        },
    )


def emprestimo_divida_baixar(request, pk):
    _garantir_contas_financeiras_padrao()
    divida = get_object_or_404(EmprestimoDivida, pk=pk)
    if request.method == "POST":
        valor_principal = _parse_decimal_financeiro(
            request.POST.get("valor_principal") or request.POST.get("valor")
        )
        juros_acrescimo = _parse_decimal_financeiro(request.POST.get("juros_acrescimo"))
        if juros_acrescimo is None:
            juros_acrescimo = Decimal("0.00")
        data_pagamento = parse_date(request.POST.get("data_pagamento") or "")
        forma_pagamento = request.POST.get("forma_pagamento", "").strip()
        observacao = request.POST.get("observacao", "").strip()
        lancar_saida = request.POST.get("lancar_saida_financeira") == "on"
        conta_saida = None
        if lancar_saida:
            conta_saida = ContaFinanceira.objects.filter(pk=request.POST.get("conta_saida"), ativo=True).first()

        if valor_principal is None or valor_principal <= 0:
            messages.error(request, "Informe um valor principal valido.")
        elif juros_acrescimo < 0:
            messages.error(request, "Juros/acrescimo nao pode ser negativo.")
        elif not data_pagamento:
            messages.error(request, "Informe uma data de pagamento valida.")
        elif valor_principal > divida.saldo_devedor:
            messages.error(request, "O valor principal da baixa nao pode ser maior que o saldo devedor.")
        elif lancar_saida and not conta_saida:
            messages.error(request, "Escolha a conta de onde saiu o dinheiro.")
        else:
            try:
                with transaction.atomic():
                    divida = EmprestimoDivida.objects.select_for_update().get(pk=pk)
                    if valor_principal > divida.saldo_devedor:
                        raise ValidationError("O valor principal da baixa nao pode ser maior que o saldo devedor.")
                    valor_total_pago = (valor_principal + juros_acrescimo).quantize(Decimal("0.01"))
                    principal_texto = _financeiro_moeda_br(valor_principal)
                    juros_texto = _financeiro_moeda_br(juros_acrescimo)
                    total_texto = _financeiro_moeda_br(valor_total_pago)
                    descricao_movimento = (
                        f"Pagamento/devolucao de divida rapida para {divida.credor} - "
                        f"principal {principal_texto} + juros {juros_texto}"
                    )
                    observacao_pagamento = (
                        f"Principal {principal_texto}; juros/acrescimo {juros_texto}; "
                        f"total pago {total_texto}."
                    )
                    if observacao:
                        observacao_pagamento = f"{observacao_pagamento} {observacao}"
                    observacao_pagamento = observacao_pagamento[:255]
                    PagamentoEmprestimoDivida.objects.create(
                        divida=divida,
                        valor=valor_total_pago,
                        data_pagamento=data_pagamento,
                        forma_pagamento=forma_pagamento,
                        observacao=observacao_pagamento,
                    )
                    if lancar_saida:
                        MovimentoFinanceiro.objects.create(
                            conta=conta_saida,
                            tipo=MovimentoFinanceiro.TIPO_SAIDA,
                            valor=valor_total_pago,
                            data=data_pagamento,
                            descricao=descricao_movimento,
                            origem="pagamento_emprestimo_divida",
                        )
                    divida.saldo_devedor = (divida.saldo_devedor - valor_principal).quantize(Decimal("0.01"))
                    divida.atualizar_status_por_saldo()
                    divida.save(update_fields=["saldo_devedor", "status", "atualizado_em"])
            except ValidationError as exc:
                messages.error(request, "; ".join(exc.messages))
            else:
                messages.success(request, "Baixa registrada com sucesso.")
                return redirect("estoque:emprestimo_divida_detalhe", pk=divida.pk)

    divida = _preparar_divida_template(divida)
    return render(
        request,
        "estoque/emprestimo_divida_baixar.html",
        {
            "divida": divida,
            "contas_financeiras": _contas_financeiras_com_saldo(),
            "hoje": timezone.localdate(),
        },
    )


def despesas_diarias(request):
    hoje = timezone.localdate()
    inicio_mes = hoje.replace(day=1)

    contas_saida = ContaFinanceira.objects.filter(ativo=True).order_by("tipo", "nome")
    conta_padrao = contas_saida.filter(nome__icontains="Banco").first() or contas_saida.first()
    operadores_despesa_diaria = Funcionario.objects.filter(
        ativo=True,
        pode_operar_sistema=True,
    ).order_by("nome")

    if request.method == "POST":
        acao = request.POST.get("acao")
        if acao == "excluir":
            despesa = get_object_or_404(DespesaDiaria, pk=request.POST.get("despesa_id"))
            with transaction.atomic():
                movimento = _movimento_despesa_diaria_correspondente(despesa)
                if movimento:
                    movimento.delete()
                despesa.delete()
            messages.success(request, "Despesa excluida junto com o movimento financeiro correspondente.")
            return redirect("estoque:despesas_diarias")

        if acao != "salvar_despesa":
            return redirect("estoque:despesas_diarias")

        try:
            valor = _decimal_compra(request.POST.get("valor"), casas=2)
        except ValueError:
            messages.error(request, "Informe um valor valido.")
            return redirect("estoque:despesas_diarias")

        categoria = request.POST.get("categoria")
        observacao = (request.POST.get("observacao") or "").strip()
        operador = (request.POST.get("operador") or "").strip()
        data_lancamento = parse_date(request.POST.get("data_lancamento") or "") or hoje
        conta_saida = ContaFinanceira.objects.filter(
            pk=request.POST.get("conta_saida"),
            ativo=True,
        ).first()

        categorias_validas = {opcao[0] for opcao in DespesaDiaria.CATEGORIA_CHOICES}
        forma_pagamento = DespesaDiaria.FORMA_PIX
        if conta_saida and conta_saida.tipo == ContaFinanceira.TIPO_CAIXA:
            forma_pagamento = DespesaDiaria.FORMA_DINHEIRO

        if valor <= 0:
            messages.error(request, "Informe um valor maior que zero.")
            return redirect("estoque:despesas_diarias")

        if categoria not in categorias_validas:
            messages.error(request, "Escolha uma categoria valida.")
            return redirect("estoque:despesas_diarias")

        if not operador:
            messages.error(request, "Informe o operador da despesa.")
            return redirect("estoque:despesas_diarias")

        if not conta_saida:
            messages.error(request, "Escolha a conta de saida da despesa.")
            return redirect("estoque:despesas_diarias")

        agora = timezone.localtime()
        data_hora = timezone.make_aware(
            timezone.datetime(
                data_lancamento.year,
                data_lancamento.month,
                data_lancamento.day,
                agora.hour,
                agora.minute,
                agora.second,
            ),
            timezone.get_current_timezone(),
        )

        with transaction.atomic():
            despesa = DespesaDiaria.objects.create(
                data_hora=data_hora,
                valor=valor,
                categoria=categoria,
                forma_pagamento=forma_pagamento,
                observacao=observacao,
            )
            _registrar_movimento_despesa_diaria(despesa, conta_saida)
        messages.success(request, "Despesa salva com sucesso.")
        return redirect("estoque:despesas_diarias")

    data_inicio = parse_date(request.GET.get("data_inicio") or "") or hoje
    data_fim = parse_date(request.GET.get("data_fim") or "") or hoje
    if data_inicio > data_fim:
        data_inicio, data_fim = data_fim, data_inicio

    conta_filtro_id = request.GET.get("conta_saida") or ""
    categoria_filtro = request.GET.get("categoria") or ""

    despesas_periodo = (
        DespesaDiaria.objects
        .filter(data_hora__date__gte=data_inicio, data_hora__date__lte=data_fim)
        .order_by("-data_hora", "-id")
    )

    if categoria_filtro:
        despesas_periodo = despesas_periodo.filter(categoria=categoria_filtro)

    despesas_hoje = DespesaDiaria.objects.filter(data_hora__date=hoje).order_by("-data_hora", "-id")
    resumo_hoje = despesas_hoje.aggregate(total=Sum("valor"), quantidade=Count("id"))

    total_periodo = despesas_periodo.aggregate(total=Sum("valor"), quantidade=Count("id"))
    total_mes = (
        DespesaDiaria.objects
        .filter(data_hora__date__gte=inicio_mes, data_hora__date__lte=hoje)
        .aggregate(total=Sum("valor"))["total"]
        or Decimal("0.00")
    )

    despesas_periodo = list(despesas_periodo)
    despesas_filtradas = []
    conta_filtro_int = None
    if conta_filtro_id:
        try:
            conta_filtro_int = int(conta_filtro_id)
        except (TypeError, ValueError):
            conta_filtro_int = None

    for despesa in despesas_periodo:
        movimento = _movimento_despesa_diaria_correspondente(despesa)
        despesa.movimento_financeiro = movimento
        despesa.conta_saida_nome = movimento.conta.nome if movimento and movimento.conta else "Nao identificada"
        despesa.conta_saida_id = movimento.conta_id if movimento else None
        if conta_filtro_int and despesa.conta_saida_id != conta_filtro_int:
            continue
        despesas_filtradas.append(despesa)

    return render(
        request,
        "estoque/despesas_diarias.html",
        {
            "hoje": hoje,
            "data_inicio": data_inicio,
            "data_fim": data_fim,
            "conta_filtro_id": conta_filtro_id,
            "categoria_filtro": categoria_filtro,
            "despesas_hoje": despesas_hoje,
            "despesas_periodo": despesas_filtradas,
            "total_hoje": resumo_hoje["total"] or Decimal("0.00"),
            "quantidade_hoje": resumo_hoje["quantidade"] or 0,
            "total_periodo": sum((d.valor for d in despesas_filtradas), Decimal("0.00")),
            "quantidade_periodo": len(despesas_filtradas),
            "total_mes": total_mes,
            "categorias": DespesaDiaria.CATEGORIA_CHOICES,
            "formas_pagamento": DespesaDiaria.FORMA_PAGAMENTO_CHOICES,
            "forma_padrao": DespesaDiaria.FORMA_PIX,
            "contas_saida": contas_saida,
            "conta_padrao": conta_padrao,
            "operadores_despesa_diaria": operadores_despesa_diaria,
        },
    )



def _produto_existente_por_nome_normalizado(nome):
    from .forms import normalize_product_name

    nome_normalizado = normalize_product_name(nome or "").casefold()
    if not nome_normalizado:
        return None

    produtos = Produto.objects.filter(excluido=False).only("id", "nome")
    for produto in produtos:
        if normalize_product_name(produto.nome).casefold() == nome_normalizado:
            return produto
    return None


def cadastrar_produto(request):
    criar_mais_produtos = request.GET.get("criar_mais_produtos") == "1"

    if request.method == "POST":
        criar_mais_produtos = request.POST.get("criar_mais_produtos") == "on"
        form = ProdutoForm(request.POST)
        if form.is_valid():
            produto = form.save()
            if criar_mais_produtos:
                return redirect(f"{reverse('estoque:cadastrar_produto')}?criar_mais_produtos=1")
            messages.success(request, f'Produto "{produto.nome}" cadastrado com sucesso!')
            return redirect(f"{reverse('estoque:home')}?produto_destacado={produto.id}")
        else:
            erros_nome = [str(erro) for erro in form.errors.get("nome", [])]
            nome_duplicado = any(
                "existe um produto com esse nome" in erro.casefold()
                for erro in erros_nome
            )
            if nome_duplicado:
                produto_existente = _produto_existente_por_nome_normalizado(request.POST.get("nome", ""))
                if produto_existente:
                    messages.warning(
                        request,
                        f'Produto "{produto_existente.nome}" j? estava cadastrado. Ele foi destacado na lista.'
                    )
                    return redirect(f"{reverse('estoque:home')}?produto_destacado={produto_existente.id}")

            print("ERROS DO FORM:", form.errors)
            print("DADOS RECEBIDOS:", request.POST)
    else:
        form = ProdutoForm()
        
    return render(
        request,
        "estoque/cadastrar_produto.html",
        {
            "form": form,
            "produtos": Produto.objects.all(),
            "criar_mais_produtos": criar_mais_produtos,
        },
    )
def cadastrar_unidade_json_antigo(request):
    if request.method == "POST":
        nome = request.POST.get("nome", "").strip()
        sigla = request.POST.get("sigla", "").strip().upper()
        descricao = request.POST.get("descricao", "").strip()

        if not nome or not sigla:
            return JsonResponse({
                "sucesso": False,
                "mensagem": "Nome e Sigla são obrigatórios."
            })

        Unidade.objects.create(
            nome=nome,
            sigla=sigla,
            descricao=descricao,
            ativa=True
        )

        return JsonResponse({
            "sucesso": True,
            "mensagem": "Unidade cadastrada com sucesso!"
        })

    return JsonResponse({
        "sucesso": False,
        "mensagem": "Método inválido."
    })
def cadastrar_unidade(request):
    if request.method == "POST":
        nome = request.POST.get("nome", "").strip()
        sigla = request.POST.get("sigla", "").strip().upper()
        descricao = request.POST.get("descricao", "").strip()

        if nome and sigla:
            unidade_ja_existe = (
                Unidade.objects.filter(sigla=sigla).exists()
                or Unidade.objects.filter(nome__iexact=nome).exists()
            )

            if not unidade_ja_existe:
                Unidade.objects.create(
                    nome=nome,
                    sigla=sigla,
                    descricao=descricao,
                    ativa=True,
                )

    return redirect("estoque:cadastrar_produto")

def unidades_produto(request):
    termo = request.GET.get("q", "").strip()
    unidade_selecionada = None

    unidades = Unidade.objects.all().order_by("-ativa", "sigla")

    if termo:
        unidades = unidades.filter(
            Q(nome__icontains=termo) |
            Q(sigla__icontains=termo) |
            Q(descricao__icontains=termo)
        )

    if request.method == "POST":
        acao = request.POST.get("acao")
        unidade_id = request.POST.get("unidade_id")

        if acao == "alternar_status" and unidade_id:
            unidade = get_object_or_404(Unidade, pk=unidade_id)
            unidade.ativa = request.POST.get("ativa") == "1"
            unidade.save(update_fields=["ativa"])
            status = "ativada" if unidade.ativa else "desativada"
            messages.success(request, f'Unidade "{unidade.sigla}" {status} com sucesso.')
            params = {"unidade": unidade.id}
            if termo:
                params["q"] = termo
            destino = f"{reverse('estoque:unidades_produto')}?{urlencode(params)}"
            return redirect(destino)

        if unidade_id:
            unidade_selecionada = get_object_or_404(Unidade, pk=unidade_id)
            form = UnidadeForm(request.POST, instance=unidade_selecionada)
        else:
            form = UnidadeForm(request.POST)

        if form.is_valid():
            unidade = form.save()
            messages.success(request, f'Unidade "{unidade.sigla}" salva com sucesso.')
            return redirect(f"{reverse('estoque:unidades_produto')}?unidade={unidade.id}")
        messages.error(request, "Revise os campos destacados para salvar a unidade.")
    else:
        unidade_id = request.GET.get("unidade")
        if unidade_id:
            unidade_selecionada = get_object_or_404(Unidade, pk=unidade_id)
            form = UnidadeForm(instance=unidade_selecionada)
        else:
            form = UnidadeForm(initial={"ativa": True})

    unidades = list(unidades)
    for unidade in unidades:
        unidade.produtos_em_uso = Produto.objects.filter(excluido=False).filter(
            Q(unidade_compra=unidade.sigla) |
            Q(unidade_venda_1=unidade.sigla) |
            Q(unidade_venda_2=unidade.sigla)
        ).count()

    return render(
        request,
        "estoque/unidades_produto.html",
        {
            "form": form,
            "unidades": unidades,
            "termo": termo,
            "unidade_selecionada": unidade_selecionada,
            "total_unidades": len(unidades),
        },
    )

def categorias_produto(request):
    termo = request.GET.get("q", "").strip()
    categoria_selecionada = None

    categorias = Categoria.objects.all().order_by("-ativa", "nome")

    if termo:
        categorias = categorias.filter(
            Q(nome__icontains=termo) |
            Q(descricao__icontains=termo)
        )

    if request.method == "POST":
        acao = request.POST.get("acao")
        categoria_id = request.POST.get("categoria_id")

        if acao == "alternar_status" and categoria_id:
            categoria = get_object_or_404(Categoria, pk=categoria_id)
            categoria.ativa = request.POST.get("ativa") == "1"
            categoria.save(update_fields=["ativa"])
            status = "ativada" if categoria.ativa else "desativada"
            messages.success(request, f'Categoria "{categoria.nome}" {status} com sucesso.')
            params = {"categoria": categoria.id}
            if termo:
                params["q"] = termo
            destino = f"{reverse('estoque:categorias_produto')}?{urlencode(params)}"
            return redirect(destino)

        nome_anterior = None
        if categoria_id:
            categoria_selecionada = get_object_or_404(Categoria, pk=categoria_id)
            nome_anterior = categoria_selecionada.nome
            form = CategoriaForm(request.POST, instance=categoria_selecionada)
        else:
            form = CategoriaForm(request.POST)

        if form.is_valid():
            categoria = form.save()
            if nome_anterior and nome_anterior.casefold() != categoria.nome.casefold():
                Produto.objects.filter(categoria__iexact=nome_anterior).update(
                    categoria=categoria.nome
                )
            messages.success(request, f'Categoria "{categoria.nome}" salva com sucesso.')
            return redirect(f"{reverse('estoque:categorias_produto')}?categoria={categoria.id}")
        messages.error(request, "Revise os campos destacados para salvar a categoria.")
    else:
        categoria_id = request.GET.get("categoria")
        if categoria_id:
            categoria_selecionada = get_object_or_404(Categoria, pk=categoria_id)
            form = CategoriaForm(instance=categoria_selecionada)
        else:
            form = CategoriaForm(initial={"ativa": True})

    categorias = list(categorias)
    for categoria in categorias:
        categoria.produtos_em_uso = Produto.objects.filter(
            excluido=False,
            categoria__iexact=categoria.nome,
        ).count()

    return render(
        request,
        "estoque/categorias_produto.html",
        {
            "form": form,
            "categorias": categorias,
            "termo": termo,
            "categoria_selecionada": categoria_selecionada,
            "total_categorias": len(categorias),
        },
    )

def clientes(request):
    cliente_selecionado = None
    clientes_url = "/estoque/clientes/"
    form_token = request.POST.get("form_token") or uuid4().hex

    if request.method == "POST":
        acao = request.POST.get("acao")
        cliente_id = request.POST.get("cliente_id")

        if acao == "alternar_status" and cliente_id:
            cliente = get_object_or_404(Cliente, pk=cliente_id)
            cliente.ativo = request.POST.get("ativo") == "1"
            cliente.save(update_fields=["ativo", "atualizado_em"])
            status = "ativado" if cliente.ativo else "desativado"
            messages.success(request, f'Cliente "{cliente.nome}" {status} com sucesso.')
            return redirect(f"{clientes_url}?cliente={cliente.id}")

        if cliente_id:
            cliente_selecionado = get_object_or_404(Cliente, pk=cliente_id)
            form = ClienteForm(request.POST, instance=cliente_selecionado)
        else:
            form = ClienteForm(request.POST)

        if form.is_valid():
            tokens_usados = request.session.get("cliente_form_tokens_usados", {})
            if not cliente_id and form_token and form_token in tokens_usados:
                messages.warning(
                    request,
                    "Este envio ja foi processado. O formulario foi limpo para evitar duplicidade.",
                )
                return redirect(clientes_url)

            cliente = form.save(commit=False)

            cliente_duplicado = encontrar_cliente_duplicado(cliente)
            if cliente_duplicado:
                form.add_error(None, MENSAGEM_CLIENTE_DUPLICADO)
                return render(
                    request,
                    "estoque/clientes.html",
                    {
                        "form": form,
                        "cliente_selecionado": cliente_selecionado,
                        "form_token": form_token,
                    },
                )

            cliente.save()
            if not cliente_id and form_token:
                tokens_usados[form_token] = cliente.id
                tokens_itens = list(tokens_usados.items())[-20:]
                request.session["cliente_form_tokens_usados"] = dict(tokens_itens)
                request.session.modified = True

            messages.success(request, f'Cliente "{cliente.nome}" salvo com sucesso.')
            return redirect(f"/estoque/clientes/consulta/?cliente_salvo={cliente.id}")
        messages.error(request, "Revise os campos destacados para salvar o cliente.")
    else:
        cliente_id = request.GET.get("cliente")
        if cliente_id:
            cliente_selecionado = get_object_or_404(Cliente, pk=cliente_id)
            form = ClienteForm(instance=cliente_selecionado)
        else:
            form = ClienteForm(initial={
                "ativo": True,
                "permite_contato_whatsapp": True,
                "status_credito": Cliente.STATUS_CREDITO_LIBERADO,
            })

    return render(
        request,
        "estoque/clientes.html",
        {
            "form": form,
            "cliente_selecionado": cliente_selecionado,
            "form_token": form_token,
        },
    )

def clientes_consulta(request):
    termo = request.GET.get("q", "").strip()
    localidade = request.GET.get("localidade", "").strip()
    clientes_url = "/estoque/clientes/consulta/"

    if request.method == "POST":
        acao = request.POST.get("acao")
        cliente_id = request.POST.get("cliente_id")
        params = {}
        if termo:
            params["q"] = termo
        if localidade:
            params["localidade"] = localidade
        destino = clientes_url
        if params:
            destino = f"{destino}?{urlencode(params)}"

        if acao == "alternar_status" and cliente_id:
            cliente = get_object_or_404(Cliente, pk=cliente_id)
            cliente.ativo = request.POST.get("ativo") == "1"
            cliente.save(update_fields=["ativo", "atualizado_em"])
            status = "ativado" if cliente.ativo else "desativado"
            messages.success(request, f'Cliente "{cliente.nome}" {status} com sucesso.')
            return redirect(destino)

        if acao == "excluir" and cliente_id:
            cliente = get_object_or_404(Cliente, pk=cliente_id)
            if cliente.vendas.exists():
                messages.warning(
                    request,
                    f'Cliente "{cliente.nome}" possui venda vinculada e nao pode ser excluido. Use Desativar.',
                )
            else:
                nome_cliente = cliente.nome
                cliente.delete()
                messages.success(request, f'Cliente "{nome_cliente}" excluido com sucesso.')
            return redirect(destino)

    clientes_qs = Cliente.objects.annotate(total_vendas=Count("vendas")).order_by("-ativo", "nome")

    if termo:
        clientes_qs = clientes_qs.filter(
            Q(nome__icontains=termo) |
            Q(apelido_nome_conhecido__icontains=termo) |
            Q(cpf_cnpj__icontains=termo) |
            Q(whatsapp__icontains=termo) |
            Q(whatsapp_normalizado__icontains=termo)
        )

    if localidade:
        clientes_qs = clientes_qs.filter(
            Q(bairro__icontains=localidade) |
            Q(cidade__icontains=localidade)
        )

    clientes_lista = list(clientes_qs)
    for cliente in clientes_lista:
        cliente.pode_excluir = cliente.total_vendas == 0

    return render(
        request,
        "estoque/clientes_consulta.html",
        {
            "clientes": clientes_lista,
            "termo": termo,
            "localidade": localidade,
            "total_clientes": len(clientes_lista),
            "cliente_salvo_id": request.GET.get("cliente_salvo", ""),
        },
    )


def verificar_cliente_duplicado(request):
    cliente = Cliente(
        nome=request.GET.get("nome", ""),
        apelido_nome_conhecido=request.GET.get("apelido_nome_conhecido", ""),
        cpf_cnpj=request.GET.get("cpf_cnpj", ""),
        whatsapp=request.GET.get("whatsapp", ""),
        bairro=request.GET.get("bairro", ""),
        cidade=request.GET.get("cidade", ""),
    )

    cliente_id = request.GET.get("cliente_id")
    if cliente_id:
        try:
            cliente.pk = int(cliente_id)
        except (TypeError, ValueError):
            cliente.pk = None

    duplicado, campo = detectar_cliente_duplicado(cliente)
    if not duplicado:
        return JsonResponse({"duplicado": False, "mensagem": "", "campo": ""})

    return JsonResponse({
        "duplicado": True,
        "campo": campo or "nome",
        "cliente": duplicado.nome,
        "mensagem": f'Ja existe um cliente parecido cadastrado: "{duplicado.nome}". Verifique antes de cadastrar novamente.',
    })




def _decimal_compra(valor, casas=2, padrao="0"):
    texto = str(valor or "").strip()
    if not texto:
        texto = padrao
    if "," in texto:
        texto = texto.replace(".", "").replace(",", ".")
    else:
        partes = texto.split(".")
        if len(partes) > 2:
            ultima_parte = partes[-1]
            if len(ultima_parte) <= casas:
                texto = "".join(partes[:-1]) + "." + ultima_parte
            else:
                texto = "".join(partes)
    try:
        return Decimal(texto).quantize(Decimal("1").scaleb(-casas))
    except (InvalidOperation, ValueError):
        raise ValueError("Valor numerico invalido.")




def _conta_pagar_payload(conta):
    return {
        "id": conta.id,
        "compra_id": conta.compra_id,
        "data_emissao": conta.data_emissao.strftime("%d/%m/%Y") if conta.data_emissao else "",
        "data_vencimento_iso": conta.data_vencimento.isoformat() if conta.data_vencimento else "",
        "data_vencimento": conta.data_vencimento.strftime("%d/%m/%Y") if conta.data_vencimento else "",
        "valor_original": str(conta.valor_original),
        "valor_em_aberto": str(conta.valor_em_aberto),
        "status": conta.get_status_display(),
        "observacao": conta.observacao or "",
    }


def _pagamento_conta_pagar_payload(pagamento):
    conta = pagamento.conta
    return {
        "id": pagamento.id,
        "conta_id": pagamento.conta_id,
        "compra_id": conta.compra_id if conta else "",
        "data_pagamento": pagamento.data_pagamento.strftime("%d/%m/%Y") if pagamento.data_pagamento else "",
        "valor": str(pagamento.valor),
        "juros_bancarios": str(pagamento.juros_bancarios),
        "total_pago": str((pagamento.valor + pagamento.juros_bancarios).quantize(Decimal("0.01"))),
        "forma_pagamento": pagamento.forma_pagamento or "",
        "observacao": pagamento.observacao or "",
    }




def produto_ultimas_compras(request, produto_id):
    itens = (
        ItemCompra.objects
        .select_related("compra", "compra__fornecedor", "produto")
        .filter(produto_id=produto_id)
        .order_by("-compra__data_compra", "-compra_id", "-id")[:3]
    )

    compras = []
    for item in itens:
        compra = item.compra
        fornecedor = compra.fornecedor.nome if compra and compra.fornecedor else "Fornecedor nao informado"
        compras.append({
            "compra_id": compra.id if compra else "",
            "data": compra.data_compra.strftime("%d/%m/%Y") if compra and compra.data_compra else "",
            "fornecedor": fornecedor,
            "quantidade": str(item.quantidade),
            "unidade": item.unidade or "",
            "preco": str(item.preco_unitario),
            "total": str(item.valor_total),
        })

    return JsonResponse({"compras": compras})


def fornecedor_contas_pagar_abertas(request, fornecedor_id):
    fornecedor = (
        Fornecedor.objects
        .filter(pk=fornecedor_id)
        .values("id", "nome")
        .first()
    )

    contas = list(
        ContaPagar.objects
        .filter(fornecedor_id=fornecedor_id, valor_em_aberto__gt=0)
        .exclude(status__in=[ContaPagar.STATUS_PAGA, ContaPagar.STATUS_CANCELADA])
        .only(
            "id",
            "compra_id",
            "fornecedor_id",
            "data_emissao",
            "data_vencimento",
            "valor_original",
            "valor_em_aberto",
            "status",
            "observacao",
        )
        .order_by("data_vencimento", "id")
    )

    total_aberto = sum((conta.valor_em_aberto for conta in contas), Decimal("0.00"))
    hoje = timezone.localdate()
    contas_vencidas = [
        conta
        for conta in contas
        if conta.data_vencimento and conta.data_vencimento < hoje
    ]
    total_vencido = sum((conta.valor_em_aberto for conta in contas_vencidas), Decimal("0.00"))

    pagamentos_recentes = list(
        PagamentoContaPagar.objects
        .select_related("conta")
        .filter(conta__fornecedor_id=fornecedor_id)
        .only(
            "id",
            "conta_id",
            "data_pagamento",
            "valor",
            "juros_bancarios",
            "forma_pagamento",
            "observacao",
            "conta__id",
            "conta__compra_id",
        )
        .order_by("-data_pagamento", "-id")[:5]
    )

    return JsonResponse({
        "fornecedor_id": fornecedor_id,
        "fornecedor_nome": fornecedor["nome"] if fornecedor else "",
        "total_aberto": str(total_aberto.quantize(Decimal("0.01"))),
        "total_vencido": str(total_vencido.quantize(Decimal("0.01"))),
        "quantidade": len(contas),
        "quantidade_vencidas": len(contas_vencidas),
        "proxima_conta": _conta_pagar_payload(contas[0]) if contas else None,
        "contas": [_conta_pagar_payload(conta) for conta in contas],
        "pagamentos_recentes": [
            _pagamento_conta_pagar_payload(pagamento)
            for pagamento in pagamentos_recentes
        ],
    })



def contas_pagar_abertas_geral(request):
    contas = (
        ContaPagar.objects
        .select_related("compra", "fornecedor")
        .filter(valor_em_aberto__gt=0)
        .exclude(status__in=[ContaPagar.STATUS_PAGA, ContaPagar.STATUS_CANCELADA])
        .order_by("data_vencimento", "fornecedor__nome", "id")
    )

    total_aberto = sum((conta.valor_em_aberto for conta in contas), Decimal("0.00"))

    contas_payload = []
    for conta in contas:
        contas_payload.append({
            "id": conta.id,
            "compra_id": conta.compra_id,
            "fornecedor_id": conta.fornecedor_id,
            "fornecedor": conta.fornecedor.nome if conta.fornecedor else "Fornecedor nao informado",
            "data_vencimento_iso": conta.data_vencimento.isoformat() if conta.data_vencimento else "",
            "data_vencimento": conta.data_vencimento.strftime("%d/%m/%Y") if conta.data_vencimento else "-",
            "valor_original": str(conta.valor_original.quantize(Decimal("0.01"))),
            "valor_em_aberto": str(conta.valor_em_aberto.quantize(Decimal("0.01"))),
            "status": conta.status,
            "status_texto": conta.get_status_display(),
        })

    return JsonResponse({
        "ok": True,
        "total_aberto": str(total_aberto.quantize(Decimal("0.01"))),
        "quantidade": len(contas_payload),
        "contas": contas_payload,
    })


def conta_pagar_baixar(request, pk):
    if request.method != "POST":
        return JsonResponse({"ok": False, "erro": "Metodo nao permitido."}, status=405)

    with transaction.atomic():
        conta = get_object_or_404(
            ContaPagar.objects.select_for_update(),
            pk=pk,
        )

        if conta.status in [ContaPagar.STATUS_PAGA, ContaPagar.STATUS_CANCELADA] or conta.valor_em_aberto <= 0:
            return JsonResponse({"ok": False, "erro": "Esta conta nao esta em aberto."}, status=400)

        try:
            valor = _decimal_compra(request.POST.get("valor_pago") or request.POST.get("valor"), casas=2)
            juros_bancarios = _decimal_compra(request.POST.get("juros_bancarios"), casas=2)
        except ValueError:
            return JsonResponse({"ok": False, "erro": "Informe valores validos para a baixa."}, status=400)

        data_pagamento = parse_date(request.POST.get("data_pagamento") or "") or timezone.localdate()
        forma_pagamento = (request.POST.get("forma_pagamento") or "").strip()
        observacao = (request.POST.get("observacao") or "").strip()

        if valor <= 0:
            return JsonResponse({"ok": False, "erro": "Informe um valor maior que zero."}, status=400)

        if juros_bancarios < 0:
            return JsonResponse({"ok": False, "erro": "Juros bancarios nao podem ser negativos."}, status=400)

        if valor > conta.valor_em_aberto:
            valor = conta.valor_em_aberto

        PagamentoContaPagar.objects.create(
            conta=conta,
            data_pagamento=data_pagamento,
            valor=valor,
            juros_bancarios=juros_bancarios,
            forma_pagamento=forma_pagamento,
            observacao=observacao,
        )

        conta.valor_em_aberto = (conta.valor_em_aberto - valor).quantize(Decimal("0.01"))
        conta.status = ContaPagar.STATUS_PAGA if conta.valor_em_aberto <= 0 else ContaPagar.STATUS_PARCIAL
        conta.save(update_fields=["valor_em_aberto", "status", "atualizado_em"])
        _registrar_movimento_conta_pagar_fornecedor(
            conta,
            valor,
            data_pagamento,
            forma_pagamento,
        )

    return JsonResponse({
        "ok": True,
        "fornecedor_id": conta.fornecedor_id,
        "conta": _conta_pagar_payload(conta),
    })

def _situacao_financeira_compra_lista(compra, hoje=None):
    hoje = hoje or timezone.localdate()
    if compra.cancelada or compra.status == Compra.STATUS_CANCELADA:
        return "Cancelada", "cancelada"
    if _compra_pagamento_imediato(compra.tipo_pagamento):
        return "Nota paga", "paga"

    conta_pagar = getattr(compra, "conta_pagar", None)
    if not conta_pagar:
        return "Financeiro não localizado", "alerta"
    if conta_pagar.status == ContaPagar.STATUS_PAGA or conta_pagar.valor_em_aberto <= Decimal("0.00"):
        return "Nota paga", "paga"
    if conta_pagar.status == ContaPagar.STATUS_CANCELADA:
        return "Conta cancelada", "cancelada"

    parcial = conta_pagar.status == ContaPagar.STATUS_PARCIAL
    vencimento = conta_pagar.data_vencimento
    if not vencimento:
        return ("Parcial" if parcial else "Em aberto"), ("parcial" if parcial else "pendente")
    if vencimento < hoje:
        prefixo = "Parcial - vencida desde" if parcial else "Vencida desde"
        return f"{prefixo} {vencimento.strftime('%d/%m/%Y')}", "vencida"

    prefixo = "Parcial - vence em" if parcial else "Vence em"
    return f"{prefixo} {vencimento.strftime('%d/%m/%Y')}", ("parcial" if parcial else "pendente")


def compras_lista(request):
    compra_filtro = request.GET.get("compra", "").strip().lstrip("#")
    fornecedor_filtro = request.GET.get("fornecedor", "").strip()
    data_inicio = request.GET.get("data_inicio", "").strip()
    data_fim = request.GET.get("data_fim", "").strip()
    pagamento_filtro = request.GET.get("pagamento", "").strip()
    financeiro_filtro = request.GET.get("financeiro", "").strip()

    compras = Compra.objects.select_related("fornecedor", "conta_pagar").order_by("-data_compra", "-id")

    if compra_filtro:
        if compra_filtro.isdigit():
            compras = compras.filter(id=int(compra_filtro))
        else:
            compras = compras.none()

    if fornecedor_filtro:
        compras = compras.filter(fornecedor__nome__icontains=fornecedor_filtro)

    data_inicio_obj = parse_date(data_inicio) if data_inicio else None
    data_fim_obj = parse_date(data_fim) if data_fim else None

    if data_inicio_obj and data_fim_obj and data_inicio_obj > data_fim_obj:
        data_inicio_obj, data_fim_obj = data_fim_obj, data_inicio_obj
        data_inicio, data_fim = data_fim, data_inicio

    if data_inicio_obj:
        compras = compras.filter(data_compra__gte=data_inicio_obj)

    if data_fim_obj:
        compras = compras.filter(data_compra__lte=data_fim_obj)

    if pagamento_filtro == "avista":
        compras = [
            compra for compra in compras
            if _compra_pagamento_imediato(compra.tipo_pagamento)
        ]
    elif pagamento_filtro == "prazo":
        compras = compras.filter(tipo_pagamento__icontains="prazo")
    elif pagamento_filtro == "cartao":
        compras = compras.filter(tipo_pagamento__icontains="cart")
    elif pagamento_filtro == "pix_dinheiro":
        compras = compras.filter(
            Q(tipo_pagamento__icontains="pix")
            | Q(tipo_pagamento__icontains="dinheiro")
            | Q(tipo_pagamento__icontains="vista")
        )

    hoje = timezone.localdate()
    compras_lista_filtrada = []
    for compra in compras:
        compra.situacao_financeira_texto, compra.situacao_financeira_classe = (
            _situacao_financeira_compra_lista(compra, hoje)
        )
        if financeiro_filtro and compra.situacao_financeira_classe != financeiro_filtro:
            continue
        compras_lista_filtrada.append(compra)

    return render(
        request,
        "estoque/compras_lista.html",
        {
            "compras": compras_lista_filtrada,
            "termo": fornecedor_filtro,
            "compra_filtro": compra_filtro,
            "fornecedor_filtro": fornecedor_filtro,
            "data_inicio": data_inicio,
            "data_fim": data_fim,
            "pagamento_filtro": pagamento_filtro,
            "financeiro_filtro": financeiro_filtro,
        },
    )

def compras_nova(request):
    fornecedores = Fornecedor.objects.filter(ativo=True).order_by("nome", "id")
    produtos = Produto.objects.filter(excluido=False).order_by("nome")

    if request.method == "POST":
        fechamento_token = (request.POST.get("fechamento_token") or "").strip() or uuid4().hex
        if Compra.objects.filter(fechamento_token=fechamento_token).exists():
            messages.warning(request, "Esta compra ja foi fechada e lancada no financeiro.")
            return redirect("estoque:compras_lista")
        fornecedor_id = request.POST.get("fornecedor_id")
        data_compra = parse_date(request.POST.get("data_compra") or "")
        tipo_pagamento = (request.POST.get("tipo_pagamento") or "").strip()
        tipo_pagamento_normalizado = (
            tipo_pagamento.lower()
            .replace("à", "a")
            .replace("á", "a")
            .replace(" ", "")
            .replace("_", "")
            .replace("-", "")
        )
        compra_a_prazo = _compra_pagamento_a_prazo(tipo_pagamento)
        compra_a_vista = _compra_pagamento_imediato(tipo_pagamento)
        data_vencimento = parse_date(request.POST.get("data_vencimento") or "")
        observacao = (request.POST.get("observacao") or "").strip()

        produto_ids = request.POST.getlist("produto_id[]")
        quantidades = request.POST.getlist("quantidade[]")
        unidades = request.POST.getlist("unidade[]")
        precos = request.POST.getlist("preco_unitario[]")
        observacoes_itens = request.POST.getlist("observacao_item[]")

        fornecedor = Fornecedor.objects.filter(pk=fornecedor_id, ativo=True).first()
        if not fornecedor:
            messages.error(request, "Selecione um fornecedor ativo.")
            return redirect("estoque:compras_nova")

        if not data_compra:
            messages.error(request, "Informe uma data valida para a compra.")
            return redirect("estoque:compras_nova")

        if compra_a_prazo and not data_vencimento:
            messages.error(request, "Informe o vencimento da compra a prazo.")
            return redirect("estoque:compras_nova")

        itens_validos = []
        try:
            for indice, produto_id in enumerate(produto_ids):
                produto_id = str(produto_id or "").strip()
                if not produto_id:
                    continue

                produto = Produto.objects.filter(pk=produto_id, excluido=False).first()
                if not produto:
                    raise ValueError("Produto informado nao foi encontrado.")

                quantidade = _decimal_compra(quantidades[indice] if indice < len(quantidades) else "", casas=3)
                preco_unitario = _decimal_compra(precos[indice] if indice < len(precos) else "", casas=2)
                unidade = (unidades[indice] if indice < len(unidades) else "").strip()

                if quantidade <= 0:
                    raise ValueError(f"Informe quantidade maior que zero para {produto.nome}.")
                if preco_unitario < 0:
                    raise ValueError(f"Informe preco valido para {produto.nome}.")

                valor_total = (quantidade * preco_unitario).quantize(Decimal("0.01"))
                itens_validos.append({
                    "produto": produto,
                    "quantidade": quantidade,
                    "unidade": unidade,
                    "preco_unitario": preco_unitario,
                    "valor_total": valor_total,
                    "observacao": (observacoes_itens[indice] if indice < len(observacoes_itens) else "").strip(),
                })
        except (ValueError, IndexError) as exc:
            messages.error(request, str(exc))
            return redirect("estoque:compras_nova")

        if not itens_validos:
            messages.error(request, "Inclua pelo menos um item na compra.")
            return redirect("estoque:compras_nova")

        total = sum((item["valor_total"] for item in itens_validos), Decimal("0.00")).quantize(Decimal("0.01"))
        valores_origem = None
        if compra_a_vista:
            try:
                valores_origem = _valores_origem_compra_post(request)
                _validar_origem_compra_a_vista(valores_origem, total)
            except ValueError as exc:
                messages.error(request, str(exc))
                return redirect("estoque:compras_nova")

        try:
            with transaction.atomic():
                compra = Compra.objects.create(
                    fornecedor=fornecedor,
                    data_compra=data_compra,
                    data_vencimento=data_vencimento if compra_a_prazo else None,
                    tipo_pagamento=tipo_pagamento,
                    total=total,
                    observacao=observacao,
                    status=Compra.STATUS_ABERTA,
                    fechamento_token=fechamento_token,
                )

                for item in itens_validos:
                    ItemCompra.objects.create(
                        compra=compra,
                        produto=item["produto"],
                        quantidade=item["quantidade"],
                        unidade=item["unidade"],
                        preco_unitario=item["preco_unitario"],
                        valor_total=item["valor_total"],
                        observacao=item["observacao"] or None,
                    )

                    produto = Produto.objects.select_for_update().get(pk=item["produto"].pk)
                    produto.quantidade = Decimal(str(produto.quantidade or "0")) + item["quantidade"]
                    produto.save(update_fields=["quantidade", "atualizado_em"])

                    ProdutoFornecedor.objects.update_or_create(
                        produto=produto,
                        fornecedor=fornecedor,
                        defaults={
                            "ultimo_preco_compra": item["preco_unitario"],
                            "ultima_compra_em": data_compra,
                        },
                    )

                compra.estoque_entrada_realizada = True
                compra.estoque_entrada_realizada_em = timezone.now()
                compra.status = Compra.STATUS_FINALIZADA
                compra.save(update_fields=["estoque_entrada_realizada", "estoque_entrada_realizada_em", "status", "atualizado_em"])

                if compra_a_prazo:
                    ContaPagar.objects.create(
                        compra=compra,
                        fornecedor=fornecedor,
                        data_emissao=data_compra,
                        data_vencimento=data_vencimento,
                        valor_original=total,
                        valor_em_aberto=total,
                        status=ContaPagar.STATUS_ABERTA,
                        observacao=observacao or "",
                    )
                else:
                    _registrar_movimentos_compra_a_vista(compra, valores_origem)
        except IntegrityError:
            if Compra.objects.filter(fechamento_token=fechamento_token).exists():
                messages.warning(request, "Esta compra ja foi fechada e lancada no financeiro.")
            else:
                logger.exception("Falha de integridade ao fechar compra")
                messages.error(request, "Não foi possível fechar a compra. Nenhum valor foi lançado no financeiro.")
            return redirect("estoque:compras_lista")
        except Exception:
            logger.exception("Falha ao fechar compra e lancar no financeiro")
            messages.error(request, "Não foi possível fechar a compra. Nenhum valor foi lançado no financeiro.")
            return redirect("estoque:compras_nova")

        if compra_a_prazo:
            messages.success(request, "Compra fechada e conta a pagar criada com sucesso.")
        else:
            messages.success(request, "Compra fechada e valores lançados no financeiro com sucesso.")
        return redirect("estoque:compras_lista")

    conta_caixa = _conta_financeira_padrao("caixa")
    conta_reserva = _conta_financeira_padrao("reserva")
    conta_banco = _conta_financeira_padrao("banco")
    saldo_caixa = _saldo_conta_financeira(conta_caixa) if conta_caixa else Decimal("0.00")
    saldo_reserva = _saldo_conta_financeira(conta_reserva) if conta_reserva else Decimal("0.00")
    saldo_banco = _saldo_conta_financeira(conta_banco) if conta_banco else Decimal("0.00")

    return render(
        request,
        "estoque/compras_nova.html",
        {
            "fornecedores": fornecedores,
            "produtos": produtos,
            "hoje": timezone.localdate(),
            "fechamento_token": uuid4().hex,
            "saldo_caixa_modal": _financeiro_moeda_br(saldo_caixa),
            "saldo_reserva_modal": _financeiro_moeda_br(saldo_reserva),
            "saldo_banco_modal": _financeiro_moeda_br(saldo_banco),
        },
    )


def compras_detalhe(request, pk):
    compra = get_object_or_404(
        Compra.objects.select_related("fornecedor").prefetch_related(
            "itens__produto",
            Prefetch("conta_pagar__pagamentos", queryset=PagamentoContaPagar.objects.order_by("-data_pagamento", "-id")),
        ),
        pk=pk,
    )
    conta_pagar = getattr(compra, "conta_pagar", None)
    compra_a_vista = _compra_pagamento_imediato(compra.tipo_pagamento)
    situacao_financeira_texto, _ = _situacao_financeira_compra_lista(compra)
    pagamento_detalhe_texto = compra.tipo_pagamento_texto
    if pagamento_detalhe_texto == "À vista (Dinheiro / Pix)":
        pagamento_detalhe_texto = "À vista"
    alocacao_financeira = _alocacao_financeira_compra(compra)
    correcao_financeira = None
    aviso_erro_lancamento_financeiro = None
    if conta_pagar and not compra_a_vista:
        correcao_financeira = _resumo_correcao_financeira_compra(compra, conta_pagar)
        texto_rastro_financeiro = f"{conta_pagar.observacao or ''}\n{compra.observacao or ''}".lower()
        if "erro de lancamento" in texto_rastro_financeiro and "caixa/banco nao alterado" in texto_rastro_financeiro:
            total_pagamentos_historico = sum(
                (pagamento.valor or Decimal("0.00")) for pagamento in conta_pagar.pagamentos.all()
            ).quantize(Decimal("0.01"))
            diferenca_historica = (total_pagamentos_historico - _financeiro_dinheiro(conta_pagar.valor_original)).quantize(Decimal("0.01"))
            aviso_erro_lancamento_financeiro = {
                "total_conta": _financeiro_dinheiro(conta_pagar.valor_original).quantize(Decimal("0.01")),
                "total_pagamentos": total_pagamentos_historico,
                "diferenca_historica": diferenca_historica,
            }
    total_alocado = sum(alocacao_financeira.values(), Decimal("0.00")).quantize(Decimal("0.01"))
    aviso_financeiro_avista = compra_a_vista and total_alocado != _financeiro_dinheiro(compra.total).quantize(Decimal("0.01"))
    return render(
        request,
        "estoque/compras_detalhe.html",
        {
            "compra": compra,
            "itens": compra.itens.all(),
            "conta_pagar": conta_pagar,
            "movimentos_financeiros": _movimentos_financeiros_compra(compra),
            "alocacao_financeira": alocacao_financeira,
            "compra_a_vista": compra_a_vista,
            "pagamento_detalhe_texto": pagamento_detalhe_texto,
            "situacao_financeira_texto": situacao_financeira_texto,
            "correcao_financeira": correcao_financeira,
            "aviso_erro_lancamento_financeiro": aviso_erro_lancamento_financeiro,
            "aviso_financeiro_avista": aviso_financeiro_avista,
            "total_alocado": total_alocado,
        },
    )


def _registrar_observacao_compra(compra, texto):
    agora = timezone.localtime().strftime("%d/%m/%Y %H:%M")
    observacao_atual = (compra.observacao or "").strip()
    nova_linha = f"[{agora}] {texto}"
    compra.observacao = f"{observacao_atual}\n{nova_linha}".strip() if observacao_atual else nova_linha
    compra.save(update_fields=["observacao", "atualizado_em"])


def _resumo_correcao_financeira_compra(compra, conta_pagar):
    total_compra = _financeiro_dinheiro(compra.total).quantize(Decimal("0.01"))
    if not conta_pagar:
        return None
    valor_original = _financeiro_dinheiro(conta_pagar.valor_original).quantize(Decimal("0.01"))
    total_pago = sum(
        (_financeiro_dinheiro(pagamento.valor) for pagamento in conta_pagar.pagamentos.all()),
        Decimal("0.00"),
    ).quantize(Decimal("0.01"))
    novo_aberto = (total_compra - total_pago).quantize(Decimal("0.01"))
    diferenca = (total_compra - valor_original).quantize(Decimal("0.01"))
    bloqueio = ""
    if conta_pagar.status == ContaPagar.STATUS_PAGA or conta_pagar.valor_em_aberto <= Decimal("0.00"):
        bloqueio = "A conta ja esta quitada e exige uma correcao financeira especifica."
    elif conta_pagar.status == ContaPagar.STATUS_CANCELADA:
        bloqueio = "A conta esta cancelada e nao pode ser ajustada automaticamente."
    elif novo_aberto < Decimal("0.00"):
        bloqueio = "O total ja pago e maior que o novo total da compra. A correcao automatica foi bloqueada."
    return {
        "divergente": diferenca != Decimal("0.00"),
        "total_compra": total_compra,
        "valor_original": valor_original,
        "valor_em_aberto": _financeiro_dinheiro(conta_pagar.valor_em_aberto).quantize(Decimal("0.01")),
        "total_pago": total_pago,
        "novo_valor_em_aberto": novo_aberto,
        "diferenca": diferenca,
        "bloqueio": bloqueio,
        "pode_corrigir": diferenca != Decimal("0.00") and not bloqueio,
    }



def _corrigir_pagamento_simples_compra(compra, novo_pagamento, movimento_financeiro_correcao=''):
    novo_pagamento = str(novo_pagamento or "").strip()
    if novo_pagamento not in {"A prazo", "A vista", "? vista"}:
        return False, ""

    pagamento_anterior = compra.tipo_pagamento or ""
    anterior_prazo = _compra_pagamento_a_prazo(pagamento_anterior)
    novo_prazo = _compra_pagamento_a_prazo(novo_pagamento)

    if anterior_prazo == novo_prazo:
        return False, ""

    conta_pagar = (
        ContaPagar.objects
        .filter(compra=compra)
        .prefetch_related("pagamentos")
        .first()
    )

    if anterior_prazo and not novo_prazo:
        tinha_pagamento = bool(conta_pagar and conta_pagar.pagamentos.exists())
        movimento_financeiro_correcao = str(movimento_financeiro_correcao or "").strip()

        conta_pagamento = None
        if not tinha_pagamento:
            if not movimento_financeiro_correcao:
                raise ValueError("Escolha como tratar o financeiro antes de mudar a compra para A vista.")

            if movimento_financeiro_correcao == "manter":
                conta_pagamento = None
            elif movimento_financeiro_correcao.startswith("pagar:"):
                conta_ref = movimento_financeiro_correcao.split(":", 1)[1]
                contas_padrao = {
                    "caixa": _conta_financeira_padrao("caixa"),
                    "banco": _conta_financeira_padrao("banco"),
                    "reserva": _conta_financeira_padrao("reserva"),
                }
                conta_pagamento = contas_padrao.get(conta_ref)
                if conta_pagamento is None and conta_ref.isdigit():
                    conta_pagamento = ContaFinanceira.objects.filter(pk=conta_ref, ativo=True).first()
                if not conta_pagamento:
                    raise ValueError("Escolha uma conta valida de onde saiu o dinheiro.")

                valor_compra = _financeiro_dinheiro(compra.total).quantize(Decimal("0.01"))
                if _saldo_conta_financeira(conta_pagamento) < valor_compra:
                    raise ValueError(f"Saldo insuficiente em {conta_pagamento.nome}.")
            else:
                raise ValueError("Opcao de pagamento invalida para mudar a compra para A vista.")

        compra.tipo_pagamento = "A vista"
        compra.data_vencimento = None
        compra.save(update_fields=["tipo_pagamento", "data_vencimento", "atualizado_em"])

        if conta_pagar and tinha_pagamento:
            conta_pagar.delete()
            return True, ""

        if conta_pagar:
            conta_pagar.delete()

        if conta_pagamento:
            MovimentoFinanceiro.objects.create(
                conta=conta_pagamento,
                tipo=MovimentoFinanceiro.TIPO_SAIDA,
                valor=_financeiro_dinheiro(compra.total).quantize(Decimal("0.01")),
                data=compra.data_compra or timezone.localdate(),
                descricao=_descricao_compra_a_vista(compra, conta_pagamento),
                operador=compra.operador or "",
                origem="compra_a_vista",
                compra=compra,
            )
        else:
            _registrar_movimentos_compra_a_vista(compra)

        return True, ""

    if not anterior_prazo and novo_prazo:
        movimentos = list(_movimentos_financeiros_compra(compra))
        movimento_financeiro_correcao = str(movimento_financeiro_correcao or "").strip()

        if movimentos and not movimento_financeiro_correcao:
            raise ValueError("Escolha como tratar o dinheiro que ja saiu antes de mudar a compra para A prazo.")

        conta_devolucao = None
        if movimento_financeiro_correcao.startswith("devolver:"):
            conta_ref = movimento_financeiro_correcao.split(":", 1)[1]
            contas_padrao = {
                "caixa": _conta_financeira_padrao("caixa"),
                "banco": _conta_financeira_padrao("banco"),
                "reserva": _conta_financeira_padrao("reserva"),
            }
            conta_devolucao = contas_padrao.get(conta_ref)
            if conta_devolucao is None and conta_ref.isdigit():
                conta_devolucao = ContaFinanceira.objects.filter(pk=conta_ref, ativo=True).first()
            if not conta_devolucao:
                raise ValueError("Escolha uma conta valida para devolver o dinheiro.")
        elif movimento_financeiro_correcao not in {"", "manter"}:
            raise ValueError("Opcao de movimento financeiro invalida.")

        total_estornado = Decimal("0.00")
        if conta_devolucao:
            for movimento in movimentos:
                if movimento.tipo == MovimentoFinanceiro.TIPO_SAIDA:
                    valor_estorno = _financeiro_dinheiro(movimento.valor).quantize(Decimal("0.01"))
                    if valor_estorno <= 0:
                        continue
                    MovimentoFinanceiro.objects.create(
                        conta=conta_devolucao,
                        tipo=MovimentoFinanceiro.TIPO_ENTRADA,
                        valor=valor_estorno,
                        data=timezone.localdate(),
                        descricao=f"Devolucao por mudanca da compra #{compra.id} para A prazo"[:255],
                        operador=compra.operador or "",
                        origem="compra_correcao_pagamento",
                        compra=compra,
                    )
                    total_estornado += valor_estorno

        compra.tipo_pagamento = "A prazo"
        if not compra.data_vencimento:
            compra.data_vencimento = compra.data_compra
        compra.save(update_fields=["tipo_pagamento", "data_vencimento", "atualizado_em"])

        if not conta_pagar:
            total = _financeiro_dinheiro(compra.total).quantize(Decimal("0.01"))
            ContaPagar.objects.create(
                compra=compra,
                fornecedor=compra.fornecedor,
                data_emissao=compra.data_compra or timezone.localdate(),
                data_vencimento=compra.data_vencimento,
                valor_original=total,
                valor_em_aberto=total,
                status=ContaPagar.STATUS_ABERTA,
                observacao="Criada automaticamente ao mudar a compra para A prazo.",
            )

        if conta_devolucao:
            nome_conta = conta_devolucao.nome
            nome_normalizado = nome_conta.lower()

            if "caixa" in nome_normalizado:
                destino = "no Caixa"
            elif "banco" in nome_normalizado or "pix" in nome_normalizado:
                destino = "no Banco/Pix"
            elif "sangria" in nome_normalizado or "reserva" in nome_normalizado:
                destino = "na Sangria/Reserva"
            else:
                destino = f"em {nome_conta}"

            return True, f"Pagamento alterado para A prazo. Devolucao registrada {destino}."

        return True, "Pagamento alterado para A prazo. Conta a Pagar criada."

    return False, ""


def compra_corrigir_itens(request, pk):
    compra = get_object_or_404(
        Compra.objects.select_related("fornecedor").prefetch_related("itens__produto"),
        pk=pk,
    )

    if request.method == "POST":
        novo_tipo_pagamento_compra = request.POST.get("tipo_pagamento_compra", "").strip()
        try:
            ids_postados = [int(valor) for valor in request.POST.getlist("item_id[]")]
            quantidades = request.POST.getlist("quantidade[]")
            precos = request.POST.getlist("preco_unitario[]")
            ids_remover = {int(valor) for valor in request.POST.getlist("remover_item[]")}
            novos_produtos = request.POST.getlist("novo_produto_id[]")
            novas_quantidades = request.POST.getlist("nova_quantidade[]")
            novos_precos = request.POST.getlist("novo_preco_unitario[]")
        except (TypeError, ValueError):
            messages.error(request, "Os itens informados para correcao sao invalidos.")
            return redirect("estoque:compra_corrigir_itens", pk=compra.pk)

        try:
            with transaction.atomic():
                compra = Compra.objects.select_for_update().get(pk=compra.pk)
                itens_atuais = list(
                    ItemCompra.objects.select_for_update()
                    .filter(compra=compra)
                    .order_by("id")
                )
                itens_por_id = {item.id: item for item in itens_atuais}
                if set(ids_postados) != set(itens_por_id):
                    raise ValueError("A lista de itens mudou. Recarregue a pagina antes de corrigir.")
                if len(quantidades) != len(ids_postados) or len(precos) != len(ids_postados):
                    raise ValueError("Preencha quantidade e preco de todos os itens.")

                planos_existentes = []
                deltas_estoque = {}
                novo_total = Decimal("0.00")
                rastros = []

                for indice, item_id in enumerate(ids_postados):
                    item = itens_por_id[item_id]
                    quantidade_antiga = item.quantidade or Decimal("0.000")
                    remover = item_id in ids_remover
                    quantidade_nova = Decimal("0.000") if remover else _decimal_compra(quantidades[indice], casas=3)
                    preco_novo = _decimal_compra(precos[indice], casas=2)
                    if quantidade_nova < Decimal("0.000") or preco_novo < Decimal("0.00"):
                        raise ValueError("Quantidade e preco nao podem ser negativos.")
                    if quantidade_nova == Decimal("0.000"):
                        remover = True

                    subtotal = (quantidade_nova * preco_novo).quantize(Decimal("0.01"))
                    novo_total += subtotal
                    if item.produto_id and compra.estoque_entrada_realizada:
                        deltas_estoque[item.produto_id] = (
                            deltas_estoque.get(item.produto_id, Decimal("0.000"))
                            + quantidade_nova
                            - quantidade_antiga
                        )
                    planos_existentes.append((item, remover, quantidade_nova, preco_novo, subtotal))
                    nome = item.produto.nome if item.produto else "Produto nao identificado"
                    if remover:
                        rastros.append(f"removido {nome} ({quantidade_antiga})")
                    elif quantidade_nova != quantidade_antiga or preco_novo != item.preco_unitario:
                        rastros.append(
                            f"alterado {nome}: qtd {quantidade_antiga} -> {quantidade_nova}; "
                            f"preco {_financeiro_moeda_br(item.preco_unitario)} -> {_financeiro_moeda_br(preco_novo)}"
                        )

                planos_novos = []
                for indice, produto_id_texto in enumerate(novos_produtos):
                    produto_id_texto = str(produto_id_texto or "").strip()
                    quantidade_texto = novas_quantidades[indice] if indice < len(novas_quantidades) else ""
                    preco_texto = novos_precos[indice] if indice < len(novos_precos) else ""
                    if not produto_id_texto and not str(quantidade_texto).strip() and not str(preco_texto).strip():
                        continue
                    if not produto_id_texto:
                        raise ValueError("Selecione o produto do novo item.")
                    produto = Produto.objects.filter(pk=produto_id_texto, excluido=False).first()
                    if not produto:
                        raise ValueError("Um dos novos produtos nao foi encontrado.")
                    quantidade = _decimal_compra(quantidade_texto, casas=3)
                    preco = _decimal_compra(preco_texto, casas=2)
                    if quantidade <= Decimal("0.000"):
                        raise ValueError(f"Informe quantidade maior que zero para {produto.nome}.")
                    if preco < Decimal("0.00"):
                        raise ValueError(f"O preco de {produto.nome} nao pode ser negativo.")
                    subtotal = (quantidade * preco).quantize(Decimal("0.01"))
                    novo_total += subtotal
                    if compra.estoque_entrada_realizada:
                        deltas_estoque[produto.id] = deltas_estoque.get(produto.id, Decimal("0.000")) + quantidade
                    planos_novos.append((produto, quantidade, preco, subtotal))
                    rastros.append(f"adicionado {produto.nome} ({quantidade}) por {_financeiro_moeda_br(subtotal)}")

                if not any(not plano[1] for plano in planos_existentes) and not planos_novos:
                    raise ValueError("A compra precisa permanecer com pelo menos um item.")

                total_anterior = _financeiro_dinheiro(compra.total).quantize(Decimal("0.01"))
                novo_total = novo_total.quantize(Decimal("0.01"))
                diferenca = (novo_total - total_anterior).quantize(Decimal("0.01"))
                pagamento_vai_mudar = bool(
                    novo_tipo_pagamento_compra
                    and _compra_pagamento_a_prazo(compra.tipo_pagamento)
                    != _compra_pagamento_a_prazo(novo_tipo_pagamento_compra)
                )
                if not rastros and diferenca == Decimal("0.00") and not pagamento_vai_mudar:
                    messages.info(request, "Nenhuma alteração de itens foi feita.")
                    return redirect("estoque:compras_detalhe", pk=compra.pk)

                produtos_bloqueados = {
                    produto.id: produto
                    for produto in Produto.objects.select_for_update().filter(pk__in=deltas_estoque)
                }
                for produto_id, delta in deltas_estoque.items():
                    produto = produtos_bloqueados[produto_id]
                    estoque_novo = (produto.quantidade or Decimal("0.000")) + delta
                    if estoque_novo < Decimal("0.000"):
                        raise ValueError(f"A correcao deixaria o estoque de {produto.nome} negativo.")
                    produto.quantidade = estoque_novo
                    produto.save(update_fields=["quantidade", "atualizado_em"])

                for item, remover, quantidade, preco, subtotal in planos_existentes:
                    if remover:
                        item.delete()
                    else:
                        item.quantidade = quantidade
                        item.preco_unitario = preco
                        item.valor_total = subtotal
                        item.save(update_fields=["quantidade", "preco_unitario", "valor_total"])

                for produto, quantidade, preco, subtotal in planos_novos:
                    ItemCompra.objects.create(
                        compra=compra,
                        produto=produto,
                        quantidade=quantidade,
                        unidade=produto.unidade_compra or "",
                        preco_unitario=preco,
                        valor_total=subtotal,
                    )

                compra.total = novo_total
                compra.save(update_fields=["total", "atualizado_em"])

                movimento_financeiro_correcao = request.POST.get("movimento_financeiro_correcao", "").strip()
                pagamento_alterado, resumo_pagamento = _corrigir_pagamento_simples_compra(
                    compra,
                    novo_tipo_pagamento_compra,
                    movimento_financeiro_correcao,
                )

                resumo_rastro = "; ".join(rastros) if rastros else "nenhuma alteracao de itens"
                observacao_correcao = (
                    "Correcao de itens/pagamento: " + resumo_rastro + ". "
                    f"Total anterior {_financeiro_moeda_br(total_anterior)}; "
                    f"novo total {_financeiro_moeda_br(novo_total)}; "
                    f"diferenca {_financeiro_moeda_br(diferenca)}. "
                    "Financeiro dos itens nao alterado nesta etapa."
                )
                if pagamento_alterado and resumo_pagamento:
                    observacao_correcao += " " + resumo_pagamento

                _registrar_observacao_compra(compra, observacao_correcao)
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect("estoque:compra_corrigir_itens", pk=compra.pk)

        if diferenca != Decimal("0.00") and _compra_pagamento_imediato(compra.tipo_pagamento):
            messages.success(
                request,
                "Itens corrigidos. Agora ajuste a origem do pagamento para bater com o novo total.",
            )
            return redirect("estoque:compra_corrigir_origem_pagamento", pk=compra.pk)

        if diferenca != Decimal("0.00") and _compra_pagamento_a_prazo(compra.tipo_pagamento):
            messages.success(
                request,
                "Itens corrigidos. Agora ajuste a Conta a Pagar para bater com o novo total.",
            )
            return redirect("estoque:compra_corrigir_financeiro", pk=compra.pk)

        mudou_somente_pagamento = (
            'pagamento_alterado' in locals()
            and pagamento_alterado
            and not rastros
            and diferenca == Decimal("0.00")
        )

        if mudou_somente_pagamento and 'resumo_pagamento' in locals() and resumo_pagamento:
            mensagem_final = resumo_pagamento
        else:
            mensagem_final = (
                f"Itens corrigidos. Total anterior: {_financeiro_moeda_br(total_anterior)}. "
                f"Novo total: {_financeiro_moeda_br(novo_total)}. Diferenca: {_financeiro_moeda_br(diferenca)}."
            )
            if 'pagamento_alterado' in locals() and pagamento_alterado:
                if 'resumo_pagamento' in locals() and resumo_pagamento:
                    mensagem_final += " " + resumo_pagamento
            else:
                mensagem_final += " Nenhum ajuste financeiro foi realizado."
        messages.success(request, mensagem_final)
        return redirect("estoque:compras_detalhe", pk=compra.pk)

    itens = compra.itens.select_related("produto").all()
    return render(
        request,
        "estoque/compra_corrigir_itens.html",
        {
            "compra": compra,
            "itens": itens,
            "produtos": Produto.objects.filter(excluido=False).order_by("nome"),
        },
    )


def compra_corrigir_financeiro(request, pk):
    compra = get_object_or_404(
        Compra.objects.select_related("fornecedor").prefetch_related("conta_pagar__pagamentos"),
        pk=pk,
    )
    if not _compra_pagamento_a_prazo(compra.tipo_pagamento):
        messages.error(
            request,
            "Esta etapa corrige somente compras a prazo. Para compra a vista, use a correcao de origem do dinheiro.",
        )
        return redirect("estoque:compras_detalhe", pk=compra.pk)

    conta_pagar = getattr(compra, "conta_pagar", None)
    if not conta_pagar:
        messages.error(request, "Esta compra nao possui Conta a Pagar vinculada.")
        return redirect("estoque:compras_detalhe", pk=compra.pk)

    resumo = _resumo_correcao_financeira_compra(compra, conta_pagar)
    if request.method == "POST":
        if request.POST.get("confirmar") != "1":
            messages.error(request, "Confirme o ajuste financeiro antes de continuar.")
            return redirect("estoque:compra_corrigir_financeiro", pk=compra.pk)

        with transaction.atomic():
            compra = Compra.objects.select_for_update().get(pk=compra.pk)
            conta_pagar = (
                ContaPagar.objects.select_for_update()
                .prefetch_related("pagamentos")
                .get(compra=compra)
            )
            resumo = _resumo_correcao_financeira_compra(compra, conta_pagar)
            if not resumo["divergente"]:
                messages.success(request, "A Conta a Pagar ja esta de acordo com o total da compra.")
                return redirect("estoque:compras_detalhe", pk=compra.pk)
            motivo_correcao = (request.POST.get("motivo_correcao") or "").strip()
            quitada_com_bloqueio = (
                resumo["bloqueio"]
                and (conta_pagar.status == ContaPagar.STATUS_PAGA or resumo["valor_em_aberto"] <= Decimal("0.00"))
            )

            motivos_quitada = {
                "erro_lancamento",
                "devolucao_dinheiro",
                "credito_fornecedor",
                "pagar_diferenca",
                "deixar_em_aberto",
            }
            if resumo["bloqueio"] and not (quitada_com_bloqueio and motivo_correcao in motivos_quitada):
                messages.error(request, resumo["bloqueio"])
                return redirect("estoque:compra_corrigir_financeiro", pk=compra.pk)

            if quitada_com_bloqueio and motivo_correcao in motivos_quitada:
                valor_anterior = resumo["valor_original"]
                aberto_anterior = resumo["valor_em_aberto"]
                diferenca_pago_a_maior = max(
                    (resumo["total_pago"] - resumo["total_compra"]).quantize(Decimal("0.01")),
                    Decimal("0.00"),
                )
                diferenca_a_pagar = max(
                    (resumo["total_compra"] - resumo["total_pago"]).quantize(Decimal("0.01")),
                    Decimal("0.00"),
                )

                if motivo_correcao in {"devolucao_dinheiro", "credito_fornecedor"} and diferenca_pago_a_maior <= Decimal("0.00"):
                    messages.error(request, "Esta compra nao tem valor pago a maior. Use pagar diferenca, deixar em aberto ou erro de lancamento.")
                    return redirect("estoque:compra_corrigir_financeiro", pk=compra.pk)

                if motivo_correcao in {"pagar_diferenca", "deixar_em_aberto"} and diferenca_a_pagar <= Decimal("0.00"):
                    messages.error(request, "Esta compra nao tem diferenca a pagar. Use devolucao, credito com fornecedor ou erro de lancamento.")
                    return redirect("estoque:compra_corrigir_financeiro", pk=compra.pk)

                conta_devolucao = None
                conta_pagamento_diferenca = None

                if motivo_correcao == "devolucao_dinheiro":
                    conta_devolucao = ContaFinanceira.objects.filter(
                        pk=request.POST.get("conta_devolucao"),
                        ativo=True,
                    ).first()
                    if not conta_devolucao:
                        messages.error(request, "Escolha a conta onde entrou a devolucao do fornecedor.")
                        return redirect("estoque:compra_corrigir_financeiro", pk=compra.pk)

                if motivo_correcao == "pagar_diferenca":
                    conta_pagamento_diferenca = ContaFinanceira.objects.filter(
                        pk=request.POST.get("conta_pagamento_diferenca"),
                        ativo=True,
                    ).first()
                    if not conta_pagamento_diferenca:
                        messages.error(request, "Escolha a conta usada para pagar a diferenca.")
                        return redirect("estoque:compra_corrigir_financeiro", pk=compra.pk)
                    if _saldo_conta_financeira(conta_pagamento_diferenca) < diferenca_a_pagar:
                        messages.error(request, f"Saldo insuficiente em {conta_pagamento_diferenca.nome}.")
                        return redirect("estoque:compra_corrigir_financeiro", pk=compra.pk)

                conta_pagar.valor_original = resumo["total_compra"]

                agora = timezone.localtime().strftime("%d/%m/%Y %H:%M")
                operador = (compra.operador or "Operador nao informado").strip()

                if motivo_correcao == "erro_lancamento":
                    conta_pagar.valor_em_aberto = Decimal("0.00")
                    conta_pagar.status = ContaPagar.STATUS_PAGA
                    titulo_rastro = "Ajuste financeiro por erro de lancamento"
                    detalhe_extra = "Conta mantida como paga. Caixa/Banco nao alterado."
                    mensagem = "Financeiro ajustado como erro de lancamento da nota. Caixa/Banco nao foi alterado."
                    observacao_compra = (
                        "Financeiro ajustado por erro de lancamento da nota apos correcao de itens. "
                        f"Conta a Pagar {_financeiro_moeda_br(valor_anterior)} -> {_financeiro_moeda_br(conta_pagar.valor_original)}. "
                        "Conta mantida como paga. Caixa/Banco nao alterado."
                    )

                elif motivo_correcao == "devolucao_dinheiro":
                    conta_pagar.valor_em_aberto = Decimal("0.00")
                    conta_pagar.status = ContaPagar.STATUS_PAGA
                    MovimentoFinanceiro.objects.create(
                        conta=conta_devolucao,
                        tipo=MovimentoFinanceiro.TIPO_ENTRADA,
                        valor=diferenca_pago_a_maior,
                        data=timezone.localdate(),
                        descricao=f"Devolucao de fornecedor referente a compra #{compra.id}"[:255],
                        operador=operador,
                        origem="compra_devolucao_fornecedor",
                        compra=compra,
                    )
                    titulo_rastro = "Ajuste financeiro com devolucao de dinheiro do fornecedor"
                    detalhe_extra = (
                        f"Fornecedor devolveu {_financeiro_moeda_br(diferenca_pago_a_maior)} "
                        f"em {conta_devolucao.nome}. Entrada registrada no Caixa/Banco."
                    )
                    mensagem = "Financeiro ajustado com devolucao do fornecedor. Entrada registrada no Caixa/Banco."
                    observacao_compra = (
                        "Financeiro ajustado com devolucao de dinheiro do fornecedor apos correcao de itens. "
                        f"Conta a Pagar {_financeiro_moeda_br(valor_anterior)} -> {_financeiro_moeda_br(conta_pagar.valor_original)}. "
                        f"Devolucao registrada: {_financeiro_moeda_br(diferenca_pago_a_maior)} em {conta_devolucao.nome}."
                    )

                elif motivo_correcao == "credito_fornecedor":
                    conta_pagar.valor_em_aberto = Decimal("0.00")
                    conta_pagar.status = ContaPagar.STATUS_PAGA
                    titulo_rastro = "Ajuste financeiro com credito junto ao fornecedor"
                    detalhe_extra = (
                        f"Credito com fornecedor registrado em historico: {_financeiro_moeda_br(diferenca_pago_a_maior)}. "
                        "Caixa/Banco nao alterado."
                    )
                    mensagem = "Financeiro ajustado e credito com fornecedor registrado no historico. Caixa/Banco nao foi alterado."
                    observacao_compra = (
                        "Financeiro ajustado com credito junto ao fornecedor apos correcao de itens. "
                        f"Conta a Pagar {_financeiro_moeda_br(valor_anterior)} -> {_financeiro_moeda_br(conta_pagar.valor_original)}. "
                        f"Credito registrado em historico: {_financeiro_moeda_br(diferenca_pago_a_maior)}. "
                        "Caixa/Banco nao alterado."
                    )

                elif motivo_correcao == "pagar_diferenca":
                    conta_pagar.valor_em_aberto = Decimal("0.00")
                    conta_pagar.status = ContaPagar.STATUS_PAGA
                    PagamentoContaPagar.objects.create(
                        conta=conta_pagar,
                        data_pagamento=timezone.localdate(),
                        valor=diferenca_a_pagar,
                        forma_pagamento=conta_pagamento_diferenca.nome[:50],
                        observacao=f"Pagamento da diferenca apos correcao da compra #{compra.id}"[:255],
                    )
                    MovimentoFinanceiro.objects.create(
                        conta=conta_pagamento_diferenca,
                        tipo=MovimentoFinanceiro.TIPO_SAIDA,
                        valor=diferenca_a_pagar,
                        data=timezone.localdate(),
                        descricao=f"Pagamento de diferenca da compra #{compra.id}"[:255],
                        operador=operador,
                        origem="compra_pagamento_diferenca",
                        compra=compra,
                    )
                    titulo_rastro = "Ajuste financeiro com pagamento da diferenca"
                    detalhe_extra = (
                        f"Diferenca paga agora: {_financeiro_moeda_br(diferenca_a_pagar)} "
                        f"por {conta_pagamento_diferenca.nome}. Saida registrada no Caixa/Banco."
                    )
                    mensagem = "Financeiro ajustado e diferenca paga. Saida registrada no Caixa/Banco."
                    observacao_compra = (
                        "Financeiro ajustado com pagamento da diferenca apos correcao de itens. "
                        f"Conta a Pagar {_financeiro_moeda_br(valor_anterior)} -> {_financeiro_moeda_br(conta_pagar.valor_original)}. "
                        f"Diferenca paga: {_financeiro_moeda_br(diferenca_a_pagar)} por {conta_pagamento_diferenca.nome}."
                    )

                else:
                    conta_pagar.valor_em_aberto = diferenca_a_pagar
                    conta_pagar.status = ContaPagar.STATUS_PARCIAL if resumo["total_pago"] > Decimal("0.00") else ContaPagar.STATUS_ABERTA
                    titulo_rastro = "Ajuste financeiro com diferenca deixada em aberto"
                    detalhe_extra = (
                        f"Diferenca deixada em aberto: {_financeiro_moeda_br(diferenca_a_pagar)}. "
                        "Caixa/Banco nao alterado."
                    )
                    mensagem = "Financeiro ajustado e diferenca deixada em aberto para pagar depois. Caixa/Banco nao foi alterado."
                    observacao_compra = (
                        "Financeiro ajustado com diferenca deixada em aberto apos correcao de itens. "
                        f"Conta a Pagar {_financeiro_moeda_br(valor_anterior)} -> {_financeiro_moeda_br(conta_pagar.valor_original)}. "
                        f"Diferenca em aberto: {_financeiro_moeda_br(diferenca_a_pagar)}. "
                        "Caixa/Banco nao alterado."
                    )

                rastro = (
                    f"[{agora}] {titulo_rastro} apos correcao de itens por {operador}: "
                    f"valor original {_financeiro_moeda_br(valor_anterior)} -> {_financeiro_moeda_br(conta_pagar.valor_original)}; "
                    f"valor em aberto {_financeiro_moeda_br(aberto_anterior)} -> {_financeiro_moeda_br(conta_pagar.valor_em_aberto)}; "
                    f"total ja pago registrado {_financeiro_moeda_br(resumo['total_pago'])}; "
                    f"pago a maior {_financeiro_moeda_br(diferenca_pago_a_maior)}; "
                    f"diferenca a pagar {_financeiro_moeda_br(diferenca_a_pagar)}. "
                    f"{detalhe_extra}"
                )
                observacao_atual = (conta_pagar.observacao or "").strip()
                conta_pagar.observacao = f"{observacao_atual}\n{rastro}".strip() if observacao_atual else rastro
                conta_pagar.save(update_fields=["valor_original", "valor_em_aberto", "status", "observacao"])

                _registrar_observacao_compra(compra, observacao_compra)
                messages.success(request, mensagem)
                return redirect("estoque:compras_detalhe", pk=compra.pk)

            valor_anterior = resumo["valor_original"]
            aberto_anterior = resumo["valor_em_aberto"]
            conta_pagar.valor_original = resumo["total_compra"]
            conta_pagar.valor_em_aberto = resumo["novo_valor_em_aberto"]
            if resumo["novo_valor_em_aberto"] <= Decimal("0.00"):
                conta_pagar.status = ContaPagar.STATUS_PAGA
            elif resumo["total_pago"] > Decimal("0.00"):
                conta_pagar.status = ContaPagar.STATUS_PARCIAL
            else:
                conta_pagar.status = ContaPagar.STATUS_ABERTA

            agora = timezone.localtime().strftime("%d/%m/%Y %H:%M")
            operador = (compra.operador or "Operador nao informado").strip()
            rastro = (
                f"[{agora}] Ajuste financeiro apos correcao de itens por {operador}: "
                f"valor original {_financeiro_moeda_br(valor_anterior)} -> {_financeiro_moeda_br(conta_pagar.valor_original)}; "
                f"valor em aberto {_financeiro_moeda_br(aberto_anterior)} -> {_financeiro_moeda_br(conta_pagar.valor_em_aberto)}; "
                f"total pago {_financeiro_moeda_br(resumo['total_pago'])}. Caixa/Banco nao alterado."
            )
            observacao_atual = (conta_pagar.observacao or "").strip()
            conta_pagar.observacao = f"{observacao_atual}\n{rastro}".strip() if observacao_atual else rastro
            conta_pagar.save(update_fields=["valor_original", "valor_em_aberto", "status", "observacao", "atualizado_em"])
            _registrar_observacao_compra(compra, rastro.split("] ", 1)[-1])

        messages.success(request, "Financeiro da compra corrigido com sucesso. Caixa/Banco nao foi alterado.")
        return redirect("estoque:compras_detalhe", pk=compra.pk)

    return render(
        request,
        "estoque/compra_corrigir_financeiro.html",
        {
            "compra": compra,
            "conta_pagar": conta_pagar,
            "resumo": resumo,
            "contas_financeiras": _contas_financeiras_com_saldo(),
        },
    )


def compra_corrigir_origem_pagamento(request, pk):
    compra = get_object_or_404(Compra.objects.select_related("fornecedor"), pk=pk)
    if not _compra_pagamento_imediato(compra.tipo_pagamento):
        messages.error(request, "A correcao de origem do pagamento se aplica apenas a compra a vista.")
        return redirect("estoque:compras_detalhe", pk=compra.pk)

    contas = {
        "caixa": _conta_financeira_padrao("caixa"),
        "reserva": _conta_financeira_padrao("reserva"),
        "banco": _conta_financeira_padrao("banco"),
    }
    alocacao_atual = _alocacao_financeira_compra(compra)

    if request.method == "POST":
        try:
            valores = _valores_origem_compra_post(request)
            _validar_origem_compra_a_vista(valores, compra.total)
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect("estoque:compra_corrigir_origem_pagamento", pk=compra.pk)

        with transaction.atomic():
            compra = Compra.objects.select_for_update().get(pk=compra.pk)
            alocacao_atual = _alocacao_financeira_compra(compra)
            movimentos_criados = 0
            diferencas = {}
            for chave, valor_correto in valores.items():
                valor_atual = alocacao_atual.get(chave, Decimal("0.00")).quantize(Decimal("0.01"))
                diferenca = (valor_correto - valor_atual).quantize(Decimal("0.01"))
                diferencas[chave] = diferenca
                if diferenca == Decimal("0.00"):
                    continue
                conta = contas.get(chave)
                if not conta:
                    raise ValueError(f"Conta financeira {chave} nao encontrada para aplicar a correcao.")
                tipo = MovimentoFinanceiro.TIPO_SAIDA if diferenca > 0 else MovimentoFinanceiro.TIPO_ENTRADA
                MovimentoFinanceiro.objects.create(
                    conta=conta,
                    tipo=tipo,
                    valor=abs(diferenca),
                    data=timezone.localdate(),
                    descricao=f"Correcao de origem da compra #{compra.id} - {conta.nome}"[:255],
                    operador=compra.operador or "",
                    origem="compra_correcao_origem",
                    compra=compra,
                )
                movimentos_criados += 1

            if movimentos_criados:
                operador = (compra.operador or "Operador nao informado").strip()
                _registrar_observacao_compra(
                    compra,
                    f"Correcao de origem do dinheiro por {operador}. "
                    "Origem anterior: "
                    f"Caixa {_financeiro_moeda_br(alocacao_atual['caixa'])}; "
                    f"Reserva {_financeiro_moeda_br(alocacao_atual['reserva'])}; "
                    f"Banco/Pix {_financeiro_moeda_br(alocacao_atual['banco'])}. "
                    "Nova origem: "
                    f"Caixa {_financeiro_moeda_br(valores['caixa'])}; "
                    f"Reserva {_financeiro_moeda_br(valores['reserva'])}; "
                    f"Banco/Pix {_financeiro_moeda_br(valores['banco'])}. "
                    "Diferencas lancadas: "
                    f"Caixa {_financeiro_moeda_br(diferencas['caixa'])}; "
                    f"Reserva {_financeiro_moeda_br(diferencas['reserva'])}; "
                    f"Banco/Pix {_financeiro_moeda_br(diferencas['banco'])}.",
                )

        if movimentos_criados:
            messages.success(request, "Origem do pagamento corrigida com movimentos compensatorios rastreaveis.")
        else:
            messages.success(request, "A origem do pagamento ja estava correta. Nenhum movimento novo foi criado.")
        return redirect("estoque:compras_detalhe", pk=compra.pk)

    return render(
        request,
        "estoque/compra_corrigir_origem_pagamento.html",
        {
            "compra": compra,
            "alocacao_atual": alocacao_atual,
            "movimentos_financeiros": _movimentos_financeiros_compra(compra),
        },
    )




def compra_excluir(request, pk):
    if request.method != "POST":
        return redirect("estoque:compras_detalhe", pk=pk)

    with transaction.atomic():
        compra = get_object_or_404(
            Compra.objects.select_for_update().prefetch_related("itens__produto"),
            pk=pk,
        )

        conta = getattr(compra, "conta_pagar", None)

        if conta and conta.pagamentos.exists():
            messages.error(
                request,
                "Esta compra nao pode ser excluida porque ja existe pagamento/baixa na conta a pagar."
            )
            return redirect("estoque:compras_detalhe", pk=compra.pk)

        if compra.estoque_entrada_realizada:
            for item in compra.itens.select_related("produto").all():
                produto = item.produto
                if not produto:
                    continue

                quantidade_atual = produto.quantidade or 0
                quantidade_estorno = int(item.quantidade or 0)
                novo_estoque = quantidade_atual - quantidade_estorno

                if novo_estoque < 0:
                    novo_estoque = 0

                produto.quantidade = novo_estoque
                produto.save(update_fields=["quantidade", "atualizado_em"])

        numero_compra = compra.pk
        compra.delete()

    messages.success(request, f"Compra #{numero_compra} excluida e estoque estornado com sucesso.")
    return redirect("estoque:compras_lista")

def fornecedores(request):
    termo = request.GET.get("q", "").strip()
    fornecedor_selecionado = None
    fornecedores_url = reverse("estoque:fornecedores")

    fornecedores_qs = Fornecedor.objects.prefetch_related(
        Prefetch("contatos", queryset=FornecedorContato.objects.all())
    ).annotate(
        total_compras=Count("compras", distinct=True),
        total_produtos=Count("produtos_fornecedor", distinct=True),
    ).order_by("-ativo", "nome", "id")

    if termo:
        fornecedores_qs = fornecedores_qs.filter(
            Q(nome__icontains=termo) |
            Q(nome_fantasia__icontains=termo) |
            Q(telefone_whatsapp__icontains=termo) |
            Q(cidade__icontains=termo) |
            Q(bairro__icontains=termo) |
            Q(observacao__icontains=termo) |
            Q(contatos__nome__icontains=termo) |
            Q(contatos__cargo__icontains=termo) |
            Q(contatos__telefone_whatsapp__icontains=termo) |
            Q(contatos__telefone_whatsapp_normalizado__icontains=termo)
        ).distinct()

    if request.method == "POST":
        acao = request.POST.get("acao")
        fornecedor_id = request.POST.get("fornecedor_id")

        if acao == "alternar_status" and fornecedor_id:
            fornecedor = get_object_or_404(Fornecedor, pk=fornecedor_id)
            fornecedor.ativo = request.POST.get("ativo") == "1"
            fornecedor.save(update_fields=["ativo", "atualizado_em"])
            status = "ativado" if fornecedor.ativo else "desativado"
            messages.success(request, f'Fornecedor "{fornecedor.nome}" {status} com sucesso.')
            params = {"fornecedor": fornecedor.id}
            if termo:
                params["q"] = termo
            return redirect(f"{fornecedores_url}?{urlencode(params)}")

        post_data = request.POST.copy()
        total_contatos = int(post_data.get("contatos-TOTAL_FORMS") or 0)
        for indice in range(total_contatos):
            prefixo = f"contatos-{indice}"
            contato_id = (post_data.get(f"{prefixo}-id") or "").strip()
            nome = (post_data.get(f"{prefixo}-nome") or "").strip()
            cargo = (post_data.get(f"{prefixo}-cargo") or "").strip()
            telefone = (post_data.get(f"{prefixo}-telefone_whatsapp") or "").strip()
            observacao_contato = (post_data.get(f"{prefixo}-observacao") or "").strip()

            if not contato_id and not nome and not cargo and not telefone and not observacao_contato:
                post_data[f"{prefixo}-DELETE"] = "on"

        if fornecedor_id:
            fornecedor_selecionado = get_object_or_404(Fornecedor, pk=fornecedor_id)
            form = FornecedorForm(post_data, instance=fornecedor_selecionado)
            contatos_formset = FornecedorContatoFormSet(post_data, instance=fornecedor_selecionado, prefix="contatos")
        else:
            form = FornecedorForm(post_data)
            contatos_formset = FornecedorContatoFormSet(post_data, instance=Fornecedor(), prefix="contatos")

        if form.is_valid() and contatos_formset.is_valid():
            fornecedor = form.save()
            contatos_formset.instance = fornecedor
            contatos_formset.save()
            messages.success(request, f'Fornecedor "{fornecedor.nome}" salvo com sucesso.')
            return redirect(f"{fornecedores_url}?fornecedor_salvo={fornecedor.id}")
        messages.error(request, "Revise os campos destacados para salvar o fornecedor.")
    else:
        fornecedor_id = request.GET.get("fornecedor")
        if fornecedor_id:
            fornecedor_selecionado = get_object_or_404(Fornecedor, pk=fornecedor_id)
            form = FornecedorForm(instance=fornecedor_selecionado)
            contatos_formset = FornecedorContatoFormSet(instance=fornecedor_selecionado, prefix="contatos")
        else:
            form = FornecedorForm(initial={"ativo": True})
            contatos_formset = FornecedorContatoFormSet(instance=Fornecedor(), prefix="contatos")

    fornecedores_lista = list(fornecedores_qs)
    fornecedores_ativos = sum(1 for fornecedor in fornecedores_lista if fornecedor.ativo)

    return render(
        request,
        "estoque/fornecedores.html",
        {
            "form": form,
            "contatos_formset": contatos_formset,
            "fornecedores": fornecedores_lista,
            "fornecedor_selecionado": fornecedor_selecionado,
            "termo": termo,
            "total_fornecedores": len(fornecedores_lista),
            "fornecedores_ativos": fornecedores_ativos,
        },
    )



def meios_pagamento(request):
    termo = request.GET.get("q", "").strip()
    meio_selecionado = None
    meios_url = reverse("estoque:cartoes")

    meios_qs = MeioPagamento.objects.all().order_by(
        "-ativo",
        "tipo",
        "-principal",
        "nome",
        "id",
    )

    if termo:
        meios_qs = meios_qs.filter(
            Q(nome__icontains=termo) |
            Q(tipo__icontains=termo) |
            Q(banco_ou_pessoa__icontains=termo) |
            Q(observacao__icontains=termo)
        )

    if request.method == "POST":
        acao = request.POST.get("acao")
        meio_id = request.POST.get("meio_id")

        if acao == "alternar_status" and meio_id:
            meio = get_object_or_404(MeioPagamento, pk=meio_id)
            meio.ativo = request.POST.get("ativo") == "1"
            meio.save(update_fields=["ativo", "atualizado_em"])
            status = "ativado" if meio.ativo else "desativado"
            messages.success(request, f'Cartão "{meio.nome}" {status} com sucesso.')
            return redirect(f"{meios_url}?meio={meio.id}")

        if meio_id:
            meio_selecionado = get_object_or_404(MeioPagamento, pk=meio_id)
            form = MeioPagamentoForm(request.POST, instance=meio_selecionado)
        else:
            form = MeioPagamentoForm(request.POST)

        if form.is_valid():
            meio = form.save()
            messages.success(request, f'Cartão "{meio.nome}" salvo com sucesso.')
            return redirect(f"{meios_url}?meio={meio.id}")

        messages.error(request, "Revise os campos destacados para salvar o meio de pagamento.")
    else:
        meio_id = request.GET.get("meio")
        if meio_id:
            meio_selecionado = get_object_or_404(MeioPagamento, pk=meio_id)
            form = MeioPagamentoForm(instance=meio_selecionado)
        else:
            form = MeioPagamentoForm(initial={
                "ativo": True,
                "principal": False,
            })

    meios_lista = list(meios_qs)
    meios_ativos = sum(1 for meio in meios_lista if meio.ativo)

    return render(
        request,
        "estoque/cartoes.html",
        {
            "form": form,
            "meios": meios_lista,
            "meio_selecionado": meio_selecionado,
            "termo": termo,
            "total_meios": len(meios_lista),
            "meios_ativos": meios_ativos,
        },
    )



def _limpar_contatos_vazios_fornecedor(post_data):
    total_contatos = int(post_data.get("contatos-TOTAL_FORMS") or 0)
    for indice in range(total_contatos):
        prefixo = f"contatos-{indice}"
        contato_id = (post_data.get(f"{prefixo}-id") or "").strip()
        nome = (post_data.get(f"{prefixo}-nome") or "").strip()
        cargo = (post_data.get(f"{prefixo}-cargo") or "").strip()
        telefone = (post_data.get(f"{prefixo}-telefone_whatsapp") or "").strip()
        observacao_contato = (post_data.get(f"{prefixo}-observacao") or "").strip()

        if not contato_id and not nome and not cargo and not telefone and not observacao_contato:
            post_data[f"{prefixo}-DELETE"] = "on"

    return post_data


def fornecedor_novo(request):
    if request.method == "POST":
        post_data = _limpar_contatos_vazios_fornecedor(request.POST.copy())
        form = FornecedorForm(post_data)
        fornecedor_base = Fornecedor()
        contatos_formset = FornecedorContatoFormSet(post_data, instance=fornecedor_base, prefix="contatos")

        if form.is_valid() and contatos_formset.is_valid():
            fornecedor = form.save()
            contatos_formset.instance = fornecedor
            contatos_formset.save()
            messages.success(request, f'Fornecedor "{fornecedor.nome}" salvo com sucesso.')
            return redirect(f"{reverse('estoque:fornecedores')}?fornecedor_salvo={fornecedor.id}")

        messages.error(request, "Revise os campos destacados para salvar o fornecedor.")
    else:
        form = FornecedorForm(initial={"ativo": True})
        contatos_formset = FornecedorContatoFormSet(instance=Fornecedor(), prefix="contatos")

    return render(
        request,
        "estoque/fornecedor_form.html",
        {
            "form": form,
            "contatos_formset": contatos_formset,
            "fornecedor": None,
            "titulo_formulario": "Novo fornecedor",
        },
    )


def fornecedor_editar(request, pk):
    fornecedor = get_object_or_404(Fornecedor, pk=pk)

    if request.method == "POST":
        post_data = _limpar_contatos_vazios_fornecedor(request.POST.copy())
        form = FornecedorForm(post_data, instance=fornecedor)
        contatos_formset = FornecedorContatoFormSet(post_data, instance=fornecedor, prefix="contatos")

        if form.is_valid() and contatos_formset.is_valid():
            fornecedor = form.save()
            contatos_formset.instance = fornecedor
            contatos_formset.save()
            messages.success(request, f'Fornecedor "{fornecedor.nome}" salvo com sucesso.')
            return redirect(f"{reverse('estoque:fornecedores')}?fornecedor_salvo={fornecedor.id}")

        messages.error(request, "Revise os campos destacados para salvar o fornecedor.")
    else:
        form = FornecedorForm(instance=fornecedor)
        contatos_formset = FornecedorContatoFormSet(instance=fornecedor, prefix="contatos")

    return render(
        request,
        "estoque/fornecedor_form.html",
        {
            "form": form,
            "contatos_formset": contatos_formset,
            "fornecedor": fornecedor,
            "titulo_formulario": "Editar fornecedor",
        },
    )


def funcionarios(request):
    termo = request.GET.get("q", "").strip()
    funcionario_selecionado = None
    funcionarios_url = reverse("estoque:funcionarios")

    funcionarios_qs = Funcionario.objects.all().order_by(
        "-ativo",
        "-pode_operar_sistema",
        "-pode_receber_checklist",
        "nome",
    )

    if termo:
        funcionarios_qs = funcionarios_qs.filter(
            Q(nome__icontains=termo) |
            Q(telefone_whatsapp__icontains=termo) |
            Q(telefone_whatsapp_normalizado__icontains=termo) |
            Q(observacoes__icontains=termo)
        )

    if request.method == "POST":
        acao = request.POST.get("acao")
        funcionario_id = request.POST.get("funcionario_id")

        if acao == "alternar_status" and funcionario_id:
            funcionario = get_object_or_404(Funcionario, pk=funcionario_id)
            funcionario.ativo = request.POST.get("ativo") == "1"
            if not funcionario.ativo:
                funcionario.pode_receber_checklist = False
                funcionario.pode_operar_sistema = False
            funcionario.save(update_fields=[
                "ativo",
                "pode_operar_sistema",
                "pode_operar_caixa",
                "pode_receber_checklist",
                "telefone_whatsapp_normalizado",
                "atualizado_em",
            ])
            status = "ativado" if funcionario.ativo else "desativado"
            messages.success(request, f'Funcionario "{funcionario.nome}" {status} com sucesso.')
            params = {"funcionario": funcionario.id}
            if termo:
                params["q"] = termo
            return redirect(f"{funcionarios_url}?{urlencode(params)}")

        if funcionario_id:
            funcionario_selecionado = get_object_or_404(Funcionario, pk=funcionario_id)
            form = FuncionarioForm(request.POST, instance=funcionario_selecionado)
        else:
            form = FuncionarioForm(request.POST)

        if form.is_valid():
            funcionario = form.save()
            messages.success(request, f'Funcionario "{funcionario.nome}" salvo com sucesso.')
            return redirect(f"{funcionarios_url}?funcionario={funcionario.id}")
        messages.error(request, "Revise os campos destacados para salvar o funcionario.")
    else:
        funcionario_id = request.GET.get("funcionario")
        if funcionario_id:
            funcionario_selecionado = get_object_or_404(Funcionario, pk=funcionario_id)
            form = FuncionarioForm(instance=funcionario_selecionado)
        else:
            form = FuncionarioForm(initial={
                "ativo": True,
                "pode_receber_checklist": False,
                "pode_operar_sistema": False,
                "pode_operar_caixa": False,
            })

    funcionarios_lista = list(funcionarios_qs)
    funcionarios_habilitados = sum(
        1 for funcionario in funcionarios_lista
        if funcionario.ativo and funcionario.pode_receber_checklist
    )
    funcionarios_operadores = sum(
        1 for funcionario in funcionarios_lista
        if funcionario.ativo and funcionario.pode_operar_sistema
    )
    funcionarios_operadores_caixa = sum(
        1 for funcionario in funcionarios_lista
        if funcionario.ativo and funcionario.pode_operar_caixa
    )

    return render(
        request,
        "estoque/funcionarios.html",
        {
            "form": form,
            "funcionarios": funcionarios_lista,
            "funcionario_selecionado": funcionario_selecionado,
            "termo": termo,
            "total_funcionarios": len(funcionarios_lista),
            "funcionarios_habilitados": funcionarios_habilitados,
            "funcionarios_operadores": funcionarios_operadores,
            "funcionarios_operadores_caixa": funcionarios_operadores_caixa,
        },
    )


def _resumo_cliente_venda(cliente, hoje=None):
    hoje = hoje or timezone.localdate()
    credito_disponivel = (
        CreditoCliente.objects.filter(cliente=cliente)
        .aggregate(total=Sum("valor"))
        .get("total")
        or Decimal("0.00")
    ).quantize(Decimal("0.01"))
    credito_disponivel = max(credito_disponivel, Decimal("0.00"))

    contas_abertas_qs = ContaReceber.objects.filter(
        cliente=cliente,
        status__in=[ContaReceber.STATUS_ABERTA, ContaReceber.STATUS_PARCIAL],
    )
    contas_abertas = contas_abertas_qs.aggregate(
        quantidade=Count("id"),
        total=Sum("valor_em_aberto"),
    )
    contas_vencidas = contas_abertas_qs.filter(data_vencimento__lt=hoje).aggregate(
        quantidade=Count("id"),
        total=Sum("valor_em_aberto"),
    )
    vencimento_mais_antigo = (
        contas_abertas_qs
        .filter(data_vencimento__lt=hoje)
        .order_by("data_vencimento")
        .values_list("data_vencimento", flat=True)
        .first()
    )
    maior_atraso_dias = (hoje - vencimento_mais_antigo).days if vencimento_mais_antigo else 0
    contas_preview = _montar_contas_preview_cobranca(contas_abertas_qs, hoje)
    whatsapp_cobranca = _montar_whatsapp_cobranca_cliente(
        cliente,
        contas_abertas,
        contas_vencidas,
        maior_atraso_dias,
        contas_preview,
    )
    whatsapp_cobranca["visual_url"] = reverse("estoque:cliente_cobranca_imagem", kwargs={"cliente_id": cliente.id})
    return {
        "id": cliente.id,
        "nome": cliente.nome,
        "prazo": cliente.prazo_padrao_dias or 0,
        "limite": str(cliente.limite_credito or 0),
        "status": cliente.status_credito,
        "status_label": cliente.get_status_credito_display(),
        "whatsapp": cliente.whatsapp or "",
        "financeiro": {
            "credito_disponivel": str(credito_disponivel),
            "contas_abertas_qtd": contas_abertas.get("quantidade") or 0,
            "contas_abertas_total": str(contas_abertas.get("total") or Decimal("0.00")),
            "contas_vencidas_qtd": contas_vencidas.get("quantidade") or 0,
            "contas_vencidas_total": str(contas_vencidas.get("total") or Decimal("0.00")),
        },
        "whatsapp_cobranca": whatsapp_cobranca,
    }


def clientes_autocomplete(request):
    termo = request.GET.get("q", "").strip()
    contexto = request.GET.get("contexto", "").strip()
    cliente_id = request.GET.get("cliente_id", "").strip()
    clientes_qs = Cliente.objects.filter(ativo=True).order_by("nome")
    hoje = timezone.localdate()

    if cliente_id.isdigit():
        cliente = clientes_qs.filter(pk=cliente_id).first()
        if not cliente:
            return JsonResponse({"clientes": []})
        dados_cliente = _resumo_cliente_venda(cliente, hoje)
        dados_cliente["documento"] = cliente.cpf_cnpj or ""
        dados_cliente["telefone"] = cliente.whatsapp or cliente.telefone_alternativo or ""
        return JsonResponse({"clientes": [dados_cliente]})

    if termo:
        if contexto == "pix_detalhe":
            partes = normalizar_texto_cliente(termo).split()
            clientes_filtrados = []
            for cliente in clientes_qs:
                nome_normalizado = normalizar_texto_cliente(cliente.nome)
                apelido_normalizado = normalizar_texto_cliente(cliente.apelido_nome_conhecido)
                documento_normalizado = normalizar_documento_cliente(cliente.cpf_cnpj)
                whatsapp_normalizado = normalizar_documento_cliente(cliente.whatsapp or cliente.whatsapp_normalizado)
                termo_documento = normalizar_documento_cliente(termo)
                if all(
                    parte in nome_normalizado
                    or parte in apelido_normalizado
                    or (parte.isdigit() and parte in documento_normalizado)
                    or (parte.isdigit() and parte in whatsapp_normalizado)
                    for parte in partes
                ) or (termo_documento and (termo_documento in documento_normalizado or termo_documento in whatsapp_normalizado)):
                    clientes_filtrados.append(cliente)

            termo_normalizado = " ".join(partes)
            clientes_qs = sorted(
                clientes_filtrados,
                key=lambda cliente: (
                    not normalizar_texto_cliente(cliente.nome).startswith(termo_normalizado),
                    not normalizar_texto_cliente(cliente.apelido_nome_conhecido).startswith(termo_normalizado),
                    cliente.nome or "",
                ),
            )
        else:
            for parte in termo.split():
                clientes_qs = clientes_qs.filter(
                    Q(nome__icontains=parte) |
                    Q(apelido_nome_conhecido__icontains=parte) |
                    Q(whatsapp__icontains=parte) |
                    Q(whatsapp_normalizado__icontains=parte)
                )

    limite = 12 if contexto == "pix_detalhe" else 12

    clientes = []
    calcular_financeiro_completo = contexto in {"venda", "venda_detalhe"}

    for cliente in clientes_qs[:limite]:
        if calcular_financeiro_completo:
            dados_cliente = _resumo_cliente_venda(cliente, hoje)
        else:
            dados_cliente = {
                "id": cliente.id,
                "nome": cliente.nome,
                "prazo": cliente.prazo_padrao_dias or 0,
                "limite": str(cliente.limite_credito or Decimal("0.00")),
                "status": cliente.status_credito,
                "status_label": cliente.get_status_credito_display() if hasattr(cliente, "get_status_credito_display") else cliente.status_credito,
                "whatsapp": cliente.whatsapp or "",
                "financeiro": {
                    "credito_disponivel": "0.00",
                    "contas_abertas_qtd": 0,
                    "contas_abertas_total": "0.00",
                    "contas_vencidas_qtd": 0,
                    "contas_vencidas_total": "0.00",
                },
                "whatsapp_cobranca": {
                    "tem_whatsapp": bool(cliente.whatsapp),
                    "url": "",
                    "numero": cliente.whatsapp or "",
                    "mensagem": "",
                    "maior_atraso_dias": 0,
                    "contas": [],
                    "visual_url": "",
                },
            }

        dados_cliente["documento"] = cliente.cpf_cnpj or ""
        dados_cliente["telefone"] = cliente.whatsapp or cliente.telefone_alternativo or ""
        clientes.append(dados_cliente)

    return JsonResponse({"clientes": clientes})


def _clientes_pix_autocomplete_local(limite=500):
    clientes = []
    clientes_qs = Cliente.objects.filter(ativo=True).order_by("nome").only(
        "id",
        "nome",
        "apelido_nome_conhecido",
        "cpf_cnpj",
        "whatsapp",
        "whatsapp_normalizado",
        "telefone_alternativo",
    )[:limite]
    for cliente in clientes_qs:
        busca = " ".join(
            parte
            for parte in [
                normalizar_texto_cliente(cliente.nome),
                normalizar_texto_cliente(cliente.apelido_nome_conhecido),
                normalizar_documento_cliente(cliente.cpf_cnpj),
                normalizar_documento_cliente(cliente.whatsapp),
                normalizar_documento_cliente(cliente.whatsapp_normalizado),
                normalizar_documento_cliente(cliente.telefone_alternativo),
            ]
            if parte
        )
        clientes.append({
            "id": cliente.id,
            "nome": cliente.nome or "Cliente sem nome",
            "busca": busca,
        })
    return clientes


def cliente_cobranca_imagem(request, cliente_id):
    cliente = get_object_or_404(Cliente, pk=cliente_id)
    resumo = _resumo_cliente_venda(cliente)
    financeiro = resumo["financeiro"]
    cobranca = resumo["whatsapp_cobranca"]
    buffer = _gerar_cobranca_cliente_imagem(cliente, financeiro, cobranca)
    response = HttpResponse(buffer.getvalue(), content_type="image/png")
    response["Content-Disposition"] = f'inline; filename="cobranca-cliente-{cliente.id}.png"'
    return response


def _url_retorno_segura(request):
    proxima_url = request.GET.get("next", "").strip()
    if proxima_url and url_has_allowed_host_and_scheme(
        url=proxima_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return proxima_url
    return ""


def _querystring_retorno(retorno_url):
    return urlencode({"next": retorno_url}) if retorno_url else ""


def _formatar_data_cobranca(valor):
    return valor.strftime("%d/%m/%Y") if valor else ""


def _montar_contas_preview_cobranca(contas_qs, hoje):
    contas = []
    for conta in contas_qs.select_related("venda").order_by("data_vencimento", "id"):
        vencimento = conta.data_vencimento
        atraso_dias = (hoje - vencimento).days if vencimento and vencimento < hoje else 0
        if atraso_dias == 1:
            status_texto = "Vencida há 1 dia"
        elif atraso_dias > 1:
            status_texto = f"Vencida há {atraso_dias} dias"
        else:
            status_texto = "Em dia"
        venda = conta.venda
        contas.append({
            "titulo": f"Venda #{venda.id}" if venda else f"Conta #{conta.id}",
            "data": _formatar_data_cobranca((venda.data_venda if venda else None) or conta.data_emissao),
            "vencimento": _formatar_data_cobranca(vencimento),
            "valor": _formatar_moeda(conta.valor_em_aberto or Decimal("0.00")),
            "status": status_texto,
            "vencida": atraso_dias > 0,
        })
    return contas


def _montar_whatsapp_cobranca_cliente(cliente, contas_abertas, contas_vencidas, maior_atraso_dias=0, contas_preview=None):
    numero = Cliente.normalizar_whatsapp(cliente.whatsapp_normalizado or cliente.whatsapp)
    if numero and len(numero) in (10, 11):
        numero = "55" + numero

    linhas_mensagem = [
        f"Olá, {cliente.nome}.",
        "",
        "Segue atualização das contas em aberto.",
        "",
        "Obrigado.",
        "LA Neiva",
    ]
    mensagem = "\n".join(linhas_mensagem)
    return {
        "tem_whatsapp": bool(numero),
        "url": f"https://web.whatsapp.com/send?phone={numero}&text={quote(mensagem)}" if numero else "",
        "numero": numero,
        "mensagem": mensagem,
        "maior_atraso_dias": maior_atraso_dias,
        "contas": contas_preview or [],
    }


def _url_com_retorno(url, retorno_url):
    if not retorno_url:
        return url
    separador = "&" if "?" in url else "?"
    return f"{url}{separador}{_querystring_retorno(retorno_url)}"


def _numero_whatsapp_cliente(cliente):
    if not cliente:
        return ""
    numero = Cliente.normalizar_whatsapp(cliente.whatsapp_normalizado or cliente.whatsapp)
    return _normalizar_numero_whatsapp_evento(numero)


def _montar_mensagem_confirmacao_recebimento(dados):
    forma_pagamento = str(dados.get("forma_pagamento") or "").strip().upper()
    texto_comprovante = (
        "Segue o comprovante de pagamento realizado por Pix."
        if forma_pagamento == "PIX"
        else "Segue comprovante de pagamento."
    )
    linhas = [
        f"Olá, {dados['cliente_nome']}.",
        "",
        texto_comprovante,
        "",
        "Obrigado.",
        "L A Neiva",
    ]
    return "\n".join(linhas)


def _montar_whatsapp_confirmacao_recebimento(cliente, dados):
    numero = _numero_whatsapp_cliente(cliente)
    mensagem = _montar_mensagem_confirmacao_recebimento(dados)
    return {
        "tem_whatsapp": bool(numero),
        "numero": numero,
        "url": f"https://wa.me/{numero}?text={quote(mensagem)}" if numero else "",
        "mensagem": mensagem,
    }


def _serializar_dados_comprovante_recebimento(cliente, dados):
    return {
        "cliente_id": cliente.id,
        "cliente_nome": dados["cliente_nome"],
        "data_recebimento": dados["data_recebimento"],
        "saldo_anterior": str(dados["saldo_anterior"]),
        "valor_pago": str(dados["valor_pago"]),
        "forma_pagamento": dados["forma_pagamento"],
        "saldo_atual": str(dados["saldo_atual"]),
        "credito_gerado": str(dados["credito_gerado"]),
        "contas": [
            {
                "conta_id": conta["conta_id"],
                "venda_id": conta["venda_id"],
                "data_nota": conta.get("data_nota") or "",
                "saldo_antes": str(conta.get("saldo_antes") or "0.00"),
                "nota_inteira_antes": bool(conta.get("nota_inteira_antes")),
                "valor_aplicado": str(conta["valor_aplicado"]),
                "saldo_restante": str(conta.get("saldo_restante") or "0.00"),
                "quitada": bool(conta["quitada"]),
            }
            for conta in dados.get("contas", [])
        ],
        "contas_abertas": [
            {
                "conta_id": conta["conta_id"],
                "venda_id": conta["venda_id"],
                "data_nota": conta.get("data_nota") or "",
                "saldo_atual": str(conta["saldo_atual"]),
                "dias_aberto": int(conta.get("dias_aberto") or 0),
                "em_atraso": bool(conta.get("em_atraso")),
            }
            for conta in dados.get("contas_abertas", [])
        ],
    }


def produto_detalhe(request, pk):
    produto = get_object_or_404(Produto, pk=pk)
    return render(request, "estoque/produto_detalhe.html", {"produto": produto})
def produto_editar(request, pk):
    produto = get_object_or_404(Produto, pk=pk)
    retorno_url = request.GET.get("next") or request.POST.get("next") or ""
    if not url_has_allowed_host_and_scheme(retorno_url, allowed_hosts={request.get_host()}):
        retorno_url = ""

    if request.method == "POST":
        form = ProdutoForm(request.POST, instance=produto)
        if form.is_valid():
            form.save()
            return redirect(f"{reverse('estoque:home')}?produto_destacado={produto.id}")
        else:
            print("ERROS DO FORM EDITAR:", form.errors)
            print("DADOS RECEBIDOS EDITAR:", request.POST)
    else:
        form = ProdutoForm(instance=produto)

    return render(request, "estoque/cadastrar_produto.html", {"form": form, "retorno_url": retorno_url})
def produto_excluir(request, pk):
    produto = get_object_or_404(Produto, pk=pk)
    produto.excluido = True
    produto.excluido_em = timezone.now()
    produto.save()
    return redirect("estoque:home")
def verificar_produto(request):
    nome = request.GET.get("nome", "").strip()
    produto_id = request.GET.get("produto_id")

    query = Produto.objects.filter(nome__iexact=nome)

    if produto_id:
     query = query.exclude(id=produto_id)

    existe = query.exists()
    return JsonResponse({"existe": existe})
def lixeira(request):
    produtos = Produto.objects.filter(excluido=True)
    return render(request, "estoque/lixeira.html", {"produtos": produtos})
def produto_restaurar(request, pk):
    produto = get_object_or_404(Produto, pk=pk)
    Produto.objects.filter(pk=produto.pk).update(
        excluido=False,
        excluido_em=None,
    )
    return redirect("estoque:lixeira")
def produto_excluir_definitivo(request, pk):
    produto = get_object_or_404(Produto, pk=pk)

    if request.method == "POST":
        produto.delete()

    return redirect("estoque:lixeira")
def _calcular_resumo_vendas(vendas):
    total_vista = Decimal("0.00")
    total_prazo = Decimal("0.00")
    total_geral = Decimal("0.00")
    quantidade = 0

    for venda in vendas:
        quantidade += 1
        valor_venda = venda.total or Decimal("0.00")
        total_geral += valor_venda

        tipo_pagamento = (venda.tipo_pagamento or "").strip().casefold()
        if "prazo" in tipo_pagamento:
            total_prazo += valor_venda
        else:
            total_vista += valor_venda

    return {
        "vista": total_vista,
        "prazo": total_prazo,
        "geral": total_geral,
        "quantidade": quantidade,
    }


@ensure_csrf_cookie
def vendas(request):
    produtos = Produto.objects.filter(excluido=False).order_by('nome')
    cliente_inicial = None
    cliente_id = request.GET.get("cliente_id")
    pedido_importado = None
    pedido_importado_aviso = ""
    if cliente_id:
        cliente = Cliente.objects.filter(pk=cliente_id, ativo=True).first()
        if cliente:
            cliente_inicial = _resumo_cliente_venda(cliente)
    pedido_id = request.GET.get("pedido_id")
    if pedido_id:
        from .models import Pedido

        pedido = (
            Pedido.objects.select_related("cliente")
            .prefetch_related("itens__produto")
            .filter(pk=pedido_id)
            .first()
        )
        if not pedido:
            pedido_importado_aviso = "Pedido nao encontrado. A venda foi aberta sem importacao."
        elif pedido.status not in [Pedido.STATUS_ABERTO, Pedido.STATUS_PARCIAL]:
            pedido_importado_aviso = "Apenas pedidos abertos ou parciais podem ser preparados para venda nesta etapa."
        else:
            if pedido.cliente:
                cliente_inicial = _resumo_cliente_venda(pedido.cliente)
            itens_importados = []
            itens_pedido = list(pedido.itens.all())
            if pedido.status == Pedido.STATUS_PARCIAL:
                itens_para_importar, _total_pendente = _itens_pendentes_exibicao_pedido_parcial(pedido, itens_pedido)
            else:
                itens_para_importar = itens_pedido
            for item in itens_para_importar:
                if item.quantidade <= 0:
                    continue
                produto_nome = item.produto.nome if item.produto else ""
                if not produto_nome:
                    continue
                itens_importados.append({
                    "produto_id": item.produto_id,
                    "produto_nome": produto_nome,
                    "quantidade": str(item.quantidade),
                    "unidade": item.unidade or "",
                    "preco_unitario": str(item.preco_unitario),
                    "valor_total": str(item.valor_total),
                })
            pedido_importado = {
                "id": pedido.id,
                "cliente_id": pedido.cliente_id,
                "detalhe_url": reverse("estoque:pedido_detalhe", args=[pedido.id]),
                "itens": itens_importados,
            }
            pedido_importado_aviso = (
                f"Venda preparada a partir do Pedido #{pedido.id}. "
                "Confira os itens antes de gravar."
            )
    operadores_venda = Funcionario.objects.filter(ativo=True, pode_operar_sistema=True).order_by('nome')
    hoje = timezone.localdate()
    vendas_hoje = Venda.objects.filter(
        cancelada=False,
        data_venda=hoje,
    ).only("total", "tipo_pagamento")
    resumo_vendas_hoje = _calcular_resumo_vendas(vendas_hoje)

    return render(request, 'estoque/vendas_layout_teste.html', {
        'produtos': produtos,
        'operadores_venda': operadores_venda,
        'cliente_inicial': cliente_inicial,
        'pedido_importado': pedido_importado,
        'pedido_importado_aviso': pedido_importado_aviso,
        'tem_pix_em_atencao': _tem_pix_em_atencao(),
        'resumo_vendas_hoje': resumo_vendas_hoje,
    })


def vendas_cliente_produto_historico(request):
    cliente_id = request.GET.get("cliente_id", "").strip()
    produto_id = request.GET.get("produto_id", "").strip()
    if not cliente_id or not produto_id:
        return JsonResponse({"sucesso": True, "historico": None})

    try:
        cliente_id_int = int(cliente_id)
        produto_id_int = int(produto_id)
    except ValueError:
        return JsonResponse({"sucesso": False, "mensagem": "Cliente ou produto invalido."}, status=400)

    item = (
        ItemVenda.objects.select_related("venda")
        .filter(
            venda__cliente_id=cliente_id_int,
            venda__cancelada=False,
            produto_id=produto_id_int,
        )
        .order_by("-venda__data_venda", "-venda_id", "-id")
        .first()
    )
    if not item:
        return JsonResponse({"sucesso": True, "historico": None})

    return JsonResponse({
        "sucesso": True,
        "historico": {
            "venda_id": item.venda_id,
            "data_venda": item.venda.data_venda.isoformat() if item.venda.data_venda else "",
            "preco_unitario": str((item.preco_unitario or Decimal("0.00")).quantize(Decimal("0.01"))),
            "quantidade": str(item.quantidade),
            "unidade": item.unidade or "",
        },
    })


@ensure_csrf_cookie
def consultar_vendas(request, mostrar_canceladas=False):
    hoje = timezone.localdate()
    primeira_abertura = not request.GET

    data_inicial_texto = request.GET.get("data_inicial", "").strip()
    data_final_texto = request.GET.get("data_final", "").strip()
    cliente_texto = request.GET.get("cliente", "").strip()
    numero_texto = request.GET.get("numero", "").strip()

    if primeira_abertura:
        data_inicial = hoje
        data_final = hoje
        data_inicial_texto = hoje.isoformat()
        data_final_texto = hoje.isoformat()
    else:
        data_inicial = parse_date(data_inicial_texto) if data_inicial_texto else None
        data_final = parse_date(data_final_texto) if data_final_texto else None

    vendas_qs = (
        Venda.objects.select_related("cliente")
        .prefetch_related("itens__produto", "eventos")
        .filter(cancelada=mostrar_canceladas)
        .order_by("-data_venda", "-id")
    )

    if not numero_texto and data_inicial:
        vendas_qs = vendas_qs.filter(data_venda__gte=data_inicial)
    elif not numero_texto and data_inicial_texto:
        messages.warning(request, "Data inicial invalida. O filtro foi ignorado.")

    if not numero_texto and data_final:
        vendas_qs = vendas_qs.filter(data_venda__lte=data_final)
    elif not numero_texto and data_final_texto:
        messages.warning(request, "Data final invalida. O filtro foi ignorado.")

    if cliente_texto:
        vendas_qs = vendas_qs.filter(
            Q(cliente__nome__icontains=cliente_texto) |
            Q(cliente__apelido_nome_conhecido__icontains=cliente_texto) |
            Q(cliente__cpf_cnpj__icontains=cliente_texto) |
            Q(cliente__whatsapp__icontains=cliente_texto)
        )

    if numero_texto:
        try:
            vendas_qs = vendas_qs.filter(pk=int(numero_texto))
        except ValueError:
            vendas_qs = vendas_qs.none()
            messages.warning(request, "Numero da venda invalido. Informe apenas numeros.")

    vendas_lista = list(vendas_qs)

    resumo_vendas_filtradas = _calcular_resumo_vendas(vendas_lista)
    total_vendas_vista = resumo_vendas_filtradas["vista"]
    total_vendas_prazo = resumo_vendas_filtradas["prazo"]
    total_vendas_geral = resumo_vendas_filtradas["geral"]

    for venda in vendas_lista:
        venda.whatsapp_url_consulta = "" if venda.cancelada else montar_link_whatsapp_venda(venda)
        venda.whatsapp_status_selos = (
            [{"texto": "Cancelada / venda nao realizada", "classe": "cancelada"}]
            if venda.cancelada
            else _status_whatsapp_consulta_venda(venda)
        )

    return render(
        request,
        "estoque/vendas_consulta.html",
        {
            "vendas": vendas_lista,
            "total_vendas": len(vendas_lista),
            "total_vendas_vista": total_vendas_vista,
            "total_vendas_prazo": total_vendas_prazo,
            "total_vendas_geral": total_vendas_geral,
            "data_inicial": data_inicial_texto,
            "data_final": data_final_texto,
            "cliente": cliente_texto,
            "numero": numero_texto,
            "primeira_abertura": primeira_abertura,
            "mostrar_canceladas": mostrar_canceladas,
        },
    )


@ensure_csrf_cookie
def consultar_vendas_canceladas(request):
    return consultar_vendas(request, mostrar_canceladas=True)


@ensure_csrf_cookie
def contas_receber(request):
    cliente_texto = request.GET.get("cliente", "").strip()
    data_inicial_texto = request.GET.get("data_inicial", "").strip()
    data_final_texto = request.GET.get("data_final", "").strip()
    status_filtro = request.GET.get("status", "em_aberto").strip() or "em_aberto"
    retorno_url = _url_retorno_segura(request)

    data_inicial = parse_date(data_inicial_texto) if data_inicial_texto else None
    data_final = parse_date(data_final_texto) if data_final_texto else None
    status_validos = {
        ContaReceber.STATUS_ABERTA,
        ContaReceber.STATUS_PARCIAL,
        ContaReceber.STATUS_PAGA,
        ContaReceber.STATUS_CANCELADA,
        "em_aberto",
        "todas",
    }

    if status_filtro not in status_validos:
        status_filtro = "em_aberto"
        messages.warning(request, "Status invalido. Mostrando contas em aberto.")

    contas_qs = (
        ContaReceber.objects.select_related("cliente", "venda")
        .order_by("data_vencimento", "data_emissao", "id")
    )

    if status_filtro == "em_aberto":
        contas_qs = contas_qs.filter(status__in=[ContaReceber.STATUS_ABERTA, ContaReceber.STATUS_PARCIAL])
    elif status_filtro != "todas":
        contas_qs = contas_qs.filter(status=status_filtro)

    if data_inicial:
        contas_qs = contas_qs.filter(data_vencimento__gte=data_inicial)
    elif data_inicial_texto:
        messages.warning(request, "Data inicial invalida. O filtro foi ignorado.")

    if data_final:
        contas_qs = contas_qs.filter(data_vencimento__lte=data_final)
    elif data_final_texto:
        messages.warning(request, "Data final invalida. O filtro foi ignorado.")

    if cliente_texto:
        contas_qs = contas_qs.filter(
            Q(cliente__nome__icontains=cliente_texto)
            | Q(cliente__apelido_nome_conhecido__icontains=cliente_texto)
            | Q(cliente__cpf_cnpj__icontains=cliente_texto)
            | Q(cliente__whatsapp__icontains=cliente_texto)
        )

    contas_recebimento_cliente = None
    contas_recebimento_cliente_total = Decimal("0.00")
    contas_recebimento_cliente_qtd = 0
    if cliente_texto and status_filtro == "em_aberto":
        clientes_abertos = list(
            contas_qs.exclude(cliente__isnull=True)
            .values("cliente_id", "cliente__nome")
            .annotate(qtd=Count("id"), total=Sum("valor_em_aberto"))
            .order_by("cliente__nome")[:2]
        )
        if len(clientes_abertos) == 1:
            contas_recebimento_cliente = clientes_abertos[0]
            contas_recebimento_cliente_qtd = contas_recebimento_cliente["qtd"] or 0
            contas_recebimento_cliente_total = contas_recebimento_cliente["total"] or Decimal("0.00")

    creditos_qs = CreditoCliente.objects.select_related("cliente")
    if cliente_texto:
        creditos_qs = creditos_qs.filter(
            Q(cliente__nome__icontains=cliente_texto)
            | Q(cliente__apelido_nome_conhecido__icontains=cliente_texto)
            | Q(cliente__cpf_cnpj__icontains=cliente_texto)
            | Q(cliente__whatsapp__icontains=cliente_texto)
        )

    clientes_com_credito = []
    creditos_agrupados = (
        creditos_qs.values("cliente_id", "cliente__nome")
        .annotate(total_credito=Sum("valor"), credito_mais_recente=Max("criado_em"))
        .filter(total_credito__gt=Decimal("0.00"))
        .order_by("cliente__nome")
    )
    for credito in creditos_agrupados:
        ultimo_credito = (
            creditos_qs.filter(cliente_id=credito["cliente_id"])
            .order_by("-criado_em", "-id")
            .first()
        )
        clientes_com_credito.append(
            {
                "cliente_id": credito["cliente_id"],
                "cliente_nome": credito["cliente__nome"],
                "total_credito": credito["total_credito"] or Decimal("0.00"),
                "credito_mais_recente": credito["credito_mais_recente"],
                "observacao": (ultimo_credito.observacao if ultimo_credito else ""),
            }
        )

    contas = list(contas_qs)
    return render(
        request,
        "estoque/contas_receber.html",
        {
            "contas": contas,
            "total_contas": len(contas),
            "clientes_com_credito": clientes_com_credito,
            "cliente": cliente_texto,
            "data_inicial": data_inicial_texto,
            "data_final": data_final_texto,
            "retorno_url": retorno_url,
            "detalhe_credito_retorno_url": request.get_full_path(),
            "contas_recebimento_cliente": contas_recebimento_cliente,
            "contas_recebimento_cliente_qtd": contas_recebimento_cliente_qtd,
            "contas_recebimento_cliente_total": contas_recebimento_cliente_total,
            "status_filtro": status_filtro,
            "status_opcoes": (
                ("em_aberto", "Em aberto"),
                (ContaReceber.STATUS_ABERTA, "Abertas"),
                (ContaReceber.STATUS_PARCIAL, "Parciais"),
                (ContaReceber.STATUS_PAGA, "Pagas"),
                (ContaReceber.STATUS_CANCELADA, "Canceladas"),
                ("todas", "Todas"),
            ),
            "tem_pix_em_atencao": _tem_pix_em_atencao(),
        },
    )


@ensure_csrf_cookie
def central_pix(request):
    retorno_url = request.GET.get("next", "").strip()
    if not (
        retorno_url.startswith("/")
        and not retorno_url.startswith("//")
        and url_has_allowed_host_and_scheme(
            url=retorno_url,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        )
    ):
        retorno_url = ""
    central_pix_url = reverse("estoque:central_pix")
    central_pix_atual_url = request.get_full_path()
    termo_busca_pix = request.GET.get("q", "").strip()
    data_inicio_pix = request.GET.get("data_inicio", "").strip()
    data_fim_pix = request.GET.get("data_fim", "").strip()
    status_pix = request.GET.get("status", "").strip()
    cliente_pix = request.GET.get("cliente", "").strip()
    data_inicio_filtro = parse_date(data_inicio_pix)
    data_fim_filtro = parse_date(data_fim_pix)
    status_validos_pix = {status for status, _rotulo in PixRecebido.STATUS_CHOICES}
    if status_pix not in status_validos_pix:
        status_pix = ""

    def pix_recebidos_filtrados():
        pix_qs = PixRecebido.objects.select_related("cliente", "cliente_sugerido").order_by("-criado_em", "-id")
        if status_pix:
            pix_qs = pix_qs.filter(status=status_pix)

        termo_normalizado = normalizar_texto_cliente(termo_busca_pix)
        termo_documento = normalizar_documento_cliente(termo_busca_pix)
        termo_valor = termo_busca_pix.replace("R$", "").strip().replace(".", "").replace(",", ".")
        cliente_normalizado = normalizar_texto_cliente(cliente_pix)
        cliente_documento = normalizar_documento_cliente(cliente_pix)
        resultado = []
        for pix in pix_qs:
            data_pix = timezone.localtime(pix.data_pagamento).strftime("%d/%m/%Y %H:%M") if pix.data_pagamento else ""
            data_pix_date = timezone.localtime(pix.data_pagamento).date() if pix.data_pagamento else None
            if data_inicio_filtro and (not data_pix_date or data_pix_date < data_inicio_filtro):
                continue
            if data_fim_filtro and (not data_pix_date or data_pix_date > data_fim_filtro):
                continue
            data_cadastro = timezone.localtime(pix.criado_em).strftime("%d/%m/%Y %H:%M") if pix.criado_em else ""
            valor_texto = str(pix.valor or "").replace(".", ",")
            campos_cliente = " ".join(
                parte
                for parte in [
                    pix.cliente.nome if pix.cliente else "",
                    pix.cliente_sugerido.nome if pix.cliente_sugerido else "",
                    pix.cliente.cpf_cnpj if pix.cliente else "",
                    pix.cliente_sugerido.cpf_cnpj if pix.cliente_sugerido else "",
                ]
                if parte
            )
            if cliente_pix:
                campos_cliente_normalizados = normalizar_texto_cliente(campos_cliente)
                if not (
                    cliente_normalizado in campos_cliente_normalizados
                    or (cliente_documento and cliente_documento in normalizar_documento_cliente(campos_cliente))
                ):
                    continue
            campos_busca = " ".join(
                parte
                for parte in [
                    f"#{pix.id}",
                    str(pix.id),
                    pix.nome_pagador,
                    pix.cliente.nome if pix.cliente else "",
                    pix.cliente_sugerido.nome if pix.cliente_sugerido else "",
                    pix.instituicao_pix,
                    pix.get_status_display(),
                    pix.status,
                    data_pix,
                    data_cadastro,
                    valor_texto,
                    str(pix.valor or ""),
                ]
                if parte
            )
            campos_normalizados = normalizar_texto_cliente(campos_busca)
            if termo_busca_pix and not (
                termo_normalizado in campos_normalizados
                or (termo_documento and termo_documento in normalizar_documento_cliente(campos_busca))
                or (termo_valor and termo_valor in str(pix.valor or ""))
            ):
                continue
            resultado.append(pix)
        return resultado

    pix_filtro_contexto = {
        "pix_busca": termo_busca_pix,
        "pix_data_inicio": data_inicio_pix,
        "pix_data_fim": data_fim_pix,
        "pix_status": status_pix,
        "pix_cliente_filtro": cliente_pix,
        "pix_status_choices": PixRecebido.STATUS_CHOICES,
        "pix_tem_filtro": any([termo_busca_pix, data_inicio_pix, data_fim_pix, status_pix, cliente_pix]),
    }

    if request.method == "POST":
        form = PixRecebidoForm(request.POST, request.FILES)
        if form.is_valid():
            dados_pix = dict(form.cleaned_data)
            dados_pix["instituicao_pix"] = (request.POST.get("instituicao_pix") or "").strip()[:80]
            if not dados_pix.get("cliente"):
                form.add_error("cliente", "Confirme um cliente antes de salvar o Pix.")
                messages.warning(request, "Busque e confirme um cliente antes de salvar o Pix.")
                pix_recebidos = pix_recebidos_filtrados()
                return render(
                    request,
                    "estoque/central_pix.html",
                    {
                        "form": form,
                        "pix_recebidos": pix_recebidos,
                        "total_pix": len(pix_recebidos),
                        "voltar_url": retorno_url or reverse("estoque:contas_receber"),
                        "detalhe_retorno_url": central_pix_atual_url,
                        **pix_filtro_contexto,
                    },
                )
            pix_duplicado_baixado = _pix_duplicado_baixado(dados_pix)
            pix_duplicado_pendente = None if pix_duplicado_baixado else _pix_duplicado_pendente(dados_pix)
            pix_duplicado = pix_duplicado_baixado or pix_duplicado_pendente
            pix = form.save(commit=False)
            pix.instituicao_pix = dados_pix["instituicao_pix"]
            if pix_duplicado:
                pix.pix_original = pix_duplicado
                pix.status = PixRecebido.STATUS_POSSIVEL_DUPLICADO
            pix.save()
            if pix_duplicado:
                _mensagem_unica(
                    request,
                    messages.WARNING,
                    f"Pix salvo como possivel duplicado. Confira a comparacao com o Pix #{pix_duplicado.id}.",
                )
            elif pix.cliente_id:
                messages.success(request, "Pix recebido registrado com sucesso.")
            else:
                messages.success(request, "Pix salvo como pendente. Selecione o cliente antes de usar na baixa.")
            detalhe_url = reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id})
            return redirect(f"{detalhe_url}?{urlencode({'next': central_pix_url})}")
        else:
            messages.warning(request, "Confira os campos do Pix antes de salvar.")
    else:
        form = PixRecebidoForm()

    pix_recebidos = pix_recebidos_filtrados()
    return render(
        request,
        "estoque/central_pix.html",
        {
            "form": form,
            "pix_recebidos": pix_recebidos,
            "total_pix": len(pix_recebidos),
            "voltar_url": retorno_url or reverse("estoque:contas_receber"),
            "detalhe_retorno_url": central_pix_atual_url,
            **pix_filtro_contexto,
        },
    )


@ensure_csrf_cookie
def pix_enviar_inteligente(request):
    envio_padrao = _pix_envio_url_padrao(request)
    local_url = _mobile_url_configurada("PIX_LOCAL_URL", envio_padrao)
    online_url = _mobile_url_configurada("PIX_ONLINE_URL", envio_padrao)
    return render(
        request,
        "estoque/pix_enviar_inteligente.html",
        {
            "local_url": local_url,
            "online_url": online_url,
            "timeout_ms": 2500,
        },
    )


@ensure_csrf_cookie
def central_pix_enviar_comprovante(request):
    resumo = None
    ambiente_pix = _ambiente_envio_pix(request)

    if request.method == "POST":
        arquivo = request.FILES.get("comprovante")
        if not arquivo:
            messages.warning(request, "Escolha uma imagem ou arquivo de comprovante Pix.")
        else:
            enviado_por_nome = _nome_envio_pix_mobile(request.POST)
            try:
                pix_recebido = PixRecebido.objects.create(
                    enviado_por_nome=enviado_por_nome,
                    valor=Decimal("0.00"),
                    data_pagamento=timezone.now(),
                    observacao="Comprovante recebido pelo envio mobile. OCR pendente para processamento posterior.",
                    comprovante=arquivo,
                    status=PixRecebido.STATUS_NAO_IDENTIFICADO,
                    texto_ocr_bruto=PIX_OCR_PENDENTE_MOBILE,
                )
            except Exception as exc:
                logger.exception(
                    "Erro ao salvar comprovante Pix mobile. tipo=%s mensagem=%s diagnostico_storage=%s",
                    exc.__class__.__name__,
                    str(exc),
                    _diagnostico_storage_seguro(),
                )
                messages.error(
                    request,
                    "Erro ao salvar comprovante Pix. Verifique a configuração do armazenamento online.",
                )
                return render(
                    request,
                    "estoque/central_pix_enviar_comprovante.html",
                    {
                        "resumo": resumo,
                        "ambiente_pix": ambiente_pix,
                    },
                    status=200,
                )
            messages.success(request, "Comprovante recebido e salvo na Central de Pix.")
            messages.warning(
                request,
                "OCR nao executado no envio mobile para evitar timeout. Processe depois no detalhe do Pix.",
            )
            return redirect("estoque:central_pix_envio_sucesso", pix_id=pix_recebido.id)

    return render(
        request,
        "estoque/central_pix_enviar_comprovante.html",
        {
            "resumo": resumo,
            "ambiente_pix": ambiente_pix,
        },
    )


def central_pix_envio_sucesso(request, pix_id):
    pix = get_object_or_404(PixRecebido, pk=pix_id)
    ambiente_pix = _ambiente_envio_pix(request)
    return render(
        request,
        "estoque/central_pix_envio_sucesso.html",
        {
            "pix": pix,
            "ambiente_pix": ambiente_pix,
        },
    )


def _texto_indica_falha_ocr(texto):
    texto_limpo = " ".join(str(texto or "").strip().split())
    return (
        texto_limpo.startswith("ERRO OCR:")
        or texto_limpo.startswith("[OCR bloqueado")
        or texto_limpo.startswith("[Google Vision OCR erro]")
        or texto_limpo.startswith("[Google Vision indisponivel]")
        or texto_limpo == "OCR executado, mas nao retornou texto."
    )


def _ocr_tem_dados_aproveitaveis(valor, data_pagamento, instituicao_pix, pagador):
    return bool((valor and valor > Decimal("0.00")) or data_pagamento or instituicao_pix or pagador)


def _pix_tem_texto_ocr_util(pix):
    texto = " ".join(str(getattr(pix, "texto_ocr_bruto", "") or "").strip().split())
    if not texto:
        return False
    if getattr(pix, "valor", Decimal("0.00")) > Decimal("0.00") or pix.nome_pagador or pix.instituicao_pix:
        return True
    return texto not in {PIX_OCR_PENDENTE_MOBILE, PIX_OCR_MANUAL_ERRO_RENDER} and not _texto_indica_falha_ocr(texto)


def _registrar_observacao_correcao_pix(pix):
    momento = timezone.localtime(timezone.now()).strftime("%d/%m/%Y %H:%M")
    registro = f"Correcao manual dos dados do Pix em {momento}. Conferir antes de baixar contas."
    observacao_atual = (pix.observacao or "").strip()
    pix.observacao = f"{observacao_atual}\n{registro}".strip() if observacao_atual else registro


def _registrar_observacao_pix(pix, registro):
    observacao_atual = (pix.observacao or "").strip()
    pix.observacao = f"{observacao_atual}\n{registro}".strip() if observacao_atual else registro


def _marcar_pix_como_duplicado(pix, pix_referencia, motivo):
    if not pix or pix.status == PixRecebido.STATUS_BAIXADO:
        return False
    momento = timezone.localtime(timezone.now()).strftime("%d/%m/%Y %H:%M")
    pix.status = PixRecebido.STATUS_DUPLICADO
    pix.pix_original = pix_referencia
    referencia = f" Pix valido usado como referencia: #{pix_referencia.id}." if pix_referencia else ""
    _registrar_observacao_pix(pix, f"{momento} - Pix marcado como duplicado/inativo. {motivo}.{referencia}")
    pix.save(update_fields=["status", "pix_original", "observacao", "atualizado_em"])
    return True


def _marcar_pix_baixado_com_duplicados(pix_id, conta_ids=None, valor_baixa=None):
    if not pix_id:
        return None, 0
    pix = PixRecebido.objects.select_for_update().filter(pk=pix_id).first()
    if not pix:
        raise RecebimentoContaErro("Pix informado para baixa nao foi encontrado.")
    pix_original = None
    if pix.pix_original_id:
        pix_original = PixRecebido.objects.select_for_update().filter(pk=pix.pix_original_id).first()
    if pix.status in {PixRecebido.STATUS_DUPLICADO, PixRecebido.STATUS_IGNORADO}:
        raise RecebimentoContaErro("Este Pix esta inativo e nao pode ser usado para baixa.")
    if pix.status == PixRecebido.STATUS_BAIXADO:
        raise RecebimentoContaErro("Este Pix ja foi baixado e nao pode ser usado novamente.")
    if pix_original and pix_original.status == PixRecebido.STATUS_BAIXADO:
        raise RecebimentoContaErro(
            f"O Pix parecido #{pix.pix_original_id} ja foi baixado. Este Pix atual nao deve ser baixado novamente."
        )
    pix_ja_baixado = _pix_duplicado_baixado(
        {
            "nome_pagador": pix.nome_pagador,
            "valor": pix.valor,
            "data_pagamento": pix.data_pagamento,
            "instituicao_pix": pix.instituicao_pix,
            "texto_ocr_bruto": pix.texto_ocr_bruto,
        },
        excluir_pix_id=pix.id,
    )
    if pix_ja_baixado:
        raise RecebimentoContaErro(
            f"Ja existe Pix igual baixado no registro #{pix_ja_baixado.id}. Ignore este Pix sem baixa."
        )

    momento = timezone.localtime(timezone.now()).strftime("%d/%m/%Y %H:%M")
    pix.status = PixRecebido.STATUS_BAIXADO
    detalhes_baixa = []
    if conta_ids:
        detalhes_baixa.append(f"conta(s): {', '.join(str(conta_id) for conta_id in conta_ids)}")
    if valor_baixa is not None:
        detalhes_baixa.append(f"valor: {_formatar_moeda(valor_baixa)}")
    detalhes_texto = f" ({'; '.join(detalhes_baixa)})" if detalhes_baixa else ""
    _registrar_observacao_pix(pix, f"{momento} - Pix usado em baixa confirmada pelo operador{detalhes_texto}.")
    pix.save(update_fields=["status", "observacao", "atualizado_em"])

    duplicados_marcados = 0
    if pix_original:
        duplicados_marcados += int(
            _marcar_pix_como_duplicado(
                pix_original,
                pix,
                f"Marcado automaticamente porque o Pix #{pix.id} foi usado para baixa confirmada",
            )
        )

    duplicados_relacionados = PixRecebido.objects.select_for_update().filter(
        pix_original=pix,
        status__in=[
            PixRecebido.STATUS_PENDENTE,
            PixRecebido.STATUS_IDENTIFICADO,
            PixRecebido.STATUS_POSSIVEL_DUPLICADO,
            PixRecebido.STATUS_NAO_IDENTIFICADO,
        ],
    )
    for duplicado in duplicados_relacionados:
        duplicados_marcados += int(
            _marcar_pix_como_duplicado(
                duplicado,
                pix,
                f"Marcado automaticamente porque o Pix #{pix.id} foi usado para baixa confirmada",
            )
        )
    return pix, duplicados_marcados


def _url_com_pix_recebido(url, pix_id):
    if not url or not pix_id:
        return ""
    separador = "&" if "?" in url else "?"
    return f"{url}{separador}{urlencode({'pix_recebido': pix_id})}"


def _url_detalhe_pix(pix_id, next_url="", foco_cliente=False):
    url = reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix_id})
    parametros = {}
    if next_url:
        parametros["next"] = next_url
    if foco_cliente:
        parametros["foco_cliente"] = "1"
    if parametros:
        url = f"{url}?{urlencode(parametros)}"
    return url


def _url_detalhe_pix_preservando_retorno(pix_id, retorno_url, fallback_next="", foco_cliente=False):
    detalhe_base = reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix_id})
    if retorno_url == detalhe_base or retorno_url.startswith(f"{detalhe_base}?"):
        if foco_cliente and "foco_cliente=1" not in retorno_url:
            separador = "&" if "?" in retorno_url else "?"
            return f"{retorno_url}{separador}{urlencode({'foco_cliente': '1'})}"
        return retorno_url
    return _url_detalhe_pix(pix_id, fallback_next, foco_cliente=foco_cliente)


def _pix_pode_remover_cliente_confirmado(pix):
    return bool(
        pix
        and pix.status
        in {
            PixRecebido.STATUS_PENDENTE,
            PixRecebido.STATUS_IDENTIFICADO,
            PixRecebido.STATUS_POSSIVEL_DUPLICADO,
            PixRecebido.STATUS_NAO_IDENTIFICADO,
        }
    )


def _pix_tem_vinculo_financeiro(pix):
    return bool(pix and pix.status == PixRecebido.STATUS_BAIXADO)


def _titulo_detalhe_pix(pix):
    titulos = {
        PixRecebido.STATUS_BAIXADO: "Detalhe do Pix baixado",
        PixRecebido.STATUS_IGNORADO: "Detalhe do Pix ignorado",
        PixRecebido.STATUS_POSSIVEL_DUPLICADO: "Detalhe do Pix possivel duplicado",
        PixRecebido.STATUS_DUPLICADO: "Detalhe do Pix duplicado",
        PixRecebido.STATUS_NAO_IDENTIFICADO: "Detalhe do Pix nao identificado",
        PixRecebido.STATUS_PENDENTE: "Detalhe do Pix pendente",
    }
    return titulos.get(pix.status, "Detalhe do Pix")


def _caminho_comprovante_pix_media_antiga(nome_arquivo):
    nome_arquivo = str(nome_arquivo or "").strip()
    if not nome_arquivo:
        return None
    media_root = Path(settings.MEDIA_ROOT).resolve()
    caminho = (media_root / nome_arquivo).resolve()
    try:
        caminho.relative_to(media_root)
    except ValueError:
        return None
    return caminho if caminho.is_file() else None


def _abrir_comprovante_pix(pix):
    try:
        return pix.comprovante.open("rb")
    except (FileNotFoundError, OSError, ValueError):
        caminho_antigo = _caminho_comprovante_pix_media_antiga(pix.comprovante.name)
        if caminho_antigo:
            return caminho_antigo.open("rb")
        raise


@ensure_csrf_cookie
def central_pix_detalhe(request, pix_id):
    pix = get_object_or_404(PixRecebido.objects.select_related("cliente", "cliente_sugerido", "pix_original"), pk=pix_id)
    if request.method == "GET" and not pix.visualizado_em:
        pix.visualizado_em = timezone.now()
        pix.save(update_fields=["visualizado_em"])
    voltar_url = _url_retorno_segura(request) or reverse("estoque:central_pix")
    modo_conferencia_ocr = request.GET.get("modo") == "ocr"
    detalhe_url = reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id})
    if voltar_url:
        detalhe_url = f"{detalhe_url}?{urlencode({'next': voltar_url})}"

    if request.method == "POST" and request.POST.get("acao") == "ignorar":
        if pix.status == PixRecebido.STATUS_BAIXADO:
            messages.warning(request, "Pix baixado/usado financeiramente nao pode ser ignorado.")
            return redirect(detalhe_url)
        momento = timezone.localtime(timezone.now()).strftime("%d/%m/%Y %H:%M")
        observacao_atual = (pix.observacao or "").strip()
        registro = f"{momento} - Pix ignorado sem baixa pelo operador."
        observacao = f"{observacao_atual}\n{registro}".strip() if observacao_atual else registro
        PixRecebido.objects.filter(pk=pix.pk).update(
            status=PixRecebido.STATUS_IGNORADO,
            observacao=observacao,
            atualizado_em=timezone.now(),
        )
        messages.success(request, "Pix marcado como ignorado.")
        return redirect("estoque:central_pix")

    if request.method == "POST" and request.POST.get("acao") == "marcar_duplicado":
        _marcar_pix_como_duplicado(
            pix,
            pix.pix_original,
            "Marcado manualmente pelo operador apos conferencia de Pix parecido",
        )
        messages.success(request, "Pix marcado como duplicado/inativo. Nenhuma baixa financeira foi feita.")
        return redirect(detalhe_url)

    form_correcao = PixRecebidoCorrecaoForm(pix=pix)
    acoes_pix = request.POST.getlist("acao")
    acao_pix = acoes_pix[-1] if acoes_pix else ""
    if request.method == "POST" and acao_pix in {"corrigir", "usar_baixa"}:
        form_correcao = PixRecebidoCorrecaoForm(request.POST, pix=pix)
        if form_correcao.is_valid():
            nome_pagador = form_correcao.cleaned_data["nome_pagador"]
            cliente_sugerido, _confianca_cliente, _mensagem_cliente = _sugerir_cliente_por_pagador(nome_pagador)
            cliente_confirmado = form_correcao.cleaned_data["cliente"]
            valor_corrigido = form_correcao.cleaned_data["valor"]
            data_corrigida = form_correcao.cleaned_data["data_pagamento"]
            if acao_pix == "usar_baixa":
                if not cliente_confirmado:
                    messages.warning(request, "Confirme o cliente antes de usar este Pix na baixa.")
                    return redirect(detalhe_url)
                if not valor_corrigido or valor_corrigido <= Decimal("0.00"):
                    messages.warning(request, "Informe o valor do Pix antes de usar na baixa.")
                    return redirect(detalhe_url)
                if not data_corrigida:
                    messages.warning(request, "Informe a data do pagamento do Pix antes de usar na baixa.")
                    return redirect(detalhe_url)
            pix.cliente = form_correcao.cleaned_data["cliente"]
            pix.cliente_sugerido = cliente_sugerido
            pix.nome_pagador = nome_pagador
            pix.valor = valor_corrigido
            if data_corrigida:
                pix.data_pagamento = data_corrigida
            pix.instituicao_pix = form_correcao.cleaned_data["instituicao_pix"]
            pix_duplicado_baixado = _pix_duplicado_baixado(
                {
                    "nome_pagador": pix.nome_pagador,
                    "valor": pix.valor,
                    "data_pagamento": pix.data_pagamento,
                    "instituicao_pix": pix.instituicao_pix,
                    "texto_ocr_bruto": pix.texto_ocr_bruto,
                },
                excluir_pix_id=pix.id,
            )
            if pix_duplicado_baixado:
                pix.pix_original = pix_duplicado_baixado
                pix.status = PixRecebido.STATUS_POSSIVEL_DUPLICADO
            _registrar_observacao_correcao_pix(pix)
            pix.save(update_fields=[
                "cliente",
                "cliente_sugerido",
                "pix_original",
                "nome_pagador",
                "valor",
                "data_pagamento",
                "instituicao_pix",
                "status",
                "observacao",
                "atualizado_em",
            ])
            if acao_pix == "usar_baixa":
                baixa_url = reverse("estoque:receber_cliente", kwargs={"cliente_id": pix.cliente_id})
                baixa_url = f"{baixa_url}?{urlencode({'pix_recebido': pix.id, 'next': detalhe_url})}"
                messages.success(request, "Dados do Pix salvos. Confira e confirme a baixa do cliente; nenhuma baixa foi feita ainda.")
                return redirect(baixa_url)
            messages.success(request, "Dados do Pix corrigidos. Nenhuma baixa financeira foi feita.")
            return redirect(detalhe_url)
        messages.warning(request, "Confira os dados da correcao antes de salvar.")

    pix_duplicado_baixado = (
        pix.pix_original
        if pix.pix_original and pix.pix_original.status == PixRecebido.STATUS_BAIXADO
        else _pix_duplicado_baixado(
            {
                "nome_pagador": pix.nome_pagador,
                "valor": pix.valor,
                "data_pagamento": pix.data_pagamento,
                "instituicao_pix": pix.instituicao_pix,
                "texto_ocr_bruto": pix.texto_ocr_bruto,
            },
            excluir_pix_id=pix.id,
        )
    )
    pix_retorno_baixa_url = _url_com_pix_recebido(voltar_url, pix.id) if "/receber/" in voltar_url else ""
    pix_tem_dados_lidos_sem_cliente = bool(
        not pix.cliente_id
        and pix.nome_pagador
        and pix.instituicao_pix
        and pix.valor
        and pix.valor > Decimal("0.00")
        and pix.data_pagamento
    )
    return render(
        request,
        "estoque/central_pix_detalhe.html",
        {
            "pix": pix,
            "voltar_url": voltar_url,
            "form_correcao": form_correcao,
            "pix_retorno_baixa_url": pix_retorno_baixa_url,
            "pix_parecido_baixado": bool(pix_duplicado_baixado),
            "pix_parecido_pendente": bool(
                pix.pix_original
                and pix.pix_original.status
                in {
                    PixRecebido.STATUS_PENDENTE,
                    PixRecebido.STATUS_IDENTIFICADO,
                    PixRecebido.STATUS_POSSIVEL_DUPLICADO,
                    PixRecebido.STATUS_NAO_IDENTIFICADO,
                }
            ),
            "pix_tem_texto_ocr_util": _pix_tem_texto_ocr_util(pix),
            "pix_google_vision_habilitado": pix_google_vision_habilitado(),
            "modo_conferencia_ocr": modo_conferencia_ocr,
            "clientes_pix_autocomplete": _clientes_pix_autocomplete_local(),
            "foco_cliente_confirmado": request.GET.get("foco_cliente") == "1" or not pix.cliente_id,
            "pix_tem_dados_lidos_sem_cliente": pix_tem_dados_lidos_sem_cliente,
            "pix_pode_excluir": not _pix_tem_vinculo_financeiro(pix),
            "pix_titulo_detalhe": _titulo_detalhe_pix(pix),
        },
    )


def central_pix_excluir(request, pix_id):
    pix = get_object_or_404(PixRecebido.objects.select_related("cliente"), pk=pix_id)
    detalhe_url = reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id})

    if _pix_tem_vinculo_financeiro(pix):
        messages.warning(request, "Nao e possivel excluir este Pix porque ele ja tem vinculo financeiro/baixa.")
        return redirect(detalhe_url)

    if request.method == "POST":
        confirmacao = (request.POST.get("confirmacao") or "").strip()
        if confirmacao != "EXCLUIR":
            messages.warning(request, "Digite exatamente EXCLUIR para confirmar a exclusao do Pix.")
            return redirect("estoque:central_pix_excluir", pix_id=pix.id)

        pix_id_excluido = pix.id
        nome_comprovante = pix.comprovante.name if pix.comprovante else ""
        if pix.comprovante:
            try:
                pix.comprovante.delete(save=False)
            except Exception:
                # Storage remoto pode falhar; a prioridade segura aqui e remover o registro enviado errado.
                pass
        pix.delete()
        mensagem = f"Pix #{pix_id_excluido} excluido com sucesso."
        if nome_comprovante:
            mensagem += " Comprovante associado removido quando o storage permitiu."
        messages.success(request, mensagem)
        return redirect("estoque:central_pix")

    return render(
        request,
        "estoque/central_pix_excluir.html",
        {
            "pix": pix,
            "detalhe_url": detalhe_url,
        },
    )


@require_POST
def central_pix_remover_cliente_confirmado(request, pix_id):
    pix = get_object_or_404(PixRecebido, pk=pix_id)
    detalhe_url = _url_detalhe_pix(pix.id)

    if not _pix_pode_remover_cliente_confirmado(pix):
        messages.warning(
            request,
            "Este Pix ja foi baixado/inativado. Para alterar cliente ou valor, use o fluxo proprio de estorno/cancelamento.",
        )
        return redirect(detalhe_url)

    momento = timezone.localtime(timezone.now()).strftime("%d/%m/%Y %H:%M")
    pix.cliente = None
    pix.cliente_sugerido = None
    if pix.status != PixRecebido.STATUS_POSSIVEL_DUPLICADO:
        pix.status = PixRecebido.STATUS_PENDENTE
    _registrar_observacao_pix(
        pix,
        f"{momento} - Cliente confirmado removido pelo operador antes de baixa/credito. Pix mantido para revisao.",
    )
    pix.save(update_fields=["cliente", "cliente_sugerido", "status", "observacao", "atualizado_em"])
    messages.success(
        request,
        "Cliente confirmado removido. O Pix continua salvo para revisao; nenhuma baixa ou credito foi gerado.",
    )
    return redirect(detalhe_url)


@require_POST
def central_pix_processar_ocr(request, pix_id):
    pix = get_object_or_404(PixRecebido.objects.select_related("cliente", "cliente_sugerido", "pix_original"), pk=pix_id)
    detalhe_base_url = reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id})
    detalhe_url = f"{detalhe_base_url}?{urlencode({'modo': 'ocr', 'next': detalhe_base_url})}"
    pix_original_baixado = pix.pix_original if pix.pix_original and pix.pix_original.status == PixRecebido.STATUS_BAIXADO else None

    if not pix.comprovante:
        pix.texto_ocr_bruto = "ERRO OCR: comprovante nao encontrado para processamento manual."
        pix.save(update_fields=["texto_ocr_bruto", "atualizado_em"])
        messages.warning(request, "Comprovante Pix nao encontrado para processar OCR.")
        return redirect(detalhe_url)

    arquivo = None
    inicio_ocr = time.monotonic()
    nome_arquivo = _nome_arquivo_seguro(pix.comprovante)
    try:
        arquivo = _abrir_comprovante_pix(pix)
        nome_arquivo = _nome_arquivo_seguro(arquivo) or nome_arquivo
        logger.info("[PIX OCR] Google Vision habilitado=%s pix_id=%s", pix_google_vision_habilitado(), pix.id)
        dados = _analisar_comprovante_pix_principal(arquivo, debug_prefix=f"pix_{pix.id}")
    except Exception as exc:
        tempo_ocr = time.monotonic() - inicio_ocr
        erro_resumido = str(exc).strip()[:180]
        resumo_erro = f"{exc.__class__.__name__}{f': {erro_resumido}' if erro_resumido else ''}"
        _salvar_falha_ocr_manual(pix, resumo_erro)
        logger.exception(
            "Falha inesperada no OCR manual Pix. pix_id=%s arquivo=%s tempo=%.1fs erro=%s",
            pix.id,
            nome_arquivo,
            tempo_ocr,
            resumo_erro,
        )
        messages.warning(request, "OCR nao conseguiu ler todos os dados. Confira manualmente.")
        return redirect(detalhe_url)
    finally:
        if arquivo:
            try:
                arquivo.close()
            except Exception:
                pass

    cliente_sugerido, confianca_cliente, mensagem_cliente = _sugerir_cliente_por_pagador(dados.get("pagador"))
    cliente_confirmado_automatico = pix.cliente
    if not cliente_confirmado_automatico and confianca_cliente != "ambigua":
        cliente_confirmado_automatico = _cliente_exato_por_pagador_pix(dados.get("pagador"))
    valor = _decimal_pix_lido(dados.get("valor"))
    data_pagamento_texto = dados.get("data_pagamento")
    data_pagamento_lida = _data_pix_lida(data_pagamento_texto)
    data_pagamento = data_pagamento_lida or pix.data_pagamento
    instituicao_pix = (dados.get("instituicao_pix") or "")[:80]
    nome_pagador = (dados.get("pagador") or "")[:160]
    texto_ocr_bruto = dados.get("texto_ocr_bruto") or dados.get("texto_extraido") or ""
    dados_aproveitaveis = _ocr_tem_dados_aproveitaveis(
        valor,
        data_pagamento_lida,
        instituicao_pix,
        nome_pagador,
    )
    if _texto_indica_falha_ocr(texto_ocr_bruto) and not dados_aproveitaveis:
        detalhe_erro = str(texto_ocr_bruto or "").strip()
        tempo_ocr = time.monotonic() - inicio_ocr
        if detalhe_erro.startswith("[OCR bloqueado") or detalhe_erro.startswith("[Google Vision OCR erro]"):
            pix.texto_ocr_bruto = detalhe_erro
            pix.save(update_fields=["texto_ocr_bruto", "atualizado_em"])
        else:
            _salvar_falha_ocr_manual(pix, "Falha retornada pelo OCR")
        logger.warning(
            "OCR manual Pix retornou falha. pix_id=%s arquivo=%s tempo=%.1fs extraiu_valor=%s extraiu_data=%s erro=%s",
            pix.id,
            nome_arquivo,
            tempo_ocr,
            bool(valor and valor > Decimal("0.00")),
            bool(data_pagamento_lida),
            detalhe_erro[:180],
        )
        messages.warning(request, "OCR nao conseguiu ler todos os dados. Confira manualmente.")
        return redirect(detalhe_url)

    pix_duplicado = _pix_duplicado_baixado(
        {
            "pagador": dados.get("pagador"),
            "valor": valor,
            "data_pagamento": data_pagamento_lida,
            "instituicao_pix": dados.get("instituicao_pix"),
        },
        excluir_pix_id=pix.id,
        texto_ocr_bruto=texto_ocr_bruto,
    ) or pix_original_baixado or _detectar_pix_duplicado_comprovante(dados, valor, data_pagamento_lida, texto_ocr_bruto)

    pix.cliente = cliente_confirmado_automatico
    pix.cliente_sugerido = cliente_sugerido or cliente_confirmado_automatico
    pix.pix_original = pix_duplicado
    pix.nome_pagador = nome_pagador
    pix.valor = valor
    pix.data_pagamento = data_pagamento
    pix.instituicao_pix = instituicao_pix
    pix.texto_ocr_bruto = texto_ocr_bruto
    pix.status = (
        PixRecebido.STATUS_POSSIVEL_DUPLICADO
        if pix_duplicado
        else (
            PixRecebido.STATUS_PENDENTE
            if pix.cliente or pix.cliente_sugerido
            else PixRecebido.STATUS_NAO_IDENTIFICADO
        )
    )
    pix.observacao = "OCR processado manualmente pelo detalhe do Pix. Conferir antes de baixar contas."
    if not dados.get("ok"):
        pix.observacao = f"{pix.observacao} Leitura automatica incompleta."
    if mensagem_cliente:
        pix.observacao = f"{pix.observacao} {mensagem_cliente}"
    if pix_duplicado:
        pix.observacao = f"{pix.observacao} Possivel Pix duplicado do registro #{pix_duplicado.id}."
    pix.save(update_fields=[
        "cliente_sugerido",
        "cliente",
        "pix_original",
        "nome_pagador",
        "valor",
        "data_pagamento",
        "instituicao_pix",
        "texto_ocr_bruto",
        "status",
        "observacao",
        "atualizado_em",
    ])
    ocr_parcial = dados_aproveitaveis and not (nome_pagador and (pix.cliente or pix.cliente_sugerido))
    if ocr_parcial:
        messages.warning(request, "OCR parcial concluido. Confira os dados antes de qualquer baixa.")
    elif dados.get("ok"):
        messages.success(request, "OCR processado. Confira os dados antes de qualquer baixa.")
    else:
        messages.warning(request, "OCR processado, mas nao identificou todos os dados. Confira manualmente.")
    logger.info(
        "OCR manual Pix concluido. pix_id=%s arquivo=%s tempo=%.1fs ok=%s extraiu_valor=%s extraiu_data=%s",
        pix.id,
        nome_arquivo,
        time.monotonic() - inicio_ocr,
        bool(dados.get("ok")),
        bool(valor and valor > Decimal("0.00")),
        bool(data_pagamento_lida),
    )
    return redirect(detalhe_url)


def central_pix_comprovante(request, pix_id):
    pix = get_object_or_404(PixRecebido, pk=pix_id)
    if not pix.comprovante:
        raise Http404("Comprovante Pix nao encontrado.")

    try:
        arquivo = _abrir_comprovante_pix(pix)
    except (FileNotFoundError, OSError, ValueError):
        raise Http404("Comprovante Pix nao encontrado.")

    content_type = mimetypes.guess_type(pix.comprovante.name)[0] or "application/octet-stream"
    response = FileResponse(arquivo, content_type=content_type)
    nome_arquivo = Path(pix.comprovante.name).name
    response["Content-Disposition"] = f'inline; filename="{nome_arquivo}"'
    return response


@require_POST
def central_pix_analisar_comprovante(request):
    arquivo = request.FILES.get("comprovante")
    if not arquivo:
        return JsonResponse({
            "ok": False,
            "mensagem": "Envie um comprovante para leitura automatica.",
            "nome_arquivo": "",
        }, status=400)

    try:
        dados = _analisar_comprovante_pix_principal(arquivo, debug_prefix="pix_upload")
    except Exception as exc:
        logger.exception(
            "Falha inesperada ao analisar comprovante Pix enviado. arquivo=%s erro=%s",
            _nome_arquivo_seguro(arquivo),
            exc.__class__.__name__,
        )
        return JsonResponse({
            "ok": False,
            "pagador": "",
            "valor": "",
            "data_pagamento": "",
            "instituicao_pix": "",
            "cliente_sugerido_id": None,
            "cliente_sugerido_nome": "",
            "confianca_cliente": 0,
            "mensagem_cliente": "",
            "debug_data_pagamento": "Data enviada ao frontend: nao reconhecida",
            "debug_texto_ocr": f"[OCR erro]\n{exc.__class__.__name__}",
            "texto_ocr_bruto": f"[OCR erro]\n{exc.__class__.__name__}",
            "nome_arquivo": arquivo.name,
            "mensagem": "OCR nao conseguiu ler todos os dados. Confira manualmente.",
            "observacao": "",
        })
    cliente_sugerido, confianca_cliente, mensagem_cliente = _sugerir_cliente_por_pagador(dados.get("pagador"))
    debug_texto_ocr = dados.get("texto_ocr_bruto", "")


    return JsonResponse({
        "ok": bool(dados.get("ok")),
        "pagador": dados.get("pagador", ""),
        "valor": dados.get("valor", ""),
        "data_pagamento": dados.get("data_pagamento", ""),
        "instituicao_pix": dados.get("instituicao_pix", ""),
        "cliente_sugerido_id": cliente_sugerido.id if cliente_sugerido else None,
        "cliente_sugerido_nome": cliente_sugerido.nome if cliente_sugerido else "",
        "confianca_cliente": confianca_cliente,
        "mensagem_cliente": mensagem_cliente,
        "debug_data_pagamento": dados.get("debug_data_pagamento", ""),
        "debug_texto_ocr": debug_texto_ocr,
        "texto_ocr_bruto": debug_texto_ocr,
        "nome_arquivo": arquivo.name,
        "mensagem": dados.get("mensagem", ""),
        "observacao": (
            "Dados lidos automaticamente do comprovante. Conferir antes de confirmar."
            if dados.get("ok")
            else ""
        ),
    })



@ensure_csrf_cookie
def receber_cliente_escolher(request):
    cliente_id = request.GET.get("cliente_id", "").strip()
    retorno_url = _url_retorno_segura(request) or reverse("estoque:home")
    retorno_recebimento_url = reverse("estoque:receber_cliente_escolher")

    if cliente_id.isdigit():
        cliente = Cliente.objects.filter(pk=cliente_id, ativo=True).first()
        if cliente:
            url = reverse("estoque:receber_cliente", kwargs={"cliente_id": cliente.id})
            return redirect(f"{url}?{urlencode({'next': retorno_recebimento_url})}")
        messages.warning(request, "Cliente nao encontrado ou inativo.")

    hoje = timezone.localdate()
    contas_abertas_resumo = ContaReceber.objects.filter(
        status__in=[ContaReceber.STATUS_ABERTA, ContaReceber.STATUS_PARCIAL],
        valor_em_aberto__gt=0,
    )
    resumo_receber = {
        "clientes_devendo": contas_abertas_resumo.values("cliente_id").distinct().count(),
        "contas_abertas": contas_abertas_resumo.count(),
        "total_a_receber": contas_abertas_resumo.aggregate(total=Sum("valor_em_aberto")).get("total") or Decimal("0.00"),
        "total_vencido": contas_abertas_resumo.filter(
            data_vencimento__lt=hoje
        ).aggregate(total=Sum("valor_em_aberto")).get("total") or Decimal("0.00"),
    }

    formas_pagamento = (
        "Dinheiro",
        "PIX",
        "Cartao de debito",
        "Cartao de credito",
        "Transferencia",
        "Outro",
    )
    valores = {
        "data_recebimento": hoje.isoformat(),
        "valor": "",
        "forma_pagamento": "Dinheiro",
        "destino_diferenca": "troco",
    }

    return render(
        request,
        "estoque/receber_cliente.html",
        {
            "cliente": None,
            "resumo_receber": resumo_receber,
            "contas": [],
            "contas_preview": [],
            "total_contas": 0,
            "total_em_aberto": Decimal("0.00"),
            "credito_disponivel": Decimal("0.00"),
            "creditos_disponiveis": [],
            "saldo_resultante_credito": Decimal("0.00"),
            "formas_pagamento": formas_pagamento,
            "valores": valores,
            "pagamentos_hoje_preview": [],
            "pagamentos_recentes": [],
            "feedback_recebimento": None,
            "contas_atualizadas_ids": [],
            "contas_atualizadas_feedback": {},
            "hoje_iso": hoje.isoformat(),
            "retorno_url": retorno_url,
            "tem_pix_em_atencao": _tem_pix_em_atencao(),
            "pix_recebido_escolhido": None,
            "pix_detalhe_url": "",
            "pix_trocar_cliente_url": "",
            "pix_remover_cliente_url": "",
        },
    )


@ensure_csrf_cookie
def receber_cliente(request, cliente_id):
    cliente = get_object_or_404(
        Cliente.objects.only(
            "id",
            "nome",
            "whatsapp",
            "whatsapp_normalizado",
            "prazo_padrao_dias",
        ),
        pk=cliente_id,
    )
    feedback_recebimento = request.session.pop("receber_cliente_feedback", None)
    retorno_url = _url_retorno_segura(request)
    contas_url = reverse("estoque:contas_receber")
    destino_retorno = retorno_url or f"{contas_url}?{urlencode({'cliente': cliente.nome, 'status': 'em_aberto'})}"
    destino_pos_recebimento = reverse("estoque:receber_cliente", kwargs={"cliente_id": cliente.id})
    if retorno_url:
        destino_pos_recebimento = f"{destino_pos_recebimento}?{urlencode({'next': retorno_url})}"
    hoje = timezone.localdate()
    formas_pagamento = (
        "Dinheiro",
        "PIX",
        "Cartao de debito",
        "Cartao de credito",
        "Transferencia",
        "Outro",
    )

    contas = list(_contas_receber_abertas_cliente_qs(cliente.id, hoje))

    total_em_aberto = sum((conta.valor_em_aberto or Decimal("0.00") for conta in contas), Decimal("0.00"))
    valores = {
        "data_recebimento": hoje.isoformat(),
        "valor": f"{total_em_aberto:.2f}".replace(".", ","),
        "forma_pagamento": "Dinheiro",
        "destino_diferenca": "troco",
    }
    pix_recebido_id = (
        request.POST.get("pix_recebido")
        or request.GET.get("pix_recebido", "")
    ).strip()
    pix_recebido_escolhido = None
    pix_detalhe_url = ""
    pix_trocar_cliente_url = ""
    pix_remover_cliente_url = ""
    if pix_recebido_id.isdigit():
        pix_recebido_escolhido = PixRecebido.objects.select_related("pix_original").filter(pk=pix_recebido_id).first()
    if pix_recebido_escolhido:
        pix_detalhe_url = _url_detalhe_pix_preservando_retorno(
            pix_recebido_escolhido.id,
            retorno_url,
            request.get_full_path(),
        )
        pix_trocar_cliente_url = _url_detalhe_pix_preservando_retorno(
            pix_recebido_escolhido.id,
            retorno_url,
            request.get_full_path(),
            foco_cliente=True,
        )
        pix_remover_cliente_url = reverse(
            "estoque:central_pix_remover_cliente_confirmado",
            kwargs={"pix_id": pix_recebido_escolhido.id},
        )
    if pix_recebido_escolhido and request.method != "POST":
        valores["valor"] = str((pix_recebido_escolhido.valor or Decimal("0.00")).quantize(Decimal("0.01"))).replace(".", ",")
        valores["forma_pagamento"] = "PIX"
        if pix_recebido_escolhido.data_pagamento:
            valores["data_recebimento"] = timezone.localtime(pix_recebido_escolhido.data_pagamento).date().isoformat()
        if not contas:
            messages.warning(
                request,
                "Este cliente nao tem contas abertas. Voce pode trocar o cliente, remover o vinculo ou manter este Pix pendente para revisao.",
            )
    if feedback_recebimento and total_em_aberto <= Decimal("0.00"):
        valores["valor"] = ""
    creditos_rows = list(
        CreditoCliente.objects.filter(cliente_id=cliente.id)
        .exclude(valor=Decimal("0.00"))
        .values(
            "id",
            "criado_em",
            "valor",
            "observacao",
            "origem_conta_receber_id",
            "origem_conta_receber__venda_id",
        )
        .order_by("-criado_em", "-id")
    )
    credito_disponivel = (
        sum((credito["valor"] or Decimal("0.00") for credito in creditos_rows), Decimal("0.00"))
    ).quantize(Decimal("0.01"))
    credito_disponivel = max(credito_disponivel, Decimal("0.00"))
    saldo_resultante_credito = max(
        (total_em_aberto - credito_disponivel).quantize(Decimal("0.01")),
        Decimal("0.00"),
    )
    creditos_disponiveis = []
    credito_restante_para_exibir = credito_disponivel
    for credito in creditos_rows:
        if credito_restante_para_exibir <= Decimal("0.00"):
            break
        valor_credito_original = (credito["valor"] or Decimal("0.00")).quantize(Decimal("0.01"))
        if valor_credito_original <= Decimal("0.00"):
            continue
        valor_credito = min(valor_credito_original, credito_restante_para_exibir).quantize(Decimal("0.01"))
        credito_restante_para_exibir = (credito_restante_para_exibir - valor_credito).quantize(Decimal("0.01"))
        creditos_disponiveis.append({
            "id": credito["id"],
            "criado_em": credito["criado_em"],
            "valor": valor_credito,
            "conta_id": credito["origem_conta_receber_id"],
            "venda_id": credito["origem_conta_receber__venda_id"],
            "motivo": credito["observacao"] or "Credito gerado para o cliente.",
            "saldo_resultante": max(
                (total_em_aberto - valor_credito).quantize(Decimal("0.01")),
                Decimal("0.00"),
            ),
        })
    pagamentos_hoje_preview = [
        float(valor or Decimal("0.00"))
        for valor in RecebimentoContaReceber.objects.filter(
            conta__cliente_id=cliente.id,
            criado_em__date=hoje,
        ).values_list("valor", flat=True)
    ]
    limite_recente = timezone.now() - timedelta(hours=72)
    recebimentos_recentes = (
        RecebimentoContaReceber.objects.filter(conta__cliente_id=cliente.id, criado_em__gte=limite_recente)
        .values(
            "id",
            "conta_id",
            "data_recebimento",
            "valor",
            "forma_pagamento",
            "observacao",
            "criado_em",
            "conta__venda_id",
        )
        .order_by("-criado_em", "-id")[:8]
    )
    pagamentos_recentes = []
    for recebimento in recebimentos_recentes:
        valor_total_recebido = _valor_total_recebimento_cliente(recebimento)
        pagamentos_recentes.append(
            {
                "criado_em": recebimento["criado_em"],
                "criado_em_data": timezone.localtime(recebimento["criado_em"]).date().isoformat(),
                "data_recebimento": recebimento["data_recebimento"].isoformat() if recebimento["data_recebimento"] else "",
                "valor": valor_total_recebido,
                "valor_numero": float(valor_total_recebido or Decimal("0.00")),
                "valor_aplicado": recebimento["valor"],
                "forma_pagamento": recebimento["forma_pagamento"],
                "conta_id": recebimento["conta_id"],
                "venda_id": recebimento["conta__venda_id"] if recebimento["conta_id"] else "",
                "observacao": recebimento["observacao"],
            }
        )

    if request.method == "POST":
        valores = {
            "data_recebimento": request.POST.get("data_recebimento", "").strip(),
            "valor": request.POST.get("valor", "").strip(),
            "forma_pagamento": request.POST.get("forma_pagamento", "").strip(),
            "destino_diferenca": request.POST.get("destino_diferenca", "troco").strip(),
        }
        data_recebimento = parse_date(valores["data_recebimento"])
        if not data_recebimento:
            messages.warning(request, "Informe uma data de recebimento valida.")
        elif valores["forma_pagamento"] not in formas_pagamento:
            messages.warning(request, "Selecione uma forma de pagamento.")
        elif valores["destino_diferenca"] not in {"troco", "credito"}:
            messages.warning(request, "Selecione como tratar a sobra do pagamento.")
        else:
            try:
                valor_recebido = _decimal_do_front(valores["valor"] or "0", "0.01")
            except ValueError as exc:
                messages.warning(request, str(exc))
            else:
                if valor_recebido <= Decimal("0.00"):
                    messages.warning(request, "Informe um valor recebido maior que zero.")
                elif not contas:
                    messages.warning(request, "Nao ha contas abertas para receber deste cliente.")
                else:
                    try:
                        with transaction.atomic():
                            contas_atualizadas = list(
                                _contas_receber_abertas_cliente_qs(cliente.id, hoje, bloquear=True)
                            )
                            if not contas_atualizadas:
                                raise RecebimentoContaErro("Nao ha contas abertas para receber deste cliente.")

                            restante = valor_recebido
                            distribuicao = []
                            for conta_atual in contas_atualizadas:
                                if restante <= Decimal("0.00"):
                                    break
                                saldo_conta = (conta_atual.valor_em_aberto or Decimal("0.00")).quantize(Decimal("0.01"))
                                valor_aplicar = min(restante, saldo_conta).quantize(Decimal("0.01"))
                                if valor_aplicar <= Decimal("0.00"):
                                    continue
                                distribuicao.append([conta_atual, valor_aplicar, Decimal("0.00")])
                                restante = (restante - valor_aplicar).quantize(Decimal("0.01"))

                            if not distribuicao:
                                raise RecebimentoContaErro("Informe um valor recebido maior que zero.")

                            sobra = max(restante, Decimal("0.00")).quantize(Decimal("0.01"))
                            if sobra > Decimal("0.00"):
                                distribuicao[-1][2] = sobra

                            valor_aplicado_total = Decimal("0.00")
                            credito_gerado_total = Decimal("0.00")
                            contas_afetadas = 0
                            contas_atualizadas_ids = []
                            contas_atualizadas_feedback = {}
                            contas_confirmacao_whatsapp = []
                            for conta_atual, valor_aplicar, sobra_conta in distribuicao:
                                valor_entregue_conta = (valor_aplicar + sobra_conta).quantize(Decimal("0.01"))
                                observacao = (
                                    "Recebimento geral do cliente. "
                                    f"Total recebido: {_formatar_moeda(valor_recebido)}. "
                                    f"Aplicado nesta conta: {_formatar_moeda(valor_aplicar)}."
                                )
                                resultado_recebimento = _aplicar_recebimento_conta(
                                    conta_atual,
                                    data_recebimento,
                                    valor_entregue_conta,
                                    valores["forma_pagamento"],
                                    observacao,
                                    valores["destino_diferenca"],
                                )
                                valor_aplicado_total = (valor_aplicado_total + valor_aplicar).quantize(Decimal("0.01"))
                                credito_gerado_total = (
                                    credito_gerado_total + resultado_recebimento["credito_gerado"]
                                ).quantize(Decimal("0.01"))
                                contas_afetadas += 1
                                contas_atualizadas_ids.append(conta_atual.id)
                                contas_atualizadas_feedback[str(conta_atual.id)] = {
                                    "valor_aplicado": _formatar_moeda(valor_aplicar),
                                    "saldo_restante": _formatar_moeda(resultado_recebimento["saldo_restante"]),
                                    "quitada": resultado_recebimento["saldo_restante"] <= Decimal("0.00"),
                                }
                                contas_confirmacao_whatsapp.append({
                                    "conta_id": conta_atual.id,
                                    "venda_id": conta_atual.venda_id,
                                    "data_nota": conta_atual.data_emissao.strftime("%d/%m/%Y") if conta_atual.data_emissao else "",
                                    "saldo_antes": (valor_aplicar + resultado_recebimento["saldo_restante"]).quantize(Decimal("0.01")),
                                    "nota_inteira_antes": abs(
                                        (
                                            (valor_aplicar + resultado_recebimento["saldo_restante"])
                                            - (conta_atual.valor_original or Decimal("0.00"))
                                        ).quantize(Decimal("0.01"))
                                    ) <= Decimal("0.01"),
                                    "valor_aplicado": valor_aplicar,
                                    "saldo_restante": resultado_recebimento["saldo_restante"],
                                    "quitada": resultado_recebimento["saldo_restante"] <= Decimal("0.00"),
                                })
                            pix_baixado = None
                            duplicados_marcados = 0
                            if pix_recebido_id:
                                pix_baixado, duplicados_marcados = _marcar_pix_baixado_com_duplicados(
                                    pix_recebido_id,
                                    contas_atualizadas_ids,
                                    valor_recebido,
                                )
                            _registrar_movimento_recebimento_cliente(
                                cliente,
                                valor_recebido,
                                data_recebimento,
                                valores["forma_pagamento"],
                            )
                    except RecebimentoContaErro as exc:
                        messages.warning(request, str(exc))
                    else:
                        if pix_baixado:
                            messages.success(
                                request,
                                f"Pix #{pix_baixado.id} marcado como baixado. Duplicados/inativos marcados: {duplicados_marcados}.",
                            )
                        saldo_atual_confirmacao = max(
                            (total_em_aberto - valor_aplicado_total).quantize(Decimal("0.01")),
                            Decimal("0.00"),
                        )
                        prazo_cliente = cliente.prazo_padrao_dias or 0
                        contas_abertas_confirmacao = []
                        contas_abertas_atuais = (
                            ContaReceber.objects.filter(
                                cliente_id=cliente.id,
                                status__in=[ContaReceber.STATUS_ABERTA, ContaReceber.STATUS_PARCIAL],
                                valor_em_aberto__gt=Decimal("0.00"),
                            )
                            .only("id", "venda_id", "data_emissao", "valor_original", "valor_em_aberto")
                            .order_by("data_emissao", "id")
                        )
                        for conta_aberta in contas_abertas_atuais:
                            data_nota = conta_aberta.data_emissao
                            dias_aberto = max((hoje - data_nota).days, 0) if data_nota else 0
                            contas_abertas_confirmacao.append({
                                "conta_id": conta_aberta.id,
                                "venda_id": conta_aberta.venda_id,
                                "data_nota": data_nota.strftime("%d/%m/%Y") if data_nota else "",
                                "saldo_atual": (conta_aberta.valor_em_aberto or Decimal("0.00")).quantize(Decimal("0.01")),
                                "dias_aberto": dias_aberto,
                                "em_atraso": bool(prazo_cliente and dias_aberto > prazo_cliente),
                            })
                        dados_confirmacao_whatsapp = {
                            "cliente_nome": cliente.nome,
                            "data_recebimento": data_recebimento.strftime("%d/%m/%Y"),
                            "saldo_anterior": total_em_aberto,
                            "valor_pago": valor_recebido,
                            "forma_pagamento": valores["forma_pagamento"],
                            "contas": contas_confirmacao_whatsapp,
                            "contas_abertas": contas_abertas_confirmacao,
                            "saldo_atual": saldo_atual_confirmacao,
                            "credito_gerado": credito_gerado_total,
                        }
                        comprovante_token = uuid4().hex
                        comprovante_dados = _serializar_dados_comprovante_recebimento(
                            cliente,
                            dados_confirmacao_whatsapp,
                        )
                        comprovantes_sessao = request.session.get("receber_cliente_comprovantes", {})
                        comprovantes_sessao[comprovante_token] = comprovante_dados
                        if len(comprovantes_sessao) > 8:
                            comprovantes_sessao = dict(list(comprovantes_sessao.items())[-8:])
                        request.session["receber_cliente_comprovantes"] = comprovantes_sessao
                        comprovante_url = reverse(
                            "estoque:receber_cliente_comprovante_imagem",
                            kwargs={"cliente_id": cliente.id, "token": comprovante_token},
                        )
                        request.session["receber_cliente_feedback"] = {
                            "cliente": cliente.nome,
                            "valor_aplicado": _formatar_moeda(valor_aplicado_total),
                            "valor_pago": _formatar_moeda(valor_recebido),
                            "saldo_anterior": _formatar_moeda(total_em_aberto),
                            "saldo_atual": _formatar_moeda(saldo_atual_confirmacao),
                            "credito_gerado": _formatar_moeda(credito_gerado_total),
                            "tem_credito_gerado": credito_gerado_total > Decimal("0.00"),
                            "forma_pagamento": valores["forma_pagamento"],
                            "data_recebimento": data_recebimento.strftime("%d/%m/%Y"),
                            "contas_afetadas": contas_afetadas,
                            "contas_atualizadas_ids": contas_atualizadas_ids,
                            "contas_atualizadas": contas_atualizadas_feedback,
                            "whatsapp_confirmacao": _montar_whatsapp_confirmacao_recebimento(
                                cliente,
                                dados_confirmacao_whatsapp,
                            ),
                            "comprovante_imagem_url": comprovante_url,
                        }
                        return redirect(destino_pos_recebimento)

    contas_preview = [
        {
            "id": conta.id,
            "venda_id": conta.venda_id,
            "valor_em_aberto": float(conta.valor_em_aberto or Decimal("0.00")),
        }
        for conta in contas
    ]
    tem_pix_em_atencao = _tem_pix_em_atencao()

    contexto = {
        "cliente": cliente,
        "contas": contas,
        "contas_preview": contas_preview,
        "total_contas": len(contas),
        "total_em_aberto": total_em_aberto,
        "credito_disponivel": credito_disponivel,
        "creditos_disponiveis": creditos_disponiveis,
        "saldo_resultante_credito": saldo_resultante_credito,
        "formas_pagamento": formas_pagamento,
        "valores": valores,
        "pagamentos_hoje_preview": pagamentos_hoje_preview,
        "pagamentos_recentes": pagamentos_recentes,
        "feedback_recebimento": feedback_recebimento,
        "contas_atualizadas_ids": (
            feedback_recebimento.get("contas_atualizadas_ids", [])
            if feedback_recebimento
            else []
        ),
        "contas_atualizadas_feedback": (
            feedback_recebimento.get("contas_atualizadas", {})
            if feedback_recebimento
            else {}
        ),
        "hoje_iso": hoje.isoformat(),
        "retorno_url": destino_retorno,
        "tem_pix_em_atencao": tem_pix_em_atencao,
        "pix_recebido_escolhido": pix_recebido_escolhido,
        "pix_detalhe_url": pix_detalhe_url,
        "pix_trocar_cliente_url": pix_trocar_cliente_url,
        "pix_remover_cliente_url": pix_remover_cliente_url,
    }
    response = render(request, "estoque/receber_cliente.html", contexto)
    return response


def receber_cliente_comprovante_imagem(request, cliente_id, token):
    comprovantes = request.session.get("receber_cliente_comprovantes", {})
    dados = comprovantes.get(token)
    if not dados or int(dados.get("cliente_id") or 0) != cliente_id:
        raise Http404("Comprovante nao encontrado.")

    buffer = _gerar_comprovante_recebimento_imagem(dados)
    response = HttpResponse(buffer.getvalue(), content_type="image/png")
    response["Content-Disposition"] = f'inline; filename="comprovante-pagamento-cliente-{cliente_id}.png"'
    return response


@ensure_csrf_cookie
def cliente_credito_detalhe(request, cliente_id):
    cliente = get_object_or_404(Cliente, pk=cliente_id)
    retorno_url = _url_retorno_segura(request)
    movimentos_qs = CreditoCliente.objects.filter(cliente=cliente)
    creditos_qs = (
        CreditoCliente.objects.select_related(
            "origem_conta_receber",
            "origem_conta_receber__venda",
            "origem_recebimento",
        )
        .filter(cliente=cliente)
        .exclude(valor=Decimal("0.00"))
        .order_by("-criado_em", "-id")
    )
    credito_atual = (
        movimentos_qs.aggregate(total=Sum("valor")).get("total")
        or Decimal("0.00")
    ).quantize(Decimal("0.01"))
    credito_atual = max(credito_atual, Decimal("0.00"))
    saldos_por_movimento = {}
    saldo_credito = Decimal("0.00")
    for movimento in creditos_qs.order_by("criado_em", "id"):
        saldo_antes = saldo_credito.quantize(Decimal("0.01"))
        saldo_credito = (saldo_credito + (movimento.valor or Decimal("0.00"))).quantize(Decimal("0.01"))
        saldos_por_movimento[movimento.id] = {
            "credito_antes": saldo_antes,
            "credito_depois": saldo_credito,
        }

    creditos = []
    for credito in creditos_qs:
        recebimento = credito.origem_recebimento
        valor_aplicado = (
            recebimento.valor
            if recebimento
            else Decimal("0.00")
        )
        valor_credito = credito.valor or Decimal("0.00")
        credito_utilizado = valor_credito < Decimal("0.00")
        valor_movimento = abs(valor_credito).quantize(Decimal("0.01"))
        saldos_movimento = saldos_por_movimento.get(
            credito.id,
            {"credito_antes": Decimal("0.00"), "credito_depois": Decimal("0.00")},
        )
        creditos.append(
            {
                "utilizado": credito_utilizado,
                "criado_em": credito.criado_em,
                "conta_id": credito.origem_conta_receber_id,
                "venda_id": (
                    credito.origem_conta_receber.venda_id
                    if credito.origem_conta_receber_id
                    else None
                ),
                "data_recebimento": recebimento.data_recebimento if recebimento else None,
                "valor_entregue": (valor_aplicado + valor_credito).quantize(Decimal("0.01")),
                "valor_aplicado": valor_aplicado,
                "valor_credito": valor_credito,
                "valor_movimento": valor_movimento,
                "credito_antes": saldos_movimento["credito_antes"],
                "credito_depois": saldos_movimento["credito_depois"],
                "forma_pagamento": recebimento.forma_pagamento if recebimento else "",
                "observacao": credito.observacao,
            }
        )

    return render(
        request,
        "estoque/cliente_credito_detalhe.html",
        {
            "cliente": cliente,
            "credito_atual": credito_atual,
            "creditos": creditos,
            "ultimo_movimento": creditos[0] if creditos else None,
            "retorno_url": retorno_url,
        },
    )


class RecebimentoContaErro(ValueError):
    def __init__(self, mensagem, destino="form"):
        super().__init__(mensagem)
        self.destino = destino


def _aplicar_recebimento_conta(
    conta,
    data_recebimento,
    valor_recebido,
    forma_pagamento,
    observacao,
    destino_diferenca,
    credito_utilizado=Decimal("0.00"),
):
    # Deve ser chamado dentro de transaction.atomic(); a conta deve estar bloqueada com select_for_update().
    if conta.status == ContaReceber.STATUS_CANCELADA:
        raise RecebimentoContaErro("Conta cancelada nao pode receber pagamento.", destino="retorno")
    if conta.status == ContaReceber.STATUS_PAGA:
        raise RecebimentoContaErro("Conta ja esta paga.", destino="retorno")

    saldo_atual = (conta.valor_em_aberto or Decimal("0.00")).quantize(Decimal("0.01"))
    if saldo_atual <= Decimal("0.00"):
        conta.status = ContaReceber.STATUS_PAGA
        conta.save(update_fields=["status", "atualizado_em"])
        raise RecebimentoContaErro("Conta ja esta paga.", destino="retorno")

    if credito_utilizado > Decimal("0.00"):
        if not conta.cliente_id:
            raise RecebimentoContaErro("Para usar credito, a conta precisa ter cliente vinculado.")
        credito_atual = (
            CreditoCliente.objects.select_for_update()
            .filter(cliente_id=conta.cliente_id)
            .aggregate(total=Sum("valor"))
            .get("total")
            or Decimal("0.00")
        ).quantize(Decimal("0.01"))
        credito_atual = max(credito_atual, Decimal("0.00"))
        if credito_utilizado > credito_atual:
            raise RecebimentoContaErro("Credito utilizado maior que o saldo disponivel do cliente.")
        if credito_utilizado > saldo_atual:
            raise RecebimentoContaErro("Credito utilizado maior que o saldo em aberto da conta.")

    total_para_baixa = (valor_recebido + credito_utilizado).quantize(Decimal("0.01"))
    valor_aplicado = min(total_para_baixa, saldo_atual).quantize(Decimal("0.01"))
    troco_devolvido = (total_para_baixa - valor_aplicado).quantize(Decimal("0.01"))
    if troco_devolvido > Decimal("0.00") and destino_diferenca == "credito" and not conta.cliente_id:
        raise RecebimentoContaErro("Para deixar diferenca como credito, a conta precisa ter cliente vinculado.")

    observacao_recebimento = observacao

    if troco_devolvido > Decimal("0.00"):
        descricao_diferenca = (
            f"Credito gerado para o cliente: {_formatar_moeda(troco_devolvido)}."
            if destino_diferenca == "credito"
            else f"Troco devolvido: {_formatar_moeda(troco_devolvido)}."
        )
        observacao_automatica = (
            f"Valor entregue pelo cliente: {_formatar_moeda(valor_recebido)}. "
            f"Valor aplicado na conta: {_formatar_moeda(valor_aplicado)}. "
            f"{descricao_diferenca}"
        )
        observacao_recebimento = (
            f"{observacao_recebimento}\n{observacao_automatica}"
            if observacao_recebimento
            else observacao_automatica
        )

    observacao_credito = ""
    if credito_utilizado > Decimal("0.00"):
        observacao_credito = (
            f"Valor recebido: {_formatar_moeda(valor_recebido)}. "
            f"Credito utilizado: {_formatar_moeda(credito_utilizado)}."
        )
        observacao_recebimento = (
            f"{observacao_recebimento}\n{observacao_credito}"
            if observacao_recebimento
            else observacao_credito
        )

    recebimento = RecebimentoContaReceber.objects.create(
        conta=conta,
        data_recebimento=data_recebimento,
        valor=valor_aplicado,
        forma_pagamento=(
            "Credito do cliente"
            if credito_utilizado > Decimal("0.00") and valor_recebido == Decimal("0.00")
            else forma_pagamento
        ),
        observacao=observacao_recebimento,
    )
    credito_gerado = Decimal("0.00")
    if credito_utilizado > Decimal("0.00"):
        CreditoCliente.objects.create(
            cliente=conta.cliente,
            valor=-credito_utilizado,
            tipo=CreditoCliente.TIPO_CREDITO_GERADO,
            origem_conta_receber=conta,
            origem_recebimento=recebimento,
            observacao=observacao_credito,
        )
    if troco_devolvido > Decimal("0.00") and destino_diferenca == "credito":
        CreditoCliente.objects.create(
            cliente=conta.cliente,
            valor=troco_devolvido,
            tipo=CreditoCliente.TIPO_CREDITO_GERADO,
            origem_conta_receber=conta,
            origem_recebimento=recebimento,
            observacao=observacao_recebimento,
        )
        credito_gerado = troco_devolvido

    novo_valor_aberto = (saldo_atual - valor_aplicado).quantize(Decimal("0.01"))
    conta.valor_em_aberto = novo_valor_aberto
    conta.status = (
        ContaReceber.STATUS_PAGA
        if novo_valor_aberto == Decimal("0.00")
        else ContaReceber.STATUS_PARCIAL
    )
    conta.save(update_fields=["valor_em_aberto", "status", "atualizado_em"])

    return {
        "conta": conta,
        "recebimento": recebimento,
        "valor_aplicado": valor_aplicado,
        "troco_devolvido": troco_devolvido,
        "credito_gerado": credito_gerado,
        "credito_utilizado": credito_utilizado,
        "saldo_restante": novo_valor_aberto,
    }


@ensure_csrf_cookie
def conta_receber_receber(request, pk):
    conta = get_object_or_404(
        ContaReceber.objects.select_related("cliente", "venda"),
        pk=pk,
    )
    retorno_url = _url_retorno_segura(request)
    destino_retorno = retorno_url or reverse("estoque:contas_receber")
    url_receber = reverse("estoque:conta_receber_receber", kwargs={"pk": conta.pk})
    if retorno_url:
        url_receber = f"{url_receber}?{urlencode({'next': retorno_url})}"

    if conta.status == ContaReceber.STATUS_CANCELADA:
        messages.warning(request, "Conta cancelada nao pode receber pagamento.")
        return redirect(destino_retorno)
    if conta.status == ContaReceber.STATUS_PAGA:
        messages.warning(request, "Conta ja esta paga.")
        return redirect(destino_retorno)

    formas_pagamento = (
        "Dinheiro",
        "PIX",
        "Cartao de debito",
        "Cartao de credito",
        "Transferencia",
        "Outro",
    )
    valores = {
        "data_recebimento": timezone.localdate().isoformat(),
        "valor": str(conta.valor_em_aberto or Decimal("0.00")),
        "forma_pagamento": "Dinheiro",
        "observacao": "",
        "destino_diferenca": "troco",
        "usar_credito": "",
        "credito_utilizado": "0,00",
    }
    pix_recebido_id = (
        request.POST.get("pix_recebido")
        or request.GET.get("pix_recebido", "")
    ).strip()
    pix_recebido_escolhido = None
    pix_detalhe_url = ""
    pix_remover_cliente_url = ""
    if pix_recebido_id.isdigit():
        pix_recebido_escolhido = PixRecebido.objects.select_related("pix_original").filter(pk=pix_recebido_id).first()
    if pix_recebido_escolhido:
        pix_detalhe_url = _url_detalhe_pix(pix_recebido_escolhido.id, request.get_full_path())
        pix_remover_cliente_url = reverse(
            "estoque:central_pix_remover_cliente_confirmado",
            kwargs={"pix_id": pix_recebido_escolhido.id},
        )
    if pix_recebido_escolhido and request.method != "POST":
        valores["valor"] = str((pix_recebido_escolhido.valor or Decimal("0.00")).quantize(Decimal("0.01")))
        valores["forma_pagamento"] = "PIX"
    pagamentos_recentes = []
    credito_disponivel = Decimal("0.00")
    credito_sugerido = Decimal("0.00")
    if conta.cliente_id:
        credito_disponivel = (
            CreditoCliente.objects.filter(cliente_id=conta.cliente_id)
            .aggregate(total=Sum("valor"))
            .get("total")
            or Decimal("0.00")
        ).quantize(Decimal("0.01"))
        credito_disponivel = max(credito_disponivel, Decimal("0.00"))
        credito_sugerido = min(
            credito_disponivel,
            conta.valor_em_aberto or Decimal("0.00"),
        ).quantize(Decimal("0.01"))
        valores["credito_utilizado"] = str(credito_sugerido)
        limite_recente = timezone.now() - timedelta(hours=72)
        recebimentos_recentes = (
            RecebimentoContaReceber.objects.select_related("conta", "conta__venda")
            .filter(conta__cliente_id=conta.cliente_id, criado_em__gte=limite_recente)
            .order_by("-criado_em", "-id")[:8]
        )
        pagamentos_recentes = [
            {
                "criado_em": recebimento.criado_em,
                "criado_em_data": timezone.localtime(recebimento.criado_em).date().isoformat(),
                "data_recebimento": recebimento.data_recebimento.isoformat() if recebimento.data_recebimento else "",
                "valor": recebimento.valor,
                "valor_numero": float(recebimento.valor or Decimal("0.00")),
                "forma_pagamento": recebimento.forma_pagamento,
                "conta_id": recebimento.conta_id,
                "venda_id": recebimento.conta.venda_id if recebimento.conta_id else "",
                "observacao": recebimento.observacao,
            }
            for recebimento in recebimentos_recentes
        ]

    if request.method == "POST":
        valores = {
            "data_recebimento": request.POST.get("data_recebimento", "").strip(),
            "valor": request.POST.get("valor", "").strip(),
            "forma_pagamento": request.POST.get("forma_pagamento", "").strip(),
            "observacao": request.POST.get("observacao", "").strip(),
            "destino_diferenca": request.POST.get("destino_diferenca", "troco").strip(),
            "usar_credito": request.POST.get("usar_credito", "").strip(),
            "credito_utilizado": request.POST.get("credito_utilizado", "").strip(),
        }
        data_recebimento = parse_date(valores["data_recebimento"])
        if not data_recebimento:
            messages.warning(request, "Informe uma data de recebimento valida.")
        elif valores["forma_pagamento"] not in formas_pagamento:
            messages.warning(request, "Selecione uma forma de pagamento.")
        elif valores["destino_diferenca"] not in {"troco", "credito"}:
            messages.warning(request, "Selecione como tratar a diferenca do pagamento.")
        else:
            try:
                valor_recebido = _decimal_do_front(valores["valor"] or "0", "0.01")
                credito_utilizado = (
                    _decimal_do_front(valores["credito_utilizado"] or "0", "0.01")
                    if valores["usar_credito"] == "1"
                    else Decimal("0.00")
                )
            except ValueError as exc:
                messages.warning(request, str(exc))
            else:
                if valor_recebido < Decimal("0.00"):
                    messages.warning(request, "Informe um valor recebido valido.")
                elif valores["usar_credito"] == "1" and credito_utilizado <= Decimal("0.00"):
                    messages.warning(request, "Informe um credito utilizado maior que zero.")
                elif valor_recebido <= Decimal("0.00") and credito_utilizado <= Decimal("0.00"):
                    messages.warning(request, "Informe um valor recebido ou use credito do cliente.")
                else:
                    with transaction.atomic():
                        conta_atual = ContaReceber.objects.select_for_update().get(pk=conta.pk)
                        try:
                            _aplicar_recebimento_conta(
                                conta_atual,
                                data_recebimento,
                                valor_recebido,
                                valores["forma_pagamento"],
                                valores["observacao"],
                                valores["destino_diferenca"],
                                credito_utilizado,
                            )
                            pix_baixado = None
                            duplicados_marcados = 0
                            if pix_recebido_id:
                                pix_baixado, duplicados_marcados = _marcar_pix_baixado_com_duplicados(
                                    pix_recebido_id,
                                    [conta_atual.id],
                                    valor_recebido,
                                )
                            _registrar_movimento_recebimento_cliente(
                                conta_atual.cliente or conta.cliente,
                                valor_recebido,
                                data_recebimento,
                                valores["forma_pagamento"],
                            )
                        except RecebimentoContaErro as exc:
                            transaction.set_rollback(True)
                            messages.warning(request, str(exc))
                            return redirect(
                                destino_retorno
                                if exc.destino == "retorno"
                                else url_receber
                            )

                    if pix_baixado:
                        messages.success(
                            request,
                            f"Pix #{pix_baixado.id} marcado como baixado. Duplicados/inativos marcados: {duplicados_marcados}.",
                        )
                    messages.success(request, "Recebimento registrado com sucesso.")
                    return redirect(destino_retorno)

    return render(
        request,
        "estoque/conta_receber_receber.html",
        {
            "conta": conta,
            "valores": valores,
            "formas_pagamento": formas_pagamento,
            "pagamentos_recentes": pagamentos_recentes,
            "credito_disponivel": credito_disponivel,
            "credito_sugerido": credito_sugerido,
            "hoje_iso": timezone.localdate().isoformat(),
            "retorno_url": retorno_url,
            "pix_recebido_escolhido": pix_recebido_escolhido,
            "pix_detalhe_url": pix_detalhe_url,
            "pix_remover_cliente_url": pix_remover_cliente_url,
        },
    )


@ensure_csrf_cookie
def entregas_dia(request):
    hoje = timezone.localdate()
    data_texto = (request.POST.get("data") if request.method == "POST" else request.GET.get("data", "")).strip()
    data_entrega = parse_date(data_texto) if data_texto else hoje
    if not data_entrega:
        data_entrega = hoje
        messages.warning(request, "Data invalida. Mostrando entregas de hoje.")

    if request.method == "POST":
        acao = request.POST.get("acao")
        observacao = request.POST.get("observacao", "").strip()

        if acao == "unitaria":
            venda_id = request.POST.get("venda_unitaria", "").strip()
            if not venda_id.isdigit():
                messages.warning(request, "Selecione uma venda para criar a entrega.", extra_tags="entrega-unitaria")
                return redirect(f"{reverse('estoque:entregas_dia')}?data={data_entrega.isoformat()}")

            venda = Venda.objects.filter(pk=venda_id).first()
            if not venda:
                messages.warning(request, "Selecione uma venda valida para criar a entrega.", extra_tags="entrega-unitaria")
                return redirect(f"{reverse('estoque:entregas_dia')}?data={data_entrega.isoformat()}")

            if EntregaRotaItem.objects.filter(venda=venda).exists():
                messages.warning(request, "Essa venda ja possui entrega criada.", extra_tags="entrega-unitaria")
                return redirect(f"{reverse('estoque:entregas_dia')}?data={data_entrega.isoformat()}")

            with transaction.atomic():
                rota = EntregaRota.objects.create(
                    data=data_entrega,
                    tipo=EntregaRota.TIPO_UNITARIA,
                    observacao=observacao,
                )
                EntregaRotaItem.objects.create(
                    rota=rota,
                    venda=venda,
                    ordem_entrega=1,
                )
            messages.success(request, f"Entrega unitaria #{rota.id} criada para a venda #{venda.id}.", extra_tags="entrega-unitaria")
            return redirect(f"{reverse('estoque:entregas_dia')}?data={data_entrega.isoformat()}")

        if acao == "rota":
            nome_rota = request.POST.get("nome_rota", "").strip()
            venda_ids = [
                venda_id.strip()
                for venda_id in request.POST.getlist("vendas_rota")
                if venda_id.strip().isdigit()
            ]
            pendencia_ids = [
                pendencia_id.strip()
                for pendencia_id in request.POST.getlist("pendencias_rota")
                if pendencia_id.strip().isdigit()
            ]
            pendencia_ids_selecionados = {int(pendencia_id) for pendencia_id in pendencia_ids}
            pendencias_selecionadas = pendencias_checklist_validas(pendencia_ids)
            vendas = list(Venda.objects.filter(pk__in=venda_ids).select_related("cliente"))
            if not vendas and not pendencias_selecionadas:
                messages.warning(request, "Selecione pelo menos uma venda para criar a rota.", extra_tags="rota-entrega")
                return redirect(f"{reverse('estoque:entregas_dia')}?data={data_entrega.isoformat()}")

            vendas_por_id = {str(venda.id): venda for venda in vendas}
            ordenadas = []
            for venda_id in venda_ids:
                venda = vendas_por_id.get(str(venda_id))
                if not venda:
                    continue
                ordem = ordem_postada(request.POST.get(f"ordem_{venda_id}"))
                ordenadas.append((ordem, venda.id, venda))

            ordenadas.sort(key=lambda item: (item[0], item[1]))
            nome_rota_final = localidade_principal_rota(ordenadas) or formatar_nome_rota(nome_rota)
            observacao_rota = observacao
            if nome_rota_final:
                observacao_rota = f"Rota: {nome_rota_final}"
                if observacao:
                    observacao_rota = f"{observacao_rota}\n{observacao}"

            ordem_por_pendencia = {}
            for pendencia_id in pendencia_ids:
                ordem_por_pendencia[int(pendencia_id)] = ordem_postada(
                    request.POST.get(f"ordem_pendencia_{pendencia_id}")
                )

            pendencias_por_venda = {}
            for checklist in pendencias_selecionadas:
                if checklist.id not in pendencia_ids_selecionados:
                    continue
                pendencias_por_venda.setdefault(checklist.rota_item.venda_id, {
                    "ordem": ordem_por_pendencia.get(checklist.id, 9999),
                    "checklists": [],
                })
                grupo = pendencias_por_venda[checklist.rota_item.venda_id]
                grupo["ordem"] = min(grupo["ordem"], ordem_por_pendencia.get(checklist.id, 9999))
                grupo["checklists"].append(checklist)

            pendencias_por_cliente = {}
            for venda_id_pendencia, dados_pendencia in pendencias_por_venda.items():
                checklists = dados_pendencia["checklists"]
                origem = checklists[0].rota_item
                chave_cliente = chave_cliente_entrega(origem.venda)
                pendencias_por_cliente.setdefault(chave_cliente, []).append((
                    dados_pendencia["ordem"],
                    venda_id_pendencia,
                    checklists,
                ))
            for pendencias_cliente in pendencias_por_cliente.values():
                pendencias_cliente.sort(key=lambda item: (item[0], item[1]))

            clientes_com_venda_normal = {
                chave_cliente_entrega(venda)
                for _, __, venda in ordenadas
            }
            itens_ordenaveis = [
                (ordem, venda_id, "normal", venda, None)
                for ordem, venda_id, venda in ordenadas
            ]
            for venda_id_pendencia, dados_pendencia in pendencias_por_venda.items():
                checklists = dados_pendencia["checklists"]
                venda_pendencia = checklists[0].rota_item.venda
                if chave_cliente_entrega(venda_pendencia) in clientes_com_venda_normal:
                    continue
                itens_ordenaveis.append((
                    dados_pendencia["ordem"],
                    venda_id_pendencia,
                    "pendencia",
                    venda_pendencia,
                    checklists,
                ))

            itens_ordenaveis.sort(key=lambda item: (item[0], item[1]))
            sequencia_rota = []
            for indice_item, (_, __, tipo_item, venda, checklists) in enumerate(itens_ordenaveis):
                sequencia_rota.append((tipo_item, venda, checklists))
                if tipo_item != "normal":
                    continue

                chave_cliente = chave_cliente_entrega(venda)
                tem_outra_venda_cliente = any(
                    tipo_futuro == "normal"
                    and chave_cliente_entrega(venda_futura) == chave_cliente
                    for _, __, tipo_futuro, venda_futura, ___ in itens_ordenaveis[indice_item + 1:]
                )
                if tem_outra_venda_cliente:
                    continue
                for _, ___, checklists_pendencia in pendencias_por_cliente.get(chave_cliente, []):
                    sequencia_rota.append(("pendencia", checklists_pendencia[0].rota_item.venda, checklists_pendencia))

            with transaction.atomic():
                rota = EntregaRota.objects.create(
                    data=data_entrega,
                    tipo=EntregaRota.TIPO_ROTA,
                    observacao=observacao_rota,
                )
                for ordem_entrega, (tipo_item, venda, checklists_origem) in enumerate(sequencia_rota, start=1):
                    if tipo_item == "normal":
                        EntregaRotaItem.objects.create(
                            rota=rota,
                            venda=venda,
                            ordem_entrega=ordem_entrega,
                        )
                        continue

                    checklists_para_criar = checklists_pendencia_selecionados(
                        checklists_origem,
                        pendencia_ids_selecionados,
                    )
                    if not checklists_para_criar:
                        continue

                    origem = checklists_origem[0].rota_item
                    item_pendencia = EntregaRotaItem.objects.create(
                        rota=rota,
                        venda=venda,
                        ordem_entrega=ordem_entrega,
                        is_pendencia=True,
                        origem_pendencia=origem,
                        observacao=(
                            f"Pendencia incluida da rota #{origem.rota_id} "
                            f"em {origem.rota.data:%d/%m/%Y}."
                        ),
                    )
                    EntregaChecklistItem.objects.bulk_create([
                        EntregaChecklistItem(rota_item=item_pendencia, item_venda=checklist.item_venda)
                        for checklist in checklists_para_criar
                    ])
            total_entregas = len(sequencia_rota)
            messages.success(request, f"Rota #{rota.id} criada com {total_entregas} entrega(s).", extra_tags="rota-entrega")
            return redirect(f"{reverse('estoque:entregas_dia')}?data={data_entrega.isoformat()}&rota_criada={rota.id}#rota-{rota.id}")

    vendas_lista = list(
        Venda.objects.select_related("cliente")
        .filter(data_venda=data_entrega)
        .order_by("-id")
    )
    vendas_ids = [venda.id for venda in vendas_lista]
    itens_entrega = EntregaRotaItem.objects.filter(
        venda_id__in=vendas_ids,
    ).select_related("rota").order_by("-rota__data", "-rota_id", "-id")
    status_por_venda = {}
    for item in itens_entrega:
        status_por_venda.setdefault(
            item.venda_id,
            f"{item.rota.get_tipo_display()} #{item.rota_id} - {item.get_status_display()}",
        )
    for venda in vendas_lista:
        venda.tem_entrega_criada = venda.id in status_por_venda
        venda.entrega_status_texto = status_por_venda.get(venda.id, "Sem entrega criada")

    rotas = list(
        EntregaRota.objects.filter(data=data_entrega)
        .prefetch_related("itens__venda__cliente", "itens__checklist_itens__item_venda__produto")
        .order_by("-id")
    )
    for rota in rotas:
        itens = list(rota.itens.all())
        for item in itens:
            item.resumo_pendencia = resumo_pendencia_rota_item(item)
        rota.itens_entrega = itens
        rota.itens_carregamento = list(reversed(itens))
        rota.checklist_path = reverse("estoque:entrega_rota_checklist", kwargs={"pk": rota.id})
        rota.checklist_url = montar_checklist_url(request, rota.id)
        rota.pode_excluir = rota_pode_ser_excluida(rota)

    return render(
        request,
        "estoque/entregas_dia.html",
        {
            "data_entrega": data_entrega,
            "vendas": vendas_lista,
            "rotas": rotas,
            "total_vendas": len(vendas_lista),
            "funcionarios_habilitados": Funcionario.habilitados_para_checklist(),
            "rota_criada_id": request.GET.get("rota_criada", ""),
            "total_pendencias_entrega": len(listar_pendencias_entrega()),
            "pendencias_sugeridas": pendencias_sugeriveis_entrega(),
        },
    )


def pendencias_entrega(request):
    exibindo_resolvidas = request.GET.get("status") == "resolvidas"
    filtros_resolvidas = {
        "cliente": request.GET.get("cliente", "").strip(),
        "venda": request.GET.get("venda", "").strip(),
        "data_inicial": request.GET.get("data_inicial", "").strip(),
        "data_final": request.GET.get("data_final", "").strip(),
        "produto": request.GET.get("produto", "").strip(),
    }
    pendencias_abertas = listar_pendencias_entrega()
    pendencias_resolvidas = listar_pendencias_resolvidas_entrega(
        filtros=filtros_resolvidas if exibindo_resolvidas else None
    )
    pendencias = pendencias_resolvidas if exibindo_resolvidas else pendencias_abertas
    return render(
        request,
        "estoque/pendencias_entrega.html",
        {
            "pendencias": pendencias,
            "total_pendencias": len(pendencias),
            "total_pendencias_abertas": len(pendencias_abertas),
            "total_pendencias_resolvidas": len(pendencias_resolvidas),
            "exibindo_resolvidas": exibindo_resolvidas,
            "filtros_resolvidas": filtros_resolvidas,
        },
    )


def revisar_remocao_pendencia_da_nota(request, checklist_id):
    checklist = (
        EntregaChecklistItem.objects.select_related(
            "item_venda",
            "item_venda__produto",
            "rota_item",
            "rota_item__rota",
            "rota_item__venda",
            "rota_item__venda__cliente",
            "rota_item__origem_pendencia",
        )
        .prefetch_related("rota_item__checklist_itens", "rota_item__venda__itens__produto")
        .filter(pk=checklist_id)
        .first()
    )
    if not checklist:
        messages.warning(request, "Pendencia nao encontrada ou ja resolvida.")
        return redirect("estoque:pendencias_entrega")

    item_rota = checklist.rota_item
    venda = item_rota.venda
    item_venda = checklist.item_venda
    if checklist.entregue or checklist not in checklists_validos_rota_item(item_rota):
        messages.warning(request, "Essa pendencia nao esta mais disponivel para remocao da nota.")
        return redirect("estoque:pendencias_entrega")

    total_atual = venda.total or Decimal("0.00")
    novo_total = calcular_total_itens_venda(venda, excluir_item_id=item_venda.id)
    voltar_url = request.GET.get("next") or reverse("estoque:pendencias_entrega")

    if request.method == "POST":
        with transaction.atomic():
            checklist_confirmacao = (
                EntregaChecklistItem.objects.filter(pk=checklist_id)
                .first()
            )
            if not checklist_confirmacao:
                messages.warning(request, "Pendencia nao encontrada ou ja resolvida.")
                return redirect("estoque:pendencias_entrega")

            item_rota = (
                EntregaRotaItem.objects.select_related("origem_pendencia")
                .prefetch_related("checklist_itens")
                .get(pk=checklist_confirmacao.rota_item_id)
            )
            venda = Venda.objects.get(pk=item_rota.venda_id)
            item_venda = ItemVenda.objects.filter(
                pk=checklist_confirmacao.item_venda_id,
                venda=venda,
            ).select_related("produto").first()
            if not item_venda:
                messages.warning(request, "O item da nota ja nao existe mais. Nenhuma alteracao foi feita.")
                return redirect("estoque:pendencias_entrega")

            if checklist_confirmacao.entregue or checklist_confirmacao not in checklists_validos_rota_item(item_rota):
                messages.warning(request, "Essa pendencia nao esta mais disponivel para remocao da nota.")
                return redirect("estoque:pendencias_entrega")

            produto_nome = item_venda.produto.nome if item_venda.produto else "Produto nao identificado"
            quantidade = item_venda.quantidade
            unidade = item_venda.unidade
            valor_total = item_venda.valor_total or Decimal("0.00")
            total_anterior = venda.total or Decimal("0.00")
            venda_id = venda.id
            rota_id = item_rota.rota_id
            rota_item_ids_afetados = list(
                EntregaChecklistItem.objects.filter(item_venda=item_venda)
                .values_list("rota_item_id", flat=True)
            )
            estoque_devolvido = False
            if item_venda.produto_id:
                _devolver_estoque_produto(item_venda.produto_id, quantidade, produto_nome, unidade)
                estoque_devolvido = True
            item_venda.delete()
            resolver_entregas_sem_pendencias_ativas(rota_item_ids_afetados)
            novo_total = recalcular_total_venda(venda)
            _anular_venda_sem_itens_por_remocao_pendencia(venda)
            _sincronizar_conta_receber(venda, "pendencia removida da nota")

            _registrar_evento_venda(
                venda,
                "pendencia_removida_da_nota",
                descricao_pendencia_removida_da_nota(
                    rota_id,
                    produto_nome,
                    quantidade,
                    unidade,
                    valor_total,
                    total_anterior,
                    novo_total,
                )
                + (
                    " Estoque devolvido para o produto removido."
                    if estoque_devolvido
                    else " Sem devolucao de estoque para este item."
                ),
                canal="sistema",
            )

        messages.success(
            request,
            "Item removido da nota e pendencia resolvida com sucesso.",
        )
        return redirect(f"{reverse('estoque:venda_detalhe', kwargs={'pk': venda_id})}?origem=pendencias#dados-da-nota")

    return render(
        request,
        "estoque/revisar_remocao_pendencia_da_nota.html",
        {
            "checklist": checklist,
            "item_rota": item_rota,
            "rota": item_rota.rota,
            "venda": venda,
            "cliente": venda.cliente,
            "item_venda": item_venda,
            "itens_venda": venda.itens.all(),
            "produto_nome": item_venda.produto.nome if item_venda.produto else "Produto nao identificado",
            "total_atual": total_atual,
            "novo_total": novo_total,
            "voltar_url": voltar_url,
        },
    )


@require_POST
def entrega_rota_excluir(request, pk):
    rota = EntregaRota.objects.prefetch_related("itens__checklist_itens").filter(pk=pk).first()
    if not rota:
        data_texto = request.POST.get("data", "").strip()
        data_entrega = parse_date(data_texto) if data_texto else timezone.localdate()
        if not data_entrega:
            data_entrega = timezone.localdate()
        messages.warning(request, "Rota nao encontrada ou ja foi excluida.")
        return redirect(f"{reverse('estoque:entregas_dia')}?data={data_entrega.isoformat()}")

    data_entrega = rota.data

    if not rota_pode_ser_excluida(rota):
        messages.warning(
            request,
            f"Rota #{rota.id} ja possui conferencia ou uso registrado e nao pode ser excluida.",
        )
        return redirect(f"{reverse('estoque:entregas_dia')}?data={data_entrega.isoformat()}#rota-{rota.id}")

    rota_id = rota.id
    rota_foco = (
        EntregaRota.objects.filter(data=data_entrega, id__lt=rota_id)
        .order_by("-id")
        .first()
        or EntregaRota.objects.filter(data=data_entrega, id__gt=rota_id)
        .order_by("id")
        .first()
    )
    with transaction.atomic():
        rota.delete()

    messages.success(request, f"Rota #{rota_id} excluida com sucesso.")
    redirect_url = f"{reverse('estoque:entregas_dia')}?data={data_entrega.isoformat()}"
    if rota_foco:
        redirect_url = f"{redirect_url}&rota_criada={rota_foco.id}#rota-{rota_foco.id}"
    return redirect(redirect_url)


def entrega_rota_detalhe(request, pk):
    rota = get_object_or_404(
        EntregaRota.objects.prefetch_related(
            "itens__venda__cliente",
            "itens__venda__itens__produto",
            "itens__checklist_itens__item_venda__produto",
        ),
        pk=pk,
    )
    itens_entrega = list(rota.itens.all())
    for item_rota in itens_entrega:
        item_rota.checklists_ordenados = checklists_validos_rota_item(item_rota)
        item_rota.resumo_pendencia = resumo_pendencia_rota_item(item_rota)
    itens_carregamento = list(reversed(itens_entrega))
    checklist_url = montar_checklist_url(request, rota.id)

    return render(
        request,
        "estoque/entrega_rota_detalhe.html",
        {
            "rota": rota,
            "itens_entrega": itens_entrega,
            "itens_carregamento": itens_carregamento,
            "checklist_url": checklist_url,
            "funcionarios_habilitados": Funcionario.habilitados_para_checklist(),
        },
    )


def entrega_rota_checklist(request, pk):
    rota = get_object_or_404(
        EntregaRota.objects.prefetch_related(
            "itens__venda__cliente",
            "itens__venda__itens__produto",
            "itens__checklist_itens",
        ),
        pk=pk,
    )
    itens_entrega = list(rota.itens.all())

    with transaction.atomic():
        for item_rota in itens_entrega:
            itens_venda = list(item_rota.venda.itens.all())
            existentes = {
                checklist.item_venda_id: checklist
                for checklist in item_rota.checklist_itens.all()
            }
            novos = [
                EntregaChecklistItem(rota_item=item_rota, item_venda=item_venda)
                for item_venda in itens_venda
                if item_venda.id not in existentes and not item_rota.is_pendencia
            ]
            if novos:
                EntregaChecklistItem.objects.bulk_create(novos)

    rota = get_object_or_404(
        EntregaRota.objects.prefetch_related(
            "itens__venda__cliente",
            "itens__venda__itens__produto",
            "itens__checklist_itens__item_venda__produto",
        ),
        pk=pk,
    )
    itens_entrega = list(rota.itens.all())

    if request.method == "POST":
        bloco_salvo = request.POST.get("salvar_bloco") or request.POST.get("salvar_bloco_alvo", "")
        bloco_partes = bloco_salvo.split(":", 1)
        bloco_rota_item_id = bloco_partes[0] if bloco_partes else ""
        bloco_fase = bloco_partes[1] if len(bloco_partes) > 1 else ""
        rota_item_ids = [item_rota.id for item_rota in itens_entrega]
        bloco_valido = bloco_rota_item_id.isdigit() and int(bloco_rota_item_id) in rota_item_ids
        if bloco_valido:
            rota_item_ids = [int(bloco_rota_item_id)]

        checklists_validos_ids = set()
        for item_rota in itens_entrega:
            if item_rota.id in rota_item_ids:
                checklists_validos_ids.update(
                    checklist.id for checklist in checklists_validos_rota_item(item_rota)
                )
        checklist_qs = EntregaChecklistItem.objects.filter(pk__in=checklists_validos_ids)

        for checklist in checklist_qs:
            if bloco_fase == "carregamento":
                checklist.carregado = f"carregado_{checklist.id}" in request.POST
                checklist.save(update_fields=["carregado", "atualizado_em"])
            elif bloco_fase == "entrega":
                checklist.entregue = f"entregue_{checklist.id}" in request.POST
                checklist.save(update_fields=["entregue", "atualizado_em"])

        if bloco_fase == "entrega":
            for item_rota in itens_entrega:
                if item_rota.id not in rota_item_ids:
                    continue
                item_rota.conferido_cliente = f"conferido_{item_rota.id}" in request.POST
                item_rota.entrega_concluida = f"concluida_{item_rota.id}" in request.POST
                item_rota.save(update_fields=["conferido_cliente", "entrega_concluida"])

        if bloco_valido and bloco_fase in CHECKLIST_FASE_MARKERS:
            item_rota_salvo = next(
                (item_rota for item_rota in itens_entrega if item_rota.id == int(bloco_rota_item_id)),
                None,
            )
            if item_rota_salvo:
                marcar_checklist_fase_salva(item_rota_salvo, bloco_fase)

        redirect_url = reverse("estoque:entrega_rota_checklist", kwargs={"pk": rota.id})
        if bloco_valido and bloco_fase in {"carregamento", "entrega"}:
            redirect_url = f"{redirect_url}?salvo_item={bloco_rota_item_id}&salvo_fase={bloco_fase}"
        else:
            messages.success(request, "Checklist salvo com sucesso.", extra_tags="checklist-global")
        return redirect(redirect_url)

    rota = get_object_or_404(
        EntregaRota.objects.prefetch_related(
            "itens__venda__cliente",
            "itens__venda__itens__produto",
            "itens__checklist_itens__item_venda__produto",
        ),
        pk=pk,
    )
    itens_entrega = list(rota.itens.all())
    salvo_item_id = request.GET.get("salvo_item", "")
    salvo_fase = request.GET.get("salvo_fase", "")
    eventos_checklist_enviado = set(
        EventoVenda.objects.filter(
            venda_id__in=[item_rota.venda_id for item_rota in itens_entrega],
            tipo_evento="checklist_cliente_enviado",
            canal="whatsapp_checklist",
            descricao__icontains=f"rota/entrega #{rota.id}",
        ).values_list("descricao", flat=True)
    )
    for item_rota in itens_entrega:
        item_rota.salvo_carregamento = salvo_item_id == str(item_rota.id) and salvo_fase == "carregamento"
        item_rota.salvo_entrega = salvo_item_id == str(item_rota.id) and salvo_fase == "entrega"
        checklists_validos = checklists_validos_rota_item(item_rota)
        item_rota.checklists_por_item = {
            checklist.item_venda_id: checklist
            for checklist in checklists_validos
        }
        item_rota.checklists_ordenados = [
            item_rota.checklists_por_item.get(item_venda.id)
            for item_venda in item_rota.venda.itens.all()
            if item_rota.checklists_por_item.get(item_venda.id)
        ]
        item_rota.resumo_pendencia = resumo_pendencia_rota_item(item_rota)
        item_rota.carregamento_salvo = checklist_fase_salva(item_rota, "carregamento")
        item_rota.entrega_salva = checklist_fase_salva(item_rota, "entrega")
        item_rota.carregamento_completo = (
            bool(item_rota.checklists_ordenados)
            and all(checklist.carregado for checklist in item_rota.checklists_ordenados)
        )
        item_rota.entrega_completa = (
            bool(item_rota.checklists_ordenados)
            and all(checklist.entregue for checklist in item_rota.checklists_ordenados)
        )
        item_rota.carregamento_conferido = (
            item_rota.salvo_carregamento
            or item_rota.carregamento_salvo
            or item_rota.carregamento_completo
        )
        item_rota.entrega_conferida = (
            item_rota.salvo_entrega
            or item_rota.entrega_salva
            or item_rota.conferido_cliente
            or item_rota.entrega_concluida
            or item_rota.entrega_completa
        )
        item_rota.carregamento_pendente_salva = (
            item_rota.carregamento_conferido
            and bool(item_rota.checklists_ordenados)
            and not item_rota.carregamento_completo
        )
        item_rota.entrega_pendente_salva = (
            item_rota.entrega_conferida
            and bool(item_rota.checklists_ordenados)
            and not item_rota.entrega_completa
        )
        item_rota.checklist_cliente_enviado = any(
            f"bloco #{item_rota.id}" in descricao
            for descricao in eventos_checklist_enviado
        )

    itens_entrega = sorted(
        itens_entrega,
        key=lambda item_rota: (item_rota.ordem_entrega, item_rota.id),
    )
    itens_entrega_pendentes = [
        item_rota for item_rota in itens_entrega if not item_rota.entrega_conferida
    ]
    itens_entrega_conferidos = [
        item_rota for item_rota in itens_entrega if item_rota.entrega_conferida
    ]
    itens_carregamento = sorted(
        itens_entrega,
        key=lambda item_rota: (-item_rota.ordem_entrega, -item_rota.id),
    )
    itens_carregamento_pendentes = [
        item_rota for item_rota in itens_carregamento if not item_rota.carregamento_conferido
    ]
    itens_carregamento_conferidos = [
        item_rota for item_rota in itens_carregamento if item_rota.carregamento_conferido
    ]

    for item_rota in itens_entrega:
        item_rota.checklist_url = montar_checklist_cliente_url(request, rota.id, item_rota.venda_id, item_rota.id)

    return render(
        request,
        "estoque/entrega_checklist.html",
        {
            "rota": rota,
            "itens_entrega": itens_entrega,
            "itens_entrega_pendentes": itens_entrega_pendentes,
            "itens_entrega_conferidos": itens_entrega_conferidos,
            "itens_carregamento": itens_carregamento,
            "itens_carregamento_pendentes": itens_carregamento_pendentes,
            "itens_carregamento_conferidos": itens_carregamento_conferidos,
            "salvo_item_id": salvo_item_id,
            "salvo_fase": salvo_fase,
            "checklist_url": montar_checklist_url(request, rota.id),
            "funcionarios_habilitados": Funcionario.habilitados_para_checklist(),
        },
    )


def entrega_rota_checklist_cliente(request, rota_id, venda_id=None, rota_item_id=None):
    item_rota_qs = EntregaRotaItem.objects.select_related("rota", "venda", "venda__cliente").prefetch_related(
        "checklist_itens__item_venda__produto", "venda__itens__produto"
    )
    if rota_item_id:
        item_rota = get_object_or_404(item_rota_qs, rota_id=rota_id, pk=rota_item_id)
    else:
        item_rota = item_rota_qs.filter(rota_id=rota_id, venda_id=venda_id).order_by("is_pendencia", "id").first()
        if not item_rota:
            raise Http404("Checklist do cliente nao encontrado.")

    with transaction.atomic():
        itens_venda = list(item_rota.venda.itens.all())
        existentes = {
            checklist.item_venda_id: checklist
            for checklist in item_rota.checklist_itens.all()
        }
        novos = [
            EntregaChecklistItem(rota_item=item_rota, item_venda=item_venda)
            for item_venda in itens_venda
            if item_venda.id not in existentes and not item_rota.is_pendencia
        ]
        if novos:
            EntregaChecklistItem.objects.bulk_create(novos)

    checklists_validos = checklists_validos_rota_item(item_rota)
    checklists = {
        checklist.item_venda_id: checklist
        for checklist in checklists_validos
    }

    itens = []
    for item_venda in itens_venda:
        checklist = checklists.get(item_venda.id)
        if not checklist:
            continue
        if checklist.entregue:
            status = "Entregue"
        elif checklist.carregado:
            status = "Carregado"
        else:
            status = "Pendente"
        itens.append({
            "produto_nome": item_venda.produto.nome if item_venda.produto else "Produto nao identificado",
            "quantidade": item_venda.quantidade,
            "unidade": item_venda.unidade,
            "carregado": checklist.carregado,
            "entregue": checklist.entregue,
            "status": status,
        })

    final_statuses = []
    if item_rota.conferido_cliente:
        final_statuses.append("Entrega conferida com cliente")
    if item_rota.entrega_concluida:
        final_statuses.append("Entrega concluída")

    return render(
        request,
        "estoque/entrega_checklist_cliente.html",
        {
            "rota": item_rota.rota,
            "item_rota": item_rota,
            "venda": item_rota.venda,
            "cliente": item_rota.venda.cliente,
            "itens": itens,
            "final_statuses": final_statuses,
            "checklist_url": montar_checklist_cliente_url(request, rota_id, item_rota.venda_id, item_rota.id),
        },
    )


def _decimal_do_front(valor, casas="0.01"):
    if valor is None:
        raise ValueError("Valor numerico ausente.")

    texto = str(valor).strip().replace("R$", "").replace(" ", "")
    if "," in texto and "." in texto:
        texto = texto.replace(".", "").replace(",", ".")
    else:
        texto = texto.replace(",", ".")

    try:
        return Decimal(texto).quantize(Decimal(casas))
    except (InvalidOperation, ValueError):
        raise ValueError("Valor numerico invalido.")


def _recalcular_total_venda_pelos_itens(venda):
    total_recalculado = sum(
        (
            valor or Decimal("0.00")
            for valor in ItemVenda.objects.filter(venda=venda).values_list("valor_total", flat=True)
        ),
        Decimal("0.00"),
    )
    return total_recalculado.quantize(Decimal("0.01"))


def _ids_itens_adicionados_param(request):
    ids = []
    texto_ids = request.GET.get("itens_adicionados") or request.POST.get("itens_adicionados") or ""
    for parte in str(texto_ids).split(","):
        parte = parte.strip()
        if parte.isdigit():
            ids.append(int(parte))

    item_unico = request.GET.get("item_adicionado") or request.POST.get("item_adicionado") or ""
    if str(item_unico).isdigit():
        ids.append(int(item_unico))

    return list(dict.fromkeys(ids))


def _quantidade_decimal_estoque(quantidade):
    return Decimal(quantidade or "0").quantize(Decimal("0.001"))


def _quantidade_estoque_inteira(quantidade, produto_nome, unidade=None):
    quantidade_decimal = Decimal(quantidade or "0").quantize(Decimal("0.001"))
    if quantidade_decimal != quantidade_decimal.to_integral_value():
        unidade_texto = str(unidade or "").strip()
        unidade_normalizada = unidade_texto.upper()
        unidades_que_podem_meio = {"PCT", "PACOTE", "FARDO", "FD", "CX", "CAIXA"}
        if unidade_normalizada not in unidades_que_podem_meio:
            unidade_sufixo = f" em {unidade_texto}" if unidade_texto else ""
            raise ValueError(
                f"Produto {produto_nome} nao permite venda fracionada. "
                f"Informe quantidade inteira{unidade_sufixo}."
            )
    return quantidade_decimal


def _normalizar_unidade_estoque(unidade):
    return str(unidade or "").strip().upper()


def _quantidade_estoque_para_unidade_base(produto, quantidade, unidade=None):
    quantidade_decimal = Decimal(quantidade or "0").quantize(Decimal("0.001"))
    unidade_recebida = _normalizar_unidade_estoque(unidade)
    unidade_base = produto.unidade_venda_1 or produto.unidade_compra or ""
    unidade_base_norm = _normalizar_unidade_estoque(unidade_base)
    unidade_fracionada = produto.unidade_venda_2 or ""
    unidade_fracionada_norm = _normalizar_unidade_estoque(unidade_fracionada)
    fator = Decimal(produto.fator_conversao or 0)

    if unidade_recebida and unidade_base_norm and unidade_recebida == unidade_base_norm:
        return quantidade_decimal, unidade_base

    if (
        unidade_recebida
        and produto.vende_fracionado
        and unidade_fracionada_norm
        and unidade_recebida == unidade_fracionada_norm
    ):
        if fator <= 0:
            raise ValueError(f"Fator de conversao invalido para {produto.nome}.")
        return (quantidade_decimal / fator).quantize(Decimal("0.001")), unidade_base

    if not unidade_recebida:
        return quantidade_decimal, unidade_base

    unidades_validas = [u for u in [unidade_base, unidade_fracionada if produto.vende_fracionado else ""] if u]
    raise ValueError(
        f"Unidade {unidade} nao confere com o cadastro de {produto.nome}. "
        f"Use: {', '.join(unidades_validas) or 'unidade cadastrada'}."
    )


def _estoque_disponivel_na_unidade(produto, unidade=None):
    estoque_base = Decimal(produto.quantidade or 0)
    unidade_recebida = _normalizar_unidade_estoque(unidade)
    unidade_fracionada_norm = _normalizar_unidade_estoque(produto.unidade_venda_2)
    fator = Decimal(produto.fator_conversao or 0)

    if (
        unidade_recebida
        and produto.vende_fracionado
        and unidade_fracionada_norm
        and unidade_recebida == unidade_fracionada_norm
        and fator > 0
    ):
        return (estoque_base * fator).quantize(Decimal("0.001"))

    return estoque_base.quantize(Decimal("0.001"))


def _mensagem_estoque_insuficiente(produto, quantidade, unidade, estoque_disponivel):
    unidade_texto = str(unidade or produto.unidade_venda_1 or produto.unidade_compra or "").strip()
    unidade_sufixo = f" {unidade_texto}" if unidade_texto else ""
    return (
        f"Estoque insuficiente para {produto.nome}. "
        f"Solicitado: {_formatar_quantidade(quantidade)}{unidade_sufixo}. "
        f"Disponivel: {_formatar_quantidade(estoque_disponivel)}{unidade_sufixo}."
    )


def _baixar_estoque_produto(produto_id, quantidade, produto_nome=None, unidade=None):
    produto = Produto.objects.select_for_update().get(pk=produto_id)
    nome = produto_nome or produto.nome
    quantidade_base, unidade_base = _quantidade_estoque_para_unidade_base(produto, quantidade, unidade)
    quantidade_movimento = (
        quantidade_base
        if produto.vende_fracionado
        else _quantidade_estoque_inteira(quantidade_base, nome, unidade_base)
    )
    estoque_atual = _quantidade_decimal_estoque(produto.quantidade)
    estoque_disponivel_unidade = _estoque_disponivel_na_unidade(produto, unidade)
    print(
        "[venda estoque]",
        f"produto={produto.nome}",
        f"id={produto.pk}",
        f"unidade_recebida={unidade or ''}",
        f"quantidade_recebida={quantidade}",
        f"estoque_base={estoque_atual} {unidade_base or ''}".strip(),
        f"quantidade_base_comparada={quantidade_base} {unidade_base or ''}".strip(),
        f"estoque_comparacao={estoque_disponivel_unidade} {unidade or unidade_base or ''}".strip(),
    )
    if estoque_atual < quantidade_movimento:
        print(
            "[venda estoque bloqueio]",
            f"motivo=estoque_insuficiente produto={produto.nome} id={produto.pk}",
        )
        raise ValueError(
            _mensagem_estoque_insuficiente(produto, quantidade, unidade, estoque_disponivel_unidade)
        )
    produto.quantidade = (estoque_atual - quantidade_movimento).quantize(Decimal("0.001"))
    Produto.objects.filter(pk=produto.pk).update(
        quantidade=produto.quantidade,
        atualizado_em=timezone.now(),
    )
    print(
        "[venda estoque baixa]",
        f"produto={produto.nome}",
        f"id={produto.pk}",
        f"antes={estoque_atual}",
        f"baixado={quantidade_movimento}",
        f"depois={produto.quantidade}",
    )
    return estoque_atual, produto.quantidade


def _devolver_estoque_produto(produto_id, quantidade, produto_nome=None, unidade=None):
    produto = Produto.objects.select_for_update().get(pk=produto_id)
    nome = produto_nome or produto.nome
    quantidade_base, _unidade_base = _quantidade_estoque_para_unidade_base(produto, quantidade, unidade)
    quantidade_movimento = (
        quantidade_base
        if produto.vende_fracionado
        else _quantidade_estoque_inteira(quantidade_base, nome, _unidade_base)
    )
    estoque_atual = _quantidade_decimal_estoque(produto.quantidade)
    produto.quantidade = (estoque_atual + quantidade_movimento).quantize(Decimal("0.001"))
    Produto.objects.filter(pk=produto.pk).update(
        quantidade=produto.quantidade,
        atualizado_em=timezone.now(),
    )
    return estoque_atual, produto.quantidade


def _baixar_estoque_movimentos(movimentos):
    movimentos_normalizados = []
    for movimento in movimentos:
        if len(movimento) == 2:
            produto, quantidade = movimento
            unidade = None
        else:
            produto, quantidade, unidade = movimento
        if not produto:
            raise ValueError("Produto informado nao foi encontrado no estoque.")
        movimentos_normalizados.append((produto.pk, produto.nome, quantidade, unidade))

    for produto_id, produto_nome, quantidade, unidade in sorted(
        movimentos_normalizados,
        key=lambda movimento: (movimento[0], movimento[1]),
    ):
        _baixar_estoque_produto(produto_id, quantidade, produto_nome, unidade)


def _atualizar_saldo_pendente_pedido(pedido, itens_vendidos):
    vendidos_por_produto = {}
    for item in itens_vendidos:
        produto = item.get("produto")
        if not produto:
            continue
        vendidos_por_produto[produto.pk] = (
            vendidos_por_produto.get(produto.pk, Decimal("0.000"))
            + Decimal(item.get("quantidade") or "0")
        )

    total_pendente = Decimal("0.00")
    itens_pedido = pedido.itens.select_for_update().order_by("id")
    for item_pedido in itens_pedido:
        quantidade_vendida = vendidos_por_produto.get(item_pedido.produto_id, Decimal("0.000"))
        if quantidade_vendida > 0:
            quantidade_original = Decimal(item_pedido.quantidade or "0")
            quantidade_restante = max(quantidade_original - quantidade_vendida, Decimal("0.000"))
            vendidos_por_produto[item_pedido.produto_id] = max(
                quantidade_vendida - quantidade_original,
                Decimal("0.000"),
            )
            item_pedido.quantidade = quantidade_restante.quantize(Decimal("0.001"))
            item_pedido.valor_total = (
                item_pedido.quantidade * item_pedido.preco_unitario
            ).quantize(Decimal("0.01"))
            item_pedido.save(update_fields=["quantidade", "valor_total"])

        total_pendente += Decimal(item_pedido.valor_total or "0")

    pedido.total = total_pendente.quantize(Decimal("0.01"))
    pedido.save(update_fields=["total", "atualizado_em"])


def _devolver_estoque_item_removido(item_removido):
    if item_removido.estoque_devolvido or not item_removido.produto_id:
        return False
    _devolver_estoque_produto(
        item_removido.produto_id,
        item_removido.quantidade_snapshot,
        item_removido.produto_nome_snapshot,
        item_removido.unidade_snapshot,
    )
    item_removido.estoque_devolvido = True
    item_removido.estoque_devolvido_em = timezone.now()
    item_removido.save(update_fields=["estoque_devolvido", "estoque_devolvido_em"])
    return True


def _devolver_estoque_cancelamento_venda(venda):
    if venda.estoque_devolvido_cancelamento:
        return ""

    devolvidos = []
    for item in ItemVenda.objects.select_related("produto").filter(venda=venda).order_by("id"):
        if not item.produto_id:
            continue
        produto_nome = item.produto.nome if item.produto else "Produto nao identificado"
        _devolver_estoque_produto(item.produto_id, item.quantidade, produto_nome, item.unidade)
        devolvidos.append(f"{produto_nome}: {_formatar_quantidade(item.quantidade)} {item.unidade or ''}".strip())

    venda.estoque_devolvido_cancelamento = True
    venda.estoque_devolvido_cancelamento_em = timezone.now()
    venda.save(update_fields=[
        "estoque_devolvido_cancelamento",
        "estoque_devolvido_cancelamento_em",
        "atualizado_em",
    ])
    if not devolvidos:
        return "Estoque: nenhum item ativo com produto vinculado para devolver."
    return "Estoque devolvido dos itens ativos: " + "; ".join(devolvidos) + "."


@require_POST
def gravar_venda(request):
    try:
        dados = json.loads(request.body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse(
            {"sucesso": False, "mensagem": "Nao foi possivel ler os dados da venda."},
            status=400,
        )

    itens = dados.get("itens") or []
    if not itens:
        return JsonResponse(
            {"sucesso": False, "mensagem": "Inclua pelo menos 1 item antes de gravar a venda."},
            status=400,
        )

    data_venda = parse_date(dados.get("data_venda") or "")
    if not data_venda:
        return JsonResponse(
            {"sucesso": False, "mensagem": "Informe uma data valida para a venda."},
            status=400,
        )

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
            return JsonResponse(
                {"sucesso": False, "mensagem": "Cliente selecionado nao foi encontrado."},
                status=400,
            )

    itens_validados = []
    total_calculado = Decimal("0.00")

    for item in itens:
        produto_nome = str(item.get("produto_nome") or "").strip()
        produto_id = str(item.get("produto_id") or "").strip()
        unidade = str(item.get("unidade") or "").strip()

        if not produto_nome:
            return JsonResponse(
                {"sucesso": False, "mensagem": "Existe item sem produto informado."},
                status=400,
            )

        try:
            quantidade = _decimal_do_front(item.get("quantidade"), "0.001")
            preco_unitario = _decimal_do_front(item.get("preco_unitario"), "0.01")
        except ValueError:
            return JsonResponse(
                {"sucesso": False, "mensagem": f"Revise quantidade e preco do item {produto_nome}."},
                status=400,
            )

        if quantidade <= 0 or preco_unitario <= 0:
            return JsonResponse(
                {"sucesso": False, "mensagem": f"Quantidade e preco precisam ser maiores que zero em {produto_nome}."},
                status=400,
            )

        valor_total = (quantidade * preco_unitario).quantize(Decimal("0.01"))
        produto = None
        if produto_id.isdigit():
            produto = Produto.objects.filter(pk=int(produto_id), excluido=False).first()
        if not produto:
            produto = Produto.objects.filter(nome__iexact=produto_nome, excluido=False).first()
        if not produto:
            return JsonResponse(
                {"sucesso": False, "mensagem": f'Produto "{produto_nome}" nao foi encontrado no estoque.'},
                status=400,
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
            "produto": produto,
            "quantidade": quantidade,
            "unidade": unidade,
            "preco_unitario": preco_unitario,
            "valor_total": valor_total,
        })

    tipo_pagamento_venda = str(dados.get("tipo_pagamento") or "").strip()
    valores_origem_venda = None
    if _venda_pagamento_imediato(tipo_pagamento_venda):
        try:
            valores_origem_venda = _valores_origem_venda_post(dados)
            _validar_origem_venda_a_vista(valores_origem_venda, total_calculado.quantize(Decimal("0.01")))
        except ValueError as exc:
            return JsonResponse({"sucesso": False, "mensagem": str(exc)}, status=400)

    pedido_pendencias_estoque = []
    try:
        with transaction.atomic():
            pedido_origem = None
            pedido_id = dados.get("pedido_id")
            if pedido_id:
                from .models import Pedido

                pedido_origem = Pedido.objects.select_for_update().filter(pk=pedido_id).first()
                if not pedido_origem:
                    return JsonResponse(
                        {"sucesso": False, "mensagem": "Pedido de origem nao foi encontrado."},
                        status=400,
                    )
                if pedido_origem.status not in [Pedido.STATUS_ABERTO, Pedido.STATUS_PARCIAL]:
                    return JsonResponse(
                        {"sucesso": False, "mensagem": "Pedido de origem nao esta aberto nem parcial."},
                        status=400,
                    )

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
                        })
                        total_venda += valor_total_vendido
                        produto_bloqueado.quantidade = (
                            estoque_disponivel - quantidade_vendida_base
                        ).quantize(Decimal("0.001"))
                        Produto.objects.filter(pk=produto_bloqueado.pk).update(
                            quantidade=produto_bloqueado.quantidade,
                            atualizado_em=timezone.now(),
                        )

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
                    return JsonResponse(
                        {
                            "sucesso": False,
                            "mensagem": (
                                f"Nenhum item do Pedido #{pedido_origem.id} possui estoque disponivel "
                                "para gerar venda. Os itens continuam pendentes no pedido."
                            ),
                            "toast_duracao_ms": 12000,
                        },
                        status=400,
                    )
            else:
                _baixar_estoque_movimentos(
                    (item["produto"], item["quantidade"], item["unidade"])
                    for item in itens_para_venda
                )

            venda = Venda.objects.create(
                cliente=cliente,
                data_venda=data_venda,
                data_vencimento=data_vencimento,
                tipo_pagamento=tipo_pagamento_venda,
                operador=str(dados.get("operador") or "").strip(),
                total=total_venda.quantize(Decimal("0.01")),
            )

            ItemVenda.objects.bulk_create([
                ItemVenda(venda=venda, **item)
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
        return JsonResponse({"sucesso": False, "mensagem": str(exc)}, status=400)

    mensagem = f"Venda #{venda.id} gravada com sucesso."
    if pedido_pendencias_estoque:
        mensagem = (
            f"Venda #{venda.id} gravada com itens disponiveis. "
            "Alguns itens ficaram pendentes por falta de estoque: "
            + "; ".join(pedido_pendencias_estoque)
            + "."
        )

    return JsonResponse({
        "sucesso": True,
        "mensagem": mensagem,
        "venda_id": venda.id,
        "visualizar_url": reverse("estoque:venda_detalhe", args=[venda.id]),
    })


@ensure_csrf_cookie
def venda_detalhe(request, pk):
    venda = get_object_or_404(
        Venda.objects.select_related("cliente").prefetch_related("itens__produto", "eventos"),
        pk=pk,
    )
    retorno_url = _url_retorno_segura(request)
    whatsapp_url = montar_link_whatsapp_venda(venda)
    contexto_pedido_parcial = _contexto_venda_pedido_parcial(venda)
    entrega_contexto = None
    entrega_id = request.GET.get("entrega")
    if entrega_id:
        entrega_contexto = EntregaRota.objects.filter(pk=entrega_id).first()
    itens_adicionados_ids = _ids_itens_adicionados_param(request)
    itens_adicionados_param = ",".join(str(item_id) for item_id in itens_adicionados_ids)
    itens_nota = sorted(
        list(venda.itens.all()),
        key=lambda item: ((item.produto.nome if item.produto else "Produto nao identificado").casefold(), item.id),
    )
    whatsapp_atualizacao = None if venda.cancelada else _montar_whatsapp_atualizacao_nota(request, venda)
    conta_receber = _conta_receber_da_venda(venda)
    alteracoes_pendentes_whatsapp = _resumo_alteracoes_pendentes_whatsapp(
        venda,
        itens_nota,
        incluir_edicoes_registradas=request.GET.get("nota_atualizada") == "1",
    )
    itens_adicionados_destacar_ids = set(itens_adicionados_ids) | alteracoes_pendentes_whatsapp["itens_adicionados_ids"]
    contexto_pendencia_resolvida = contexto_pendencia_resolvida_nota(request, venda)
    modo_pendencia_resolvida = contexto_pendencia_resolvida is not None
    contexto_venda_quitada = _contexto_venda_quitada(venda, conta_receber)
    ajustes_itens_quitados = list(
        AjusteItemVendaQuitada.objects.filter(venda=venda)
        .exclude(status=AjusteItemVendaQuitada.STATUS_CANCELADO)
        .select_related("item_venda", "produto")
        .order_by("item_venda_id", "-criado_em", "-id")
    )
    item_ids_ajustados = {
        ajuste.item_venda_id
        for ajuste in ajustes_itens_quitados
        if ajuste.item_venda_id
    }
    itens_nota_principais = [
        item
        for item in itens_nota
        if item.id not in item_ids_ajustados
    ]
    ajustes_itens_quitados_para_total = []
    ajustes_total_chaves = set()
    for ajuste in ajustes_itens_quitados:
        chave_total = (
            ("produto", ajuste.produto_id)
            if ajuste.produto_id
            else (ajuste.item_venda_id or ("produto_nome", ajuste.produto_nome_snapshot))
        )
        if chave_total in ajustes_total_chaves:
            continue
        ajustes_total_chaves.add(chave_total)
        ajustes_itens_quitados_para_total.append(ajuste)

    total_ajustes_itens_quitados = sum(
        (
            ajuste.diferenca_financeira or Decimal("0.00")
            for ajuste in ajustes_itens_quitados_para_total
        ),
        Decimal("0.00"),
    ).quantize(Decimal("0.01"))
    total_ajustado_entregue = max(
        ((venda.total or Decimal("0.00")) - total_ajustes_itens_quitados).quantize(Decimal("0.01")),
        Decimal("0.00"),
    )
    ajustes_itens_quitados_pendentes = [
        ajuste
        for ajuste in ajustes_itens_quitados
        if ajuste.status in {
            AjusteItemVendaQuitada.STATUS_RASCUNHO,
            AjusteItemVendaQuitada.STATUS_PENDENTE,
        }
        or ajuste.resolucao_financeira == AjusteItemVendaQuitada.RESOLUCAO_NAO_DEFINIDA
    ]

    # Verificar se venda já tem entrega/rota
    itens_removidos = list(
        ItemVendaRemovido.objects.filter(venda=venda)
        .select_related("produto", "credito_gerado", "ajuste_origem", "venda__cliente")
    )
    for item_removido in itens_removidos:
        item_removido.contexto_desfazer = _contexto_desfazer_item_removido(item_removido)
    remocoes_por_ajuste = {
        item_removido.ajuste_origem_id: item_removido
        for item_removido in itens_removidos
        if item_removido.ajuste_origem_id
    }
    for ajuste in ajustes_itens_quitados:
        ajuste.remocao_reversao = remocoes_por_ajuste.get(ajuste.id)
        if ajuste.remocao_reversao:
            ajuste.contexto_desfazer = ajuste.remocao_reversao.contexto_desfazer
        else:
            ajuste.contexto_desfazer = {
                "permitido": False,
                "motivo": "Este ajuste foi criado antes do controle de reversão automática. Não é possível desfazer automaticamente.",
            }

    entrega_existente = EntregaRotaItem.objects.filter(venda=venda).select_related("rota").first()
    entrega_info = None
    if entrega_existente:
        rota = entrega_existente.rota
        checklist_url = montar_checklist_url(request, rota.id)
        entrega_info = {
            "rota": rota,
            "checklist_url": checklist_url,
            "status": rota.get_status_display(),
        }

    _registrar_evento_venda(
        venda,
        "nota_visualizada",
        "Nota visualizada.",
        canal="sistema",
    )
    return render(
        request,
        "estoque/venda_detalhe.html",
        {
            "venda": venda,
            "whatsapp_url": whatsapp_url,
            "eventos": EventoVenda.objects.filter(venda=venda),
            "comunicacoes_whatsapp": EventoVenda.objects.filter(
                venda=venda,
                canal__in=["whatsapp", "whatsapp_checklist"],
                tipo_evento__in=[
                    "whatsapp_aberto",
                    "whatsapp_confirmado",
                    "checklist_cliente_whatsapp_aberto",
                    "checklist_cliente_enviado",
                ],
            ),
            "entrega_contexto": entrega_contexto,
            "entrega_info": entrega_info,
            "funcionarios_habilitados": Funcionario.habilitados_para_checklist(),
            "itens_adicionados_ids": itens_adicionados_ids,
            "itens_adicionados_destacar_ids": itens_adicionados_destacar_ids,
            "itens_adicionados_param": itens_adicionados_param,
            "itens_nota": itens_nota,
            "itens_nota_principais": itens_nota_principais,
            "ajustes_itens_quitados": ajustes_itens_quitados,
            "itens_removidos": itens_removidos,
            "total_ajustes_itens_quitados": total_ajustes_itens_quitados,
            "total_ajustado_entregue": total_ajustado_entregue,
            "whatsapp_atualizacao": whatsapp_atualizacao,
            "alteracoes_pendentes_whatsapp": alteracoes_pendentes_whatsapp,
            "conta_receber": conta_receber,
            "contexto_venda_quitada": contexto_venda_quitada,
            "ajustes_itens_quitados_pendentes": ajustes_itens_quitados_pendentes,
            "venda_a_prazo": _venda_a_prazo(venda),
            "venda_a_vista": _venda_pagamento_imediato(venda.tipo_pagamento),
            "alocacao_financeira_venda": _alocacao_financeira_venda(venda),
            "movimentos_financeiros_venda": _movimentos_financeiros_venda(venda),
            "retorno_url": retorno_url,
            "retorno_querystring": _querystring_retorno(retorno_url),
            "modo_pendencia_resolvida": modo_pendencia_resolvida,
            "contexto_pendencia_resolvida": contexto_pendencia_resolvida,
            "contexto_pedido_parcial": contexto_pedido_parcial,
        },
    )


def venda_cliente_detalhe(request, pk):
    venda = get_object_or_404(
        Venda.objects.select_related("cliente").prefetch_related("itens__produto"),
        pk=pk,
    )
    return render(
        request,
        "estoque/venda_cliente_detalhe.html",
        {"venda": venda},
    )


def _bloquear_venda_cancelada(request, venda, destino="estoque:venda_detalhe"):
    if not getattr(venda, "cancelada", False):
        return None
    messages.warning(request, "Venda cancelada / venda nao realizada. Esta acao esta bloqueada.")
    return redirect(destino, pk=venda.pk)


def _bloquear_edicao_venda_quitada(request, venda, permitir_conta_parcial=False):
    contexto = _contexto_venda_quitada(venda)
    conta = _conta_receber_da_venda(venda)
    if (
        permitir_conta_parcial
        and conta
        and conta.status == ContaReceber.STATUS_PARCIAL
        and not contexto.get("venda_a_vista")
        and not contexto.get("conta_paga")
    ):
        return None
    if not contexto.get("quitada"):
        return None
    messages.warning(
        request,
        "Venda quitada: edicao comum bloqueada. Para produto nao entregue/nao aceito, use futuramente o fluxo proprio de ajuste financeiro/estoque.",
    )
    detalhe_url = reverse("estoque:venda_detalhe", kwargs={"pk": venda.pk})
    return redirect(f"{detalhe_url}?edicao_bloqueada=1")


def _venda_a_prazo(venda):
    return normalizar_texto_cliente(venda.tipo_pagamento) in {"a prazo", "carteira"}


def _conta_receber_da_venda(venda):
    try:
        return venda.conta_receber
    except ContaReceber.DoesNotExist:
        return None


def _credito_disponivel_cliente(cliente):
    if not cliente:
        return Decimal("0.00")
    total = (
        CreditoCliente.objects.filter(cliente=cliente)
        .aggregate(total=Sum("valor"))
        .get("total")
        or Decimal("0.00")
    ).quantize(Decimal("0.01"))
    return max(total, Decimal("0.00"))


def _item_removido_credito_foi_usado(item_removido):
    credito = item_removido.credito_gerado
    if not credito:
        return False
    valor_credito = (credito.valor or Decimal("0.00")).quantize(Decimal("0.01"))
    if valor_credito <= Decimal("0.00"):
        return False
    return _credito_disponivel_cliente(item_removido.venda.cliente) < valor_credito


def _contexto_desfazer_item_removido(item_removido):
    if item_removido.status != ItemVendaRemovido.STATUS_REMOVIDO:
        return {
            "permitido": False,
            "motivo": "Esta remoção já foi desfeita.",
        }
    if _item_removido_credito_foi_usado(item_removido):
        return {
            "permitido": False,
            "motivo": "Este crédito já foi usado e não pode ser desfeito automaticamente nesta etapa.",
        }
    return {"permitido": True, "motivo": ""}


def _contexto_venda_quitada(venda, conta_receber=None):
    conta = conta_receber if conta_receber is not None else _conta_receber_da_venda(venda)
    recebimentos_count = conta.recebimentos.count() if conta else 0
    conta_paga = bool(conta and conta.status == ContaReceber.STATUS_PAGA)
    venda_a_vista = normalizar_texto_cliente(venda.tipo_pagamento) in {"a vista", "avista"}
    motivos = []
    if venda_a_vista:
        motivos.append("Venda a vista.")
    if conta_paga:
        motivos.append("Conta a receber quitada.")
    if recebimentos_count:
        motivos.append(f"{recebimentos_count} recebimento(s)/baixa(s) registrado(s).")
    return {
        "quitada": venda_a_vista or conta_paga or recebimentos_count > 0,
        "venda_a_vista": venda_a_vista,
        "conta_paga": conta_paga,
        "recebimentos_count": recebimentos_count,
        "motivos": motivos,
        "resumo": " ".join(motivos),
    }


def criar_ajuste_item_venda_quitada(
    venda,
    item_venda,
    motivo,
    observacao="",
    operador="",
):
    if item_venda.venda_id != venda.id:
        raise ValueError("O item informado nao pertence a venda do ajuste.")
    if not _contexto_venda_quitada(venda).get("quitada"):
        raise ValueError("Ajuste de item quitado permitido apenas para venda quitada.")

    produto = item_venda.produto
    produto_nome = produto.nome if produto else "Produto nao identificado"
    valor_total = (item_venda.valor_total or Decimal("0.00")).quantize(Decimal("0.01"))
    return AjusteItemVendaQuitada.objects.create(
        venda=venda,
        item_venda=item_venda,
        cliente=venda.cliente,
        produto=produto,
        produto_nome_snapshot=produto_nome,
        quantidade_snapshot=item_venda.quantidade,
        unidade_snapshot=item_venda.unidade or "",
        preco_unitario_snapshot=item_venda.preco_unitario,
        valor_total_snapshot=valor_total,
        motivo=motivo,
        observacao=observacao,
        diferenca_financeira=valor_total,
        resolucao_financeira=AjusteItemVendaQuitada.RESOLUCAO_NAO_DEFINIDA,
        status=AjusteItemVendaQuitada.STATUS_PENDENTE,
        operador=operador or venda.operador or "",
    )


def venda_ajuste_item_quitado(request, pk):
    venda = get_object_or_404(
        Venda.objects.select_related("cliente").prefetch_related("itens__produto"),
        pk=pk,
    )
    bloqueio = _bloquear_venda_cancelada(request, venda)
    if bloqueio:
        return bloqueio

    contexto_venda_quitada = _contexto_venda_quitada(venda)
    detalhe_url = reverse("estoque:venda_detalhe", kwargs={"pk": venda.pk})
    if not contexto_venda_quitada.get("quitada"):
        messages.warning(request, "Este fluxo e permitido apenas para venda quitada/paga.")
        return redirect(f"{detalhe_url}?ajuste_bloqueado=1")

    itens_nota = sorted(
        list(venda.itens.all()),
        key=lambda item: ((item.produto.nome if item.produto else "Produto nao identificado").casefold(), item.id),
    )
    ajustes_ativos = AjusteItemVendaQuitada.objects.filter(venda=venda).exclude(
        status=AjusteItemVendaQuitada.STATUS_CANCELADO
    )
    ajustes_pendentes_item_ids = set(ajustes_ativos.values_list("item_venda_id", flat=True))
    ajustes_ativos_produto_ids = {
        produto_id
        for produto_id in ajustes_ativos.values_list("produto_id", flat=True)
        if produto_id
    }
    motivos = AjusteItemVendaQuitada.MOTIVO_CHOICES
    valores = {
        "item_id": "",
        "motivo": AjusteItemVendaQuitada.MOTIVO_ITEM_NAO_ENTREGUE,
        "observacao": "",
    }

    if request.method == "POST":
        valores["item_id"] = request.POST.get("item_id", "").strip()
        valores["motivo"] = request.POST.get("motivo", "").strip()
        valores["observacao"] = request.POST.get("observacao", "").strip()
        item_venda = ItemVenda.objects.filter(pk=valores["item_id"]).select_related("produto").first()
        motivo_valido = valores["motivo"] in dict(motivos)

        if not item_venda or item_venda.venda_id != venda.id:
            messages.warning(request, "Selecione um item valido desta venda.")
        elif not motivo_valido:
            messages.warning(request, "Selecione um motivo valido para o ajuste.")
        elif valores["motivo"] == AjusteItemVendaQuitada.MOTIVO_OUTRO and not valores["observacao"]:
            messages.warning(request, "Informe uma observacao quando o motivo for outro.")
        elif item_venda.id in ajustes_pendentes_item_ids or (
            item_venda.produto_id and item_venda.produto_id in ajustes_ativos_produto_ids
        ):
            messages.warning(
                request,
                "Este item já possui ajuste registrado nesta venda. Desfaça ou resolva o ajuste existente antes de registrar outro.",
            )
        else:
            with transaction.atomic():
                ajuste = criar_ajuste_item_venda_quitada(
                    venda,
                    item_venda,
                    valores["motivo"],
                    observacao=valores["observacao"],
                )
                ItemVendaRemovido.objects.create(
                    venda=venda,
                    produto=ajuste.produto,
                    produto_nome_snapshot=ajuste.produto_nome_snapshot,
                    quantidade_snapshot=ajuste.quantidade_snapshot,
                    unidade_snapshot=ajuste.unidade_snapshot or "",
                    preco_unitario_snapshot=ajuste.preco_unitario_snapshot,
                    valor_total_snapshot=ajuste.valor_total_snapshot,
                    item_venda_original_id=item_venda.id,
                    ajuste_origem=ajuste,
                    operador=ajuste.operador or venda.operador or "",
                    observacao="Ajuste de item nao entregue/nao aceito registrado com controle de reversao.",
                )
                motivo_texto = dict(AjusteItemVendaQuitada.MOTIVO_CHOICES).get(
                    ajuste.motivo,
                    ajuste.motivo,
                )
                quantidade_texto = _formatar_quantidade(ajuste.quantidade_snapshot)
                unidade_texto = (ajuste.unidade_snapshot or "").strip()
                quantidade_unidade = f"{quantidade_texto} {unidade_texto}".strip()
                _registrar_evento_venda(
                    venda,
                    "ajuste_item_quitado_registrado",
                    (
                        f"Ajuste de item em venda quitada registrado: {ajuste.produto_nome_snapshot}, "
                        f"quantidade {quantidade_unidade}, valor {_formatar_moeda(ajuste.valor_total_snapshot)}. "
                        f"Motivo: {motivo_texto}. Resolucao financeira pendente; estoque, conta e recebimentos nao foram alterados."
                    ),
                    canal="sistema",
                    usuario=ajuste.operador,
                )

            messages.success(request, "Ajuste registrado com resolucao financeira pendente.")
            return redirect(f"{detalhe_url}?ajuste_registrado=1")

    return render(
        request,
        "estoque/venda_ajuste_item_quitado.html",
        {
            "venda": venda,
            "itens_nota": itens_nota,
            "motivos": motivos,
            "valores": valores,
            "contexto_venda_quitada": contexto_venda_quitada,
            "ajustes_pendentes_item_ids": ajustes_pendentes_item_ids,
        },
    )


def venda_ajuste_item_quitado_credito(request, pk, ajuste_id):
    venda = get_object_or_404(
        Venda.objects.select_related("cliente"),
        pk=pk,
    )
    detalhe_url = reverse("estoque:venda_detalhe", kwargs={"pk": venda.pk})
    ajuste = get_object_or_404(
        AjusteItemVendaQuitada.objects.select_related("item_venda", "produto", "cliente", "venda"),
        pk=ajuste_id,
        venda=venda,
    )

    if not venda.cliente_id:
        messages.warning(request, "Nao e possivel gerar credito sem cliente vinculado a venda.")
        return redirect(f"{detalhe_url}?credito_bloqueado=1")

    if (
        ajuste.status == AjusteItemVendaQuitada.STATUS_RESOLVIDO
        or ajuste.resolucao_financeira != AjusteItemVendaQuitada.RESOLUCAO_NAO_DEFINIDA
    ):
        messages.warning(request, "Este ajuste ja foi resolvido.")
        return redirect(f"{detalhe_url}?credito_bloqueado=1")

    confirmacao_credito = ""
    ciencia_credito = ""
    if request.method == "POST":
        confirmacao_credito = request.POST.get("confirmacao_credito", "")
        ciencia_credito = request.POST.get("ciencia_credito", "")
        if confirmacao_credito.strip().upper() != "CREDITO":
            messages.warning(request, "Digite CREDITO exatamente para confirmar a geracao do credito.")
        elif ciencia_credito != "1":
            messages.warning(request, "Marque a ciencia de que item, venda, conta e recebimentos serao preservados.")
        else:
            with transaction.atomic():
                ajuste_locked = (
                    AjusteItemVendaQuitada.objects.select_for_update()
                    .get(pk=ajuste.pk, venda_id=venda.pk)
                )
                if (
                    ajuste_locked.status == AjusteItemVendaQuitada.STATUS_RESOLVIDO
                    or ajuste_locked.resolucao_financeira != AjusteItemVendaQuitada.RESOLUCAO_NAO_DEFINIDA
                ):
                    messages.warning(request, "Este ajuste ja foi resolvido.")
                    return redirect(f"{detalhe_url}?credito_bloqueado=1")

                valor_credito = (ajuste_locked.diferenca_financeira or Decimal("0.00")).quantize(Decimal("0.01"))
                conta_receber = _conta_receber_da_venda(venda)
                credito = CreditoCliente.objects.create(
                    cliente=venda.cliente,
                    valor=valor_credito,
                    tipo=CreditoCliente.TIPO_CREDITO_GERADO,
                    origem_conta_receber=conta_receber,
                    observacao=(
                        f"Credito gerado pela resolucao do ajuste #{ajuste_locked.id} "
                        f"da venda #{venda.id}: {ajuste_locked.produto_nome_snapshot}."
                    ),
                )
                ajuste_locked.resolucao_financeira = AjusteItemVendaQuitada.RESOLUCAO_CREDITO_CLIENTE
                ajuste_locked.status = AjusteItemVendaQuitada.STATUS_RESOLVIDO
                ajuste_locked.save(update_fields=["resolucao_financeira", "status", "atualizado_em"])
                ItemVendaRemovido.objects.filter(
                    ajuste_origem=ajuste_locked,
                    status=ItemVendaRemovido.STATUS_REMOVIDO,
                    credito_gerado__isnull=True,
                ).update(credito_gerado=credito)
                quantidade_texto = _formatar_quantidade(ajuste_locked.quantidade_snapshot)
                unidade_texto = (ajuste_locked.unidade_snapshot or "").strip()
                quantidade_unidade = f"{quantidade_texto} {unidade_texto}".strip()
                _registrar_evento_venda(
                    venda,
                    "ajuste_item_quitado_resolvido_credito",
                    (
                        f"Ajuste de item em venda quitada resolvido como credito do cliente: "
                        f"{ajuste_locked.produto_nome_snapshot}, quantidade {quantidade_unidade}, "
                        f"credito gerado {_formatar_moeda(valor_credito)} (credito #{credito.id}). "
                        f"Item, venda, conta a receber, recebimentos e estoque foram preservados."
                    ),
                    canal="sistema",
                    usuario=ajuste_locked.operador or venda.operador,
                )

            messages.success(request, "Credito do cliente gerado e ajuste marcado como resolvido.")
            return redirect(f"{detalhe_url}?credito_resolvido=1")

    return render(
        request,
        "estoque/venda_ajuste_item_quitado_credito.html",
        {
            "venda": venda,
            "ajuste": ajuste,
            "confirmacao_credito": confirmacao_credito,
            "ciencia_credito": ciencia_credito,
        },
    )


def _sincronizar_conta_receber(venda, observacao_origem="", permitir_reabrir_cancelada=False):
    conta = _conta_receber_da_venda(venda)
    origem = f" Origem: {observacao_origem}." if observacao_origem else ""

    if venda.cancelada:
        if conta and conta.status == ContaReceber.STATUS_ABERTA:
            conta.status = ContaReceber.STATUS_CANCELADA
            conta.valor_em_aberto = Decimal("0.00")
            conta.observacao = f"Cancelada por venda nao realizada.{origem}".strip()
            conta.save(update_fields=["status", "valor_em_aberto", "observacao", "atualizado_em"])
        return conta

    if not _venda_a_prazo(venda):
        if conta and conta.status == ContaReceber.STATUS_ABERTA:
            conta.status = ContaReceber.STATUS_CANCELADA
            conta.valor_em_aberto = Decimal("0.00")
            conta.observacao = f"Cancelada porque a venda esta marcada como a vista.{origem}".strip()
            conta.save(update_fields=["status", "valor_em_aberto", "observacao", "atualizado_em"])
        return conta

    valor = (venda.total or Decimal("0.00")).quantize(Decimal("0.01"))
    if conta is None:
        return ContaReceber.objects.create(
            venda=venda,
            cliente=venda.cliente,
            data_emissao=venda.data_venda,
            data_vencimento=venda.data_vencimento,
            valor_original=valor,
            valor_em_aberto=valor,
            status=ContaReceber.STATUS_ABERTA,
            observacao=f"Criada automaticamente para venda a prazo.{origem}".strip(),
        )

    if conta.status == ContaReceber.STATUS_CANCELADA and not permitir_reabrir_cancelada:
        return conta

    if conta.status in {
        ContaReceber.STATUS_ABERTA,
        ContaReceber.STATUS_PARCIAL,
        ContaReceber.STATUS_PAGA,
        ContaReceber.STATUS_CANCELADA,
    }:
        valor_original_anterior = (conta.valor_original or Decimal("0.00")).quantize(Decimal("0.01"))
        valor_aberto_anterior = (conta.valor_em_aberto or Decimal("0.00")).quantize(Decimal("0.01"))
        valor_ja_recebido = max(
            (valor_original_anterior - valor_aberto_anterior).quantize(Decimal("0.01")),
            Decimal("0.00"),
        )
        valor_recebido_registrado = (
            conta.recebimentos.aggregate(total=Sum("valor")).get("total")
            or Decimal("0.00")
        ).quantize(Decimal("0.01"))
        valor_ja_recebido = max(valor_ja_recebido, valor_recebido_registrado)
        novo_valor_aberto = max((valor - valor_ja_recebido).quantize(Decimal("0.01")), Decimal("0.00"))
        if novo_valor_aberto == Decimal("0.00"):
            novo_status = ContaReceber.STATUS_PAGA
        elif valor_ja_recebido > Decimal("0.00"):
            novo_status = ContaReceber.STATUS_PARCIAL
        else:
            novo_status = ContaReceber.STATUS_ABERTA

        ajuste_extra = ""
        if valor_ja_recebido > valor:
            ajuste_extra = (
                f" Valor ja recebido ({_formatar_moeda(valor_ja_recebido)}) "
                f"maior que o novo total da venda ({_formatar_moeda(valor)}); saldo mantido zerado."
            )

        conta.cliente = venda.cliente
        conta.data_emissao = venda.data_venda
        conta.data_vencimento = venda.data_vencimento
        conta.valor_original = valor
        conta.valor_em_aberto = novo_valor_aberto
        conta.status = novo_status
        conta.observacao = (
            f"Sincronizada automaticamente com venda a prazo.{origem} "
            f"Valor ja recebido preservado: {_formatar_moeda(valor_ja_recebido)}. "
            f"Saldo anterior: {_formatar_moeda(valor_aberto_anterior)}; "
            f"novo saldo: {_formatar_moeda(novo_valor_aberto)}."
            f"{ajuste_extra}"
        ).strip()
        conta.save(update_fields=[
            "cliente",
            "data_emissao",
            "data_vencimento",
            "valor_original",
            "valor_em_aberto",
            "status",
            "observacao",
            "atualizado_em",
        ])

    return conta


TIPOS_EVENTO_EDICAO_NOTA_WHATSAPP = (
    "cabecalho_nota_alterado",
    "quantidade_item_alterada",
    "item_removido_da_nota",
    "produto_adicionado_na_nota",
    "item_adicionado_na_nota",
)

TIPOS_EVENTO_CORTE_WHATSAPP_NOTA = (
    "whatsapp_aberto",
    "whatsapp_confirmado",
)


def _status_whatsapp_consulta_venda(venda, eventos=None):
    eventos_lista = list(eventos if eventos is not None else venda.eventos.all())
    eventos_lista.sort(key=lambda evento: (evento.criado_em, evento.id))
    confirmacoes = [
        evento
        for evento in eventos_lista
        if evento.canal == "whatsapp" and evento.tipo_evento == "whatsapp_confirmado"
    ]

    if not confirmacoes:
        classe = "aberto" if venda.whatsapp_status == Venda.WHATSAPP_ABERTO else ""
        return [{
            "texto": venda.whatsapp_status_texto,
            "classe": classe,
        }]

    selos = [{
        "texto": "Nota enviada por WhatsApp",
        "classe": "enviado-confirmado",
    }]

    primeira_confirmacao = confirmacoes[0]
    reenvios_apos_edicao = 0
    edicao_pendente = False
    for evento in eventos_lista:
        if (evento.criado_em, evento.id) <= (primeira_confirmacao.criado_em, primeira_confirmacao.id):
            continue
        if evento.tipo_evento in TIPOS_EVENTO_EDICAO_NOTA_WHATSAPP:
            edicao_pendente = True
            continue
        if evento.canal == "whatsapp" and evento.tipo_evento == "whatsapp_confirmado" and edicao_pendente:
            reenvios_apos_edicao += 1
            edicao_pendente = False

    if reenvios_apos_edicao == 1:
        selos.append({
            "texto": "Editada e reenviada por WhatsApp",
            "classe": "editada-reenviada",
        })
    elif reenvios_apos_edicao > 1:
        selos.append({
            "texto": f"Editada e reenviada {reenvios_apos_edicao} vezes",
            "classe": "editada-reenviada",
        })

    if edicao_pendente:
        selos.append({
            "texto": (
                "Nova edicao ainda nao reenviada"
                if reenvios_apos_edicao
                else "Editada, mas ainda nao reenviada"
            ),
            "classe": "editada-pendente",
        })

    return selos


def _eventos_edicao_nota_para_whatsapp(venda):
    eventos = list(
        EventoVenda.objects.filter(
            venda=venda,
        )
        .filter(Q(tipo_evento__in=TIPOS_EVENTO_EDICAO_NOTA_WHATSAPP) | Q(canal="whatsapp", tipo_evento="whatsapp_confirmado"))
        .order_by("criado_em", "id")
    )
    if not any(evento.canal == "whatsapp" and evento.tipo_evento == "whatsapp_confirmado" for evento in eventos):
        return EventoVenda.objects.none()

    pendentes_ids = []
    for evento in eventos:
        if evento.canal == "whatsapp" and evento.tipo_evento == "whatsapp_confirmado":
            pendentes_ids = []
            continue
        if evento.tipo_evento in TIPOS_EVENTO_EDICAO_NOTA_WHATSAPP:
            pendentes_ids.append(evento.id)

    if not pendentes_ids:
        return EventoVenda.objects.none()

    return EventoVenda.objects.filter(pk__in=pendentes_ids).order_by("criado_em", "id")


def _produto_evento_por_nome(itens_nota, nome_produto):
    nome_normalizado = (nome_produto or "").strip().casefold()
    if not nome_normalizado:
        return None
    for item in itens_nota:
        nome_item = item.produto.nome if item.produto else "Produto nao identificado"
        if nome_item.strip().casefold() == nome_normalizado:
            return item
    return None


def _linha_resumo_visual_alteracao_item(evento):
    descricao = (evento.descricao or "").strip()

    if evento.tipo_evento == "quantidade_item_alterada":
        return ""

    if evento.tipo_evento == "item_removido_da_nota":
        prefixo = "Item removido da nota: "
        produto = ""
        if descricao.startswith(prefixo):
            produto = descricao[len(prefixo):].split(", quantidade ", 1)[0].strip()
        return f"Item removido: {produto or descricao or 'Item removido'}"

    if evento.tipo_evento in ("produto_adicionado_na_nota", "item_adicionado_na_nota"):
        prefixo = "Item adicionado na nota: "
        produto = descricao[len(prefixo):].split(", quantidade ", 1)[0].strip() if descricao.startswith(prefixo) else ""
        return f"Produto acrescentado: {produto or descricao or 'Produto acrescentado'}"

    return ""


def _resumo_alteracoes_pendentes_whatsapp(venda, itens_nota, incluir_edicoes_registradas=False):
    eventos = list(_eventos_edicao_nota_para_whatsapp(venda))
    if incluir_edicoes_registradas and not eventos:
        eventos = list(
            EventoVenda.objects.filter(
                venda=venda,
                tipo_evento__in=TIPOS_EVENTO_EDICAO_NOTA_WHATSAPP,
            ).order_by("criado_em", "id")
        )
    resumo = {
        "itens_adicionados_ids": set(),
        "itens_quantidade_ids": set(),
        "quantidades": {},
        "removidos": [],
        "eventos": eventos,
        "linhas": [],
    }

    for evento in eventos:
        descricao = (evento.descricao or "").strip()
        linha_visual = _linha_resumo_visual_alteracao_item(evento)
        if linha_visual:
            resumo["linhas"].append({
                "evento_id": evento.id,
                "tipo_evento": evento.tipo_evento,
                "linhas": [linha_visual],
            })

        if evento.tipo_evento in ("produto_adicionado_na_nota", "item_adicionado_na_nota"):
            prefixo = "Item adicionado na nota: "
            produto_nome = descricao[len(prefixo):].split(", quantidade ", 1)[0].strip() if descricao.startswith(prefixo) else ""
            item = _produto_evento_por_nome(itens_nota, produto_nome)
            if item:
                resumo["itens_adicionados_ids"].add(item.id)
            continue

        if evento.tipo_evento == "quantidade_item_alterada":
            prefixo = "Quantidade alterada na nota: "
            produto_nome = ""
            texto_quantidade = ""
            if descricao.startswith(prefixo) and ". De " in descricao:
                produto_nome, resto = descricao[len(prefixo):].split(". De ", 1)
                texto_quantidade = resto.split(". Total", 1)[0].strip()
            item = _produto_evento_por_nome(itens_nota, produto_nome)
            if item:
                resumo["itens_quantidade_ids"].add(item.id)
                if texto_quantidade:
                    resumo["quantidades"][item.id] = texto_quantidade
            continue

        if evento.tipo_evento == "item_removido_da_nota":
            prefixo = "Item removido da nota: "
            produto_nome = descricao[len(prefixo):].split(", quantidade ", 1)[0].strip() if descricao.startswith(prefixo) else ""
            resumo["removidos"].append(produto_nome or descricao or "Item removido")

    return resumo


def _linhas_evento_edicao_nota(evento):
    descricao = (evento.descricao or "").strip()
    if evento.tipo_evento == "quantidade_item_alterada":
        prefixo = "Quantidade alterada na nota: "
        if descricao.startswith(prefixo) and ". De " in descricao and ". Total" in descricao:
            produto, resto = descricao[len(prefixo):].split(". De ", 1)
            quantidades = resto.split(". Total", 1)[0]
            if " para " in quantidades:
                quantidade_anterior, nova_quantidade = quantidades.split(" para ", 1)
                return [
                    f'O item "{produto.strip()}" teve a quantidade alterada:',
                    f"Quantidade anterior: {quantidade_anterior.strip()}",
                    f"Nova quantidade: {nova_quantidade.strip()}",
                ]
        return [descricao or "Quantidade de item alterada na nota."]

    if evento.tipo_evento == "item_removido_da_nota":
        prefixo = "Item removido da nota: "
        if descricao.startswith(prefixo):
            detalhe = descricao[len(prefixo):]
            produto = detalhe.split(", quantidade ", 1)[0].strip()
            if produto:
                return [f'O item "{produto}" foi removido da nota.']
        return [descricao or "Item removido da nota."]

    if evento.tipo_evento in ("produto_adicionado_na_nota", "item_adicionado_na_nota"):
        return [descricao or "Item adicionado na nota."]

    if evento.tipo_evento == "cabecalho_nota_alterado":
        return [descricao or "Cabecalho da nota alterado."]

    return [descricao] if descricao else []


def _montar_whatsapp_atualizacao_nota(request, venda):
    eventos = list(_eventos_edicao_nota_para_whatsapp(venda))
    if not eventos:
        return None

    ultimo_evento = eventos[-1]
    whatsapp_aberto = EventoVenda.objects.filter(
        Q(criado_em__gt=ultimo_evento.criado_em) | Q(criado_em=ultimo_evento.criado_em, id__gt=ultimo_evento.id),
        venda=venda,
        canal="whatsapp",
        tipo_evento="whatsapp_aberto",
    ).exists()
    numero_whatsapp = _numero_whatsapp_cadastro_venda(venda)
    cliente_nome = venda.cliente.nome if venda.cliente else "Cliente"
    nota_path = reverse("estoque:venda_cliente_detalhe", kwargs={"pk": venda.pk})
    nota_url = montar_url_publica(request, nota_path)

    linhas_mensagem = [
        f"Ola, {cliente_nome}.",
        "",
        "Sua nota foi atualizada.",
        "",
    ]
    for indice, evento in enumerate(eventos):
        if indice:
            linhas_mensagem.append("")
        linhas_mensagem.extend(_linhas_evento_edicao_nota(evento))

    linhas_mensagem.extend([
        "",
        f"Novo total da nota: {_formatar_moeda(venda.total or Decimal('0.00'))}",
        "",
        "Segue a nota atualizada para conferencia:",
        nota_url,
    ])
    mensagem_whatsapp = "\n".join(linhas_mensagem)
    quantidade_eventos = len(eventos)
    texto_resumo = (
        "Nota atualizada com 1 alteracao desde o ultimo WhatsApp."
        if quantidade_eventos == 1
        else f"Nota atualizada com {quantidade_eventos} alteracoes desde o ultimo WhatsApp."
    )

    return {
        "tem_whatsapp": bool(numero_whatsapp),
        "url": (
            f"https://web.whatsapp.com/send?phone={numero_whatsapp}&text={quote(mensagem_whatsapp)}"
            if numero_whatsapp
            else ""
        ),
        "texto_resumo": texto_resumo,
        "controle_id": f"{venda.pk}-{'-'.join(str(evento.id) for evento in eventos)}",
        "whatsapp_aberto": whatsapp_aberto,
    }


def venda_editar_revisao(request, pk):
    venda = get_object_or_404(
        Venda.objects.select_related("cliente").prefetch_related("itens__produto", "eventos"),
        pk=pk,
    )
    bloqueio = _bloquear_venda_cancelada(request, venda)
    if bloqueio:
        return bloqueio
    bloqueio = _bloquear_edicao_venda_quitada(request, venda)
    if bloqueio:
        return bloqueio
    retorno_url = _url_retorno_segura(request)
    alteracao_quantidade = request.session.pop(f"venda_quantidade_alterada_{venda.pk}", None)
    item_removido = request.session.pop(f"venda_item_removido_{venda.pk}", None)

    entregas = (
        EntregaRotaItem.objects.filter(venda=venda)
        .select_related("rota")
        .prefetch_related("checklist_itens")
        .order_by("-rota__data", "-rota_id", "ordem_entrega", "id")
    )
    total_entregas = entregas.count()
    existe_checklist = EntregaChecklistItem.objects.filter(rota_item__venda=venda).exists()
    total_checklists = EntregaChecklistItem.objects.filter(rota_item__venda=venda).count()
    existe_pendencia = EntregaRotaItem.objects.filter(venda=venda, is_pendencia=True).exists()
    whatsapp_atualizacao = _montar_whatsapp_atualizacao_nota(request, venda)
    contexto_venda_quitada = _contexto_venda_quitada(venda)

    return render(
        request,
        "estoque/venda_editar_revisao.html",
        {
            "venda": venda,
            "eventos": EventoVenda.objects.filter(venda=venda),
            "entregas": entregas,
            "total_entregas": total_entregas,
            "existe_checklist": existe_checklist,
            "total_checklists": total_checklists,
            "existe_pendencia": existe_pendencia,
            "possui_alerta_operacional": total_entregas or existe_checklist or existe_pendencia,
            "contexto_venda_quitada": contexto_venda_quitada,
            "alteracao_quantidade": alteracao_quantidade,
            "whatsapp_atualizacao": whatsapp_atualizacao,
            "retorno_url": retorno_url,
            "retorno_querystring": _querystring_retorno(retorno_url),
        },
    )


def venda_editar_cabecalho(request, pk):
    venda = get_object_or_404(
        Venda.objects.select_related("cliente").prefetch_related("itens__produto"),
        pk=pk,
    )
    bloqueio = _bloquear_venda_cancelada(request, venda)
    if bloqueio:
        return bloqueio
    bloqueio = _bloquear_edicao_venda_quitada(request, venda)
    if bloqueio:
        return bloqueio
    retorno_url = _url_retorno_segura(request)
    opcoes_pagamento = ("A prazo", "À vista")
    possui_alerta_operacional = (
        EntregaRotaItem.objects.filter(venda=venda).exists()
        or EntregaChecklistItem.objects.filter(rota_item__venda=venda).exists()
        or EntregaRotaItem.objects.filter(venda=venda, is_pendencia=True).exists()
    )
    whatsapp_atualizacao = _montar_whatsapp_atualizacao_nota(request, venda)

    valores = {
        "data_venda": venda.data_venda.isoformat() if venda.data_venda else "",
        "data_vencimento": venda.data_vencimento.isoformat() if venda.data_vencimento else "",
        "tipo_pagamento": venda.tipo_pagamento if venda.tipo_pagamento in opcoes_pagamento else "A prazo",
    }

    if request.method == "POST":
        valores = {
            "data_venda": request.POST.get("data_venda", "").strip(),
            "data_vencimento": request.POST.get("data_vencimento", "").strip(),
            "tipo_pagamento": request.POST.get("tipo_pagamento", "").strip(),
        }
        nova_data_venda = parse_date(valores["data_venda"])
        novo_vencimento = parse_date(valores["data_vencimento"]) if valores["data_vencimento"] else None
        novo_pagamento = valores["tipo_pagamento"]

        if not nova_data_venda:
            messages.warning(request, "Informe uma data da venda valida.")
        elif novo_pagamento not in opcoes_pagamento:
            messages.warning(request, "Selecione uma forma de pagamento valida.")
        else:
            data_anterior = venda.data_venda
            vencimento_anterior = venda.data_vencimento
            pagamento_anterior = venda.tipo_pagamento or "-"
            houve_alteracao = (
                data_anterior != nova_data_venda
                or vencimento_anterior != novo_vencimento
                or pagamento_anterior != novo_pagamento
            )

            if not houve_alteracao:
                messages.warning(request, "Nenhuma alteracao de cabecalho foi informada.")
            else:
                with transaction.atomic():
                    venda.data_venda = nova_data_venda
                    venda.data_vencimento = novo_vencimento
                    venda.tipo_pagamento = novo_pagamento
                    venda.save(update_fields=["data_venda", "data_vencimento", "tipo_pagamento", "atualizado_em"])
                    _registrar_evento_venda(
                        venda,
                        "cabecalho_nota_alterado",
                        (
                            "Cabecalho da nota alterado. "
                            f"Data: {data_anterior.strftime('%d/%m/%Y') if data_anterior else '-'} -> {nova_data_venda.strftime('%d/%m/%Y')}. "
                            f"Vencimento: {vencimento_anterior.strftime('%d/%m/%Y') if vencimento_anterior else '-'} -> {novo_vencimento.strftime('%d/%m/%Y') if novo_vencimento else '-'}. "
                            f"Pagamento: {pagamento_anterior} -> {novo_pagamento}. "
                            "Cliente, itens, financeiro, caixa, entregas e checklist nao foram alterados."
                        ),
                        canal="sistema",
                        usuario=venda.operador,
                    )
                    _sincronizar_conta_receber(
                        venda,
                        "cabecalho da nota alterado",
                        permitir_reabrir_cancelada=True,
                    )

                messages.success(
                    request,
                    "Cabecalho da nota atualizado. O impacto financeiro real sera tratado em etapa futura, se necessario.",
                )
                revisao_url = reverse("estoque:venda_editar_revisao", kwargs={"pk": venda.pk})
                return redirect(_url_com_retorno(revisao_url, retorno_url))

    return render(
        request,
        "estoque/venda_editar_cabecalho.html",
        {
            "venda": venda,
            "valores": valores,
            "opcoes_pagamento": opcoes_pagamento,
            "possui_alerta_operacional": possui_alerta_operacional,
            "whatsapp_atualizacao": whatsapp_atualizacao,
            "retorno_url": retorno_url,
            "retorno_querystring": _querystring_retorno(retorno_url),
        },
    )


def venda_editar_quantidade_item(request, pk, item_id):
    venda = get_object_or_404(
        Venda.objects.select_related("cliente").prefetch_related("itens__produto"),
        pk=pk,
    )
    bloqueio = _bloquear_venda_cancelada(request, venda)
    if bloqueio:
        return bloqueio
    bloqueio = _bloquear_edicao_venda_quitada(request, venda)
    if bloqueio:
        return bloqueio
    retorno_url = _url_retorno_segura(request)
    item_venda = get_object_or_404(
        ItemVenda.objects.select_related("produto", "venda"),
        pk=item_id,
        venda=venda,
    )
    total_atual_venda = venda.total or Decimal("0.00")
    valor_atual_item = item_venda.valor_total or Decimal("0.00")
    quantidade_preview = None
    novo_valor_item = None
    novo_total_venda = None
    erro_quantidade = ""

    def calcular_previsao(valor_quantidade):
        nova_quantidade = _decimal_do_front(valor_quantidade, "0.001")
        if nova_quantidade <= 0:
            raise ValueError("Informe uma quantidade maior que zero.")

        novo_total_item = (nova_quantidade * item_venda.preco_unitario).quantize(Decimal("0.01"))
        total_previsto = (total_atual_venda - valor_atual_item + novo_total_item).quantize(Decimal("0.01"))
        return nova_quantidade, novo_total_item, total_previsto

    if request.method == "POST":
        quantidade_postada = request.POST.get("nova_quantidade")
        try:
            nova_quantidade, novo_total_item, total_previsto = calcular_previsao(quantidade_postada)
        except ValueError as exc:
            messages.warning(request, str(exc))
            quantidade_url = reverse(
                "estoque:venda_editar_quantidade_item",
                kwargs={"pk": venda.pk, "item_id": item_venda.pk},
            )
            return redirect(_url_com_retorno(quantidade_url, retorno_url))

        quantidade_anterior = item_venda.quantidade
        valor_anterior_item = valor_atual_item
        total_anterior = total_atual_venda
        produto_nome = item_venda.produto.nome if item_venda.produto else "Produto nao identificado"

        with transaction.atomic():
            item_venda.quantidade = nova_quantidade
            item_venda.valor_total = novo_total_item
            item_venda.save(update_fields=["quantidade", "valor_total"])
            venda.total = total_previsto
            venda.save(update_fields=["total", "atualizado_em"])
            novo_total_confirmado = total_previsto
            _registrar_evento_venda(
                venda,
                "quantidade_item_alterada",
                (
                    f"Quantidade alterada na nota: {produto_nome}. "
                    f"De {quantidade_anterior} {item_venda.unidade} para {nova_quantidade} {item_venda.unidade}. "
                    f"Total da venda: R$ {total_anterior} -> R$ {novo_total_confirmado}."
                ),
                canal="sistema",
                usuario=venda.operador,
            )
            _sincronizar_conta_receber(venda, "quantidade de item alterada")

        messages.success(request, f"Quantidade de {produto_nome} atualizada com sucesso.")
        request.session[f"venda_quantidade_alterada_{venda.pk}"] = {
            "item_id": item_venda.pk,
            "produto_nome": produto_nome,
            "unidade": item_venda.unidade,
            "quantidade_anterior": str(quantidade_anterior),
            "nova_quantidade": str(nova_quantidade),
            "valor_anterior_item": str(valor_anterior_item),
            "novo_valor_item": str(novo_total_item),
            "total_anterior_venda": str(total_anterior),
            "novo_total_venda": str(novo_total_confirmado),
        }
        revisao_url = reverse("estoque:venda_editar_revisao", kwargs={"pk": venda.pk})
        return redirect(_url_com_retorno(revisao_url, retorno_url))

    quantidade_informada = request.GET.get("nova_quantidade", "").strip()
    if quantidade_informada:
        try:
            quantidade_preview, novo_valor_item, novo_total_venda = calcular_previsao(quantidade_informada)
        except ValueError as exc:
            erro_quantidade = str(exc)

    possui_alerta_operacional = (
        EntregaRotaItem.objects.filter(venda=venda).exists()
        or EntregaChecklistItem.objects.filter(rota_item__venda=venda).exists()
        or EntregaRotaItem.objects.filter(venda=venda, is_pendencia=True).exists()
    )

    return render(
        request,
        "estoque/venda_editar_quantidade_item.html",
        {
            "venda": venda,
            "item_venda": item_venda,
            "produto_nome": item_venda.produto.nome if item_venda.produto else "Produto nao identificado",
            "total_atual_venda": total_atual_venda,
            "valor_atual_item": valor_atual_item,
            "quantidade_informada": quantidade_informada,
            "quantidade_preview": quantidade_preview,
            "novo_valor_item": novo_valor_item,
            "novo_total_venda": novo_total_venda,
            "erro_quantidade": erro_quantidade,
            "possui_alerta_operacional": possui_alerta_operacional,
            "retorno_url": retorno_url,
            "retorno_querystring": _querystring_retorno(retorno_url),
        },
    )


def venda_adicionar_produto_item(request, pk):
    venda = get_object_or_404(
        Venda.objects.select_related("cliente").prefetch_related("itens__produto"),
        pk=pk,
    )
    bloqueio = _bloquear_venda_cancelada(request, venda)
    if bloqueio:
        return bloqueio
    bloqueio = _bloquear_edicao_venda_quitada(request, venda, permitir_conta_parcial=True)
    if bloqueio:
        return bloqueio
    retorno_url = _url_retorno_segura(request)
    produtos = Produto.objects.filter(excluido=False).order_by("nome")
    total_atual_venda = venda.total or Decimal("0.00")
    produto_selecionado = None
    quantidade_preview = None
    preco_unitario = None
    unidade = ""
    valor_novo_item = None
    novo_total_venda = None
    erro_adicao = ""
    mensagem_produto_duplicado = "Este produto já existe na nota. Use Alterar quantidade no item existente."
    itens_adicionados_ids = _ids_itens_adicionados_param(request)
    itens_adicionados_param = ",".join(str(item_id) for item_id in itens_adicionados_ids)

    if request.method == "POST":
        produto_bloqueio_id = request.POST.get("produto_id")
        produto_bloqueio = (
            Produto.objects
            .filter(pk=produto_bloqueio_id, excluido=False)
            .first()
            if produto_bloqueio_id
            else None
        )
        if produto_bloqueio:
            try:
                estoque_bloqueio = Decimal(str(getattr(produto_bloqueio, "quantidade", 0) or 0))
            except Exception:
                estoque_bloqueio = Decimal("0.00")
            if estoque_bloqueio <= Decimal("0.00"):
                messages.error(
                    request,
                    f"Produto com estoque zerado: {produto_bloqueio.nome}. A nota nao foi alterada.",
                )
                return redirect("estoque:venda_adicionar_produto_item", pk=venda.id)

    def obter_produto(produto_id):
        try:
            return produtos.get(pk=produto_id)
        except (Produto.DoesNotExist, ValueError, TypeError):
            raise ValueError("Selecione um produto cadastrado.")

    def preco_produto(produto):
        preco = produto.preco_venda or produto.preco_vista or Decimal("0.00")
        return Decimal(preco).quantize(Decimal("0.01"))

    def unidade_produto(produto):
        return produto.unidade_venda_1 or produto.unidade_compra or ""

    def validar_produto_novo_na_venda(produto):
        if ItemVenda.objects.filter(venda=venda, produto_id=produto.pk).exists():
            raise ValueError(mensagem_produto_duplicado)

    def calcular_previsao(produto_id, valor_quantidade, valor_preco=None):
        produto = obter_produto(produto_id)
        validar_produto_novo_na_venda(produto)
        quantidade = _decimal_do_front(valor_quantidade, "0.001")
        if quantidade <= 0:
            raise ValueError("Informe uma quantidade maior que zero.")

        preco = _decimal_do_front(valor_preco, "0.01") if str(valor_preco or "").strip() else preco_produto(produto)
        if preco <= 0:
            raise ValueError("Informe um preco unitario maior que zero.")

        total_item = (quantidade * preco).quantize(Decimal("0.01"))
        total_previsto = (total_atual_venda + total_item).quantize(Decimal("0.01"))
        return produto, quantidade, preco, unidade_produto(produto), total_item, total_previsto

    if request.method == "POST":
        produto_id = request.POST.get("produto_id")
        quantidade_informada = request.POST.get("quantidade", "").strip()
        preco_informado = request.POST.get("preco_unitario", "").strip()
        try:
            produto_selecionado, quantidade_preview, preco_unitario, unidade, valor_novo_item, total_previsto = calcular_previsao(
                produto_id,
                quantidade_informada,
                preco_informado,
            )
        except ValueError as exc:
            erro_adicao = str(exc)
            try:
                produto_selecionado = obter_produto(produto_id)
                preco_unitario = _decimal_do_front(preco_informado, "0.01") if preco_informado else preco_produto(produto_selecionado)
                unidade = unidade_produto(produto_selecionado)
            except ValueError:
                pass
        else:
            total_anterior = total_atual_venda
            produto_nome = produto_selecionado.nome

            try:
                with transaction.atomic():
                    validar_produto_novo_na_venda(produto_selecionado)
                    _baixar_estoque_produto(
                        produto_selecionado.pk,
                        quantidade_preview,
                        produto_nome,
                    )
                    item_criado = ItemVenda.objects.create(
                        venda=venda,
                        produto=produto_selecionado,
                        quantidade=quantidade_preview,
                        unidade=unidade,
                        preco_unitario=preco_unitario,
                        valor_total=valor_novo_item,
                    )
                    total_recalculado = _recalcular_total_venda_pelos_itens(venda)
                    venda.total = total_recalculado
                    venda.save(update_fields=["total", "atualizado_em"])
                    _registrar_evento_venda(
                        venda,
                        "item_adicionado_na_nota",
                        (
                            f"Item adicionado na nota: {produto_nome}, quantidade {quantidade_preview} {unidade or ''}, "
                            f"preco unitario R$ {preco_unitario}, valor acrescentado R$ {valor_novo_item}. "
                            f"Total da venda: R$ {total_anterior} -> R$ {total_recalculado}. "
                            "Estoque baixado do produto adicionado."
                        ),
                        canal="sistema",
                        usuario=venda.operador,
                    )
                    _sincronizar_conta_receber(venda, "item adicionado na nota")
            except ValueError as exc:
                erro_adicao = str(exc)
                quantidade_preview = None
                valor_novo_item = None
                novo_total_venda = None
            else:
                messages.success(request, f'Produto "{produto_nome}" acrescentado a nota com sucesso.')
                ids_destacados = list(dict.fromkeys([*itens_adicionados_ids, item_criado.pk]))
                detalhe_url = (
                    f"{reverse('estoque:venda_detalhe', kwargs={'pk': venda.pk})}"
                    f"?nota_atualizada=1&itens_adicionados={','.join(str(item_id) for item_id in ids_destacados)}"
                )
                return redirect(_url_com_retorno(detalhe_url, retorno_url))
    else:
        produto_id = request.GET.get("produto_id", "").strip()
        quantidade_informada = request.GET.get("quantidade", "").strip()
        preco_informado = request.GET.get("preco_unitario", "").strip()

    if request.method != "POST" and produto_id:
        try:
            produto_selecionado = obter_produto(produto_id)
            preco_unitario = _decimal_do_front(preco_informado, "0.01") if preco_informado else preco_produto(produto_selecionado)
            unidade = unidade_produto(produto_selecionado)
            if quantidade_informada:
                produto_selecionado, quantidade_preview, preco_unitario, unidade, valor_novo_item, novo_total_venda = calcular_previsao(
                    produto_id,
                    quantidade_informada,
                    preco_informado,
                )
        except ValueError as exc:
            erro_adicao = str(exc)

    possui_alerta_operacional = (
        EntregaRotaItem.objects.filter(venda=venda).exists()
        or EntregaChecklistItem.objects.filter(rota_item__venda=venda).exists()
        or EntregaRotaItem.objects.filter(venda=venda, is_pendencia=True).exists()
    )
    itens_venda_ordenados = sorted(
        list(venda.itens.all()),
        key=lambda item: ((item.produto.nome if item.produto else "Produto nao identificado").casefold(), item.id),
    )
    produtos_existentes_ids = [
        item.produto_id
        for item in itens_venda_ordenados
        if item.produto_id
    ]
    produto_opcoes = [
        {
            "id": produto.id,
            "nome": produto.nome,
            "preco": str(preco_produto(produto)),
            "unidade": unidade_produto(produto),
            "estoque": str(getattr(produto, "quantidade", Decimal("0.00")) or Decimal("0.00")),
        }
        for produto in produtos
    ]

    return render(
        request,
        "estoque/venda_adicionar_produto_item.html",
        {
            "venda": venda,
            "produtos": produtos,
            "produto_opcoes": produto_opcoes,
            "produto_id": produto_id,
            "produto_selecionado": produto_selecionado,
            "quantidade_informada": quantidade_informada,
            "preco_informado": preco_informado or (str(preco_unitario) if preco_unitario is not None else ""),
            "quantidade_preview": quantidade_preview,
            "preco_unitario": preco_unitario,
            "unidade": unidade,
            "valor_novo_item": valor_novo_item,
            "total_atual_venda": total_atual_venda,
            "novo_total_venda": novo_total_venda,
            "erro_adicao": erro_adicao,
            "possui_alerta_operacional": possui_alerta_operacional,
            "itens_adicionados_param": itens_adicionados_param,
            "itens_venda_ordenados": itens_venda_ordenados,
            "produtos_existentes_ids": produtos_existentes_ids,
            "retorno_url": retorno_url,
            "retorno_querystring": _querystring_retorno(retorno_url),
        },
    )


def venda_revisar_remocao_item(request, pk, item_id):
    venda = get_object_or_404(
        Venda.objects.select_related("cliente").prefetch_related("itens__produto"),
        pk=pk,
    )
    bloqueio = _bloquear_venda_cancelada(request, venda)
    if bloqueio:
        return bloqueio
    retorno_url = _url_retorno_segura(request)
    item_venda = get_object_or_404(
        ItemVenda.objects.select_related("produto", "venda"),
        pk=item_id,
        venda=venda,
    )

    total_atual_venda = venda.total or Decimal("0.00")
    valor_atual_item = item_venda.valor_total or Decimal("0.00")
    novo_total_venda = max(
        (total_atual_venda - valor_atual_item).quantize(Decimal("0.01")),
        Decimal("0.00"),
    )
    total_itens_venda = ItemVenda.objects.filter(venda=venda).count()
    produto_nome = item_venda.produto.nome if item_venda.produto else "Produto nao identificado"
    possui_alerta_operacional = (
        EntregaRotaItem.objects.filter(venda=venda).exists()
        or EntregaChecklistItem.objects.filter(rota_item__venda=venda).exists()
        or EntregaRotaItem.objects.filter(venda=venda, is_pendencia=True).exists()
    )
    total_checklists_item = EntregaChecklistItem.objects.filter(item_venda=item_venda).count()

    if request.method == "POST":
        quantidade_removida = item_venda.quantidade
        unidade_removida = item_venda.unidade
        valor_abatido = valor_atual_item
        total_anterior = total_atual_venda
        pendencias_abertas_item = [
            pendencia
            for pendencia in listar_pendencias_entrega()
            if pendencia.get("item_venda_id") == item_venda.id
            and pendencia.get("venda")
            and pendencia["venda"].id == venda.id
        ]

        with transaction.atomic():
            rota_item_ids_afetados = list(
                EntregaChecklistItem.objects.filter(item_venda=item_venda)
                .values_list("rota_item_id", flat=True)
            )
            item_removido = ItemVendaRemovido.objects.create(
                venda=venda,
                produto=item_venda.produto,
                produto_nome_snapshot=produto_nome,
                quantidade_snapshot=quantidade_removida,
                unidade_snapshot=unidade_removida or "",
                preco_unitario_snapshot=item_venda.preco_unitario,
                valor_total_snapshot=valor_abatido,
                item_venda_original_id=item_venda.id,
                operador=venda.operador or "",
                observacao="Item removido da nota por edicao.",
            )
            estoque_devolvido = _devolver_estoque_item_removido(item_removido)
            item_venda.delete()
            resolver_entregas_sem_pendencias_ativas(rota_item_ids_afetados)
            total_recalculado = sum(
                (
                    valor or Decimal("0.00")
                    for valor in ItemVenda.objects.filter(venda=venda).values_list("valor_total", flat=True)
                ),
                Decimal("0.00"),
            )
            total_recalculado = total_recalculado.quantize(Decimal("0.01"))
            venda.total = total_recalculado
            venda.save(update_fields=["total", "atualizado_em"])
            _anular_venda_sem_itens_por_remocao_pendencia(venda)
            _registrar_evento_venda(
                venda,
                "item_removido_da_nota",
                (
                    f"Item removido da nota: {produto_nome}, quantidade {quantidade_removida} {unidade_removida or ''}, "
                    f"valor abatido R$ {valor_abatido}. Registro de remocao #{item_removido.id}. "
                    f"Total da venda: R$ {total_anterior} -> R$ {total_recalculado}. "
                    f"{'Estoque devolvido para o produto removido.' if estoque_devolvido else 'Sem devolucao de estoque para este item.'}"
                ),
                canal="sistema",
                usuario=venda.operador,
            )
            for pendencia in pendencias_abertas_item:
                rota = pendencia.get("rota")
                rota_id = rota.id if rota else ""
                evento_existente = (
                    EventoVenda.objects.filter(
                        venda=venda,
                        tipo_evento="pendencia_removida_da_nota",
                    )
                    .filter(descricao__icontains=f"rota #{rota_id}")
                    .filter(descricao__icontains=f"Item removido: {produto_nome}")
                    .exists()
                )
                if evento_existente:
                    continue

                _registrar_evento_venda(
                    venda,
                    "pendencia_removida_da_nota",
                    descricao_pendencia_removida_da_nota(
                        rota_id,
                        produto_nome,
                        quantidade_removida,
                        unidade_removida,
                        valor_abatido,
                        total_anterior,
                        total_recalculado,
                        origem="edicao da nota",
                    ),
                    canal="sistema",
                    usuario=venda.operador,
                )
            _sincronizar_conta_receber(venda, "item removido da nota")

        messages.success(request, f'Item "{produto_nome}" removido da nota com sucesso.')
        request.session[f"venda_item_removido_{venda.pk}"] = {
            "produto_nome": produto_nome,
            "novo_total_venda": str(total_recalculado),
        }
        revisao_url = reverse("estoque:venda_editar_revisao", kwargs={"pk": venda.pk})
        return redirect(_url_com_retorno(revisao_url, retorno_url))

    return render(
        request,
        "estoque/venda_revisar_remocao_item.html",
        {
            "venda": venda,
            "item_venda": item_venda,
            "produto_nome": produto_nome,
            "total_atual_venda": total_atual_venda,
            "valor_atual_item": valor_atual_item,
            "novo_total_venda": novo_total_venda,
            "valor_abatido": valor_atual_item,
            "total_itens_venda": total_itens_venda,
            "nota_ficara_zerada": total_itens_venda == 1,
            "possui_alerta_operacional": possui_alerta_operacional,
            "total_checklists_item": total_checklists_item,
            "retorno_url": retorno_url,
            "retorno_querystring": _querystring_retorno(retorno_url),
        },
    )


def venda_desfazer_remocao_item(request, pk, remocao_id):
    venda = get_object_or_404(
        Venda.objects.select_related("cliente").prefetch_related("itens__produto"),
        pk=pk,
    )
    bloqueio = _bloquear_venda_cancelada(request, venda)
    if bloqueio:
        return bloqueio
    retorno_url = _url_retorno_segura(request)
    item_removido = get_object_or_404(
        ItemVendaRemovido.objects.select_related(
            "venda",
            "produto",
            "credito_gerado",
            "ajuste_origem",
            "venda__cliente",
        ),
        pk=remocao_id,
        venda=venda,
    )
    contexto_desfazer = _contexto_desfazer_item_removido(item_removido)
    detalhe_url = reverse("estoque:venda_detalhe", kwargs={"pk": venda.pk})

    if request.method == "POST":
        confirmacao = request.POST.get("confirmacao_desfazer", "").strip().upper()
        if not contexto_desfazer["permitido"]:
            messages.warning(request, contexto_desfazer["motivo"])
            return redirect(f"{detalhe_url}?desfazer_bloqueado=1#itens-removidos")
        if confirmacao != "DESFAZER":
            messages.warning(request, 'Digite "DESFAZER" para confirmar a reversao da remocao.')
        else:
            try:
                with transaction.atomic():
                    item_removido_locked = (
                        ItemVendaRemovido.objects.select_for_update()
                        .get(pk=item_removido.pk, venda=venda)
                    )
                    contexto_locked = _contexto_desfazer_item_removido(item_removido_locked)
                    if not contexto_locked["permitido"]:
                        messages.warning(request, contexto_locked["motivo"])
                        return redirect(f"{detalhe_url}?desfazer_bloqueado=1#itens-removidos")

                    credito_cancelado = Decimal("0.00")
                    total_anterior = (venda.total or Decimal("0.00")).quantize(Decimal("0.01"))
                    ajuste_cancelado = item_removido_locked.ajuste_origem
                    estoque_baixado = False
                    if not ajuste_cancelado and item_removido_locked.produto_id:
                        _baixar_estoque_produto(
                            item_removido_locked.produto_id,
                            item_removido_locked.quantidade_snapshot,
                            item_removido_locked.produto_nome_snapshot,
                        )
                        estoque_baixado = True

                    if item_removido_locked.credito_gerado_id:
                        credito_original = item_removido_locked.credito_gerado
                        credito_cancelado = (credito_original.valor or Decimal("0.00")).quantize(Decimal("0.01"))
                        if credito_cancelado > Decimal("0.00"):
                            CreditoCliente.objects.create(
                                cliente=venda.cliente,
                                valor=-credito_cancelado,
                                tipo=CreditoCliente.TIPO_CREDITO_GERADO,
                                origem_conta_receber=credito_original.origem_conta_receber,
                                origem_recebimento=credito_original.origem_recebimento,
                                observacao=(
                                    f"Credito cancelado por reversao da remocao #{item_removido_locked.id} "
                                    f"da venda #{venda.id}. Credito original #{credito_original.id}."
                                ),
                            )

                    if ajuste_cancelado:
                        ajuste_cancelado.status = AjusteItemVendaQuitada.STATUS_CANCELADO
                        ajuste_cancelado.resolucao_financeira = AjusteItemVendaQuitada.RESOLUCAO_NAO_DEFINIDA
                        ajuste_cancelado.observacao = (
                            f"{ajuste_cancelado.observacao}\n"
                            "Ajuste desfeito por reversao da remocao registrada."
                        ).strip()
                        ajuste_cancelado.save(
                            update_fields=[
                                "status",
                                "resolucao_financeira",
                                "observacao",
                                "atualizado_em",
                            ]
                        )
                    else:
                        ItemVenda.objects.create(
                            venda=venda,
                            produto=item_removido_locked.produto,
                            quantidade=item_removido_locked.quantidade_snapshot,
                            unidade=item_removido_locked.unidade_snapshot,
                            preco_unitario=item_removido_locked.preco_unitario_snapshot,
                            valor_total=item_removido_locked.valor_total_snapshot,
                        )
                    total_recalculado = _recalcular_total_venda_pelos_itens(venda)
                    venda.total = total_recalculado
                    venda.save(update_fields=["total", "atualizado_em"])
                    _sincronizar_conta_receber(venda, "reversao de remocao de item")
                    item_removido_locked.status = ItemVendaRemovido.STATUS_REVERTIDO
                    item_removido_locked.revertido_em = timezone.now()
                    item_removido_locked.estoque_devolvido = False
                    item_removido_locked.estoque_devolvido_em = None
                    item_removido_locked.observacao = (
                        f"{item_removido_locked.observacao}\n"
                        f"Reversao feita em {timezone.localtime(item_removido_locked.revertido_em).strftime('%d/%m/%Y %H:%M')}."
                    ).strip()
                    item_removido_locked.save(update_fields=[
                        "status",
                        "revertido_em",
                        "estoque_devolvido",
                        "estoque_devolvido_em",
                        "observacao",
                    ])
                    _registrar_evento_venda(
                        venda,
                        "remocao_item_desfeita",
                        (
                            f"Remocao de item desfeita: {item_removido_locked.produto_nome_snapshot}, "
                            f"quantidade {item_removido_locked.quantidade_snapshot} {item_removido_locked.unidade_snapshot or ''}, "
                            f"valor R$ {item_removido_locked.valor_total_snapshot}. "
                            f"Total da venda: R$ {total_anterior} -> R$ {total_recalculado}. "
                            f"Credito cancelado: R$ {credito_cancelado}. "
                            f"{'Ajuste financeiro cancelado.' if ajuste_cancelado else 'Item recolocado na nota.'} "
                            f"{'Estoque baixado novamente.' if estoque_baixado else 'Sem baixa de estoque nesta reversao.'}"
                        ),
                        canal="sistema",
                        usuario=venda.operador,
                    )
            except ValueError as exc:
                messages.warning(request, str(exc))
                return redirect(f"{detalhe_url}?desfazer_bloqueado=1#itens-removidos")
            messages.success(request, "Remocao do item desfeita com sucesso.")
            return redirect(f"{detalhe_url}?remocao_desfeita=1#dados-da-nota")

    return render(
        request,
        "estoque/venda_desfazer_remocao_item.html",
        {
            "venda": venda,
            "item_removido": item_removido,
            "contexto_desfazer": contexto_desfazer,
            "retorno_url": retorno_url or detalhe_url,
        },
    )


def venda_cancelar(request, pk):
    venda = get_object_or_404(
        Venda.objects.select_related("cliente").prefetch_related("itens__produto"),
        pk=pk,
    )
    retorno_url = _url_retorno_segura(request)
    detalhe_url = reverse("estoque:venda_detalhe", kwargs={"pk": venda.pk})
    if venda.cancelada:
        messages.warning(request, "Esta venda ja esta cancelada.")
        return redirect(_url_com_retorno(detalhe_url, retorno_url))

    conta_receber = _conta_receber_da_venda(venda)
    recebimentos_count = conta_receber.recebimentos.count() if conta_receber else 0
    contexto_venda_quitada = _contexto_venda_quitada(venda, conta_receber)
    creditos_count = (
        CreditoCliente.objects.filter(origem_conta_receber=conta_receber).count()
        if conta_receber
        else 0
    )
    entregas_count = EntregaRotaItem.objects.filter(venda=venda).count()
    eventos_count = EventoVenda.objects.filter(venda=venda).count()
    venda_antiga = bool(venda.data_venda and venda.data_venda != timezone.localdate())
    sinais_consolidacao = []
    if conta_receber:
        sinais_consolidacao.append(
            f"Conta a receber #{conta_receber.id} vinculada ({conta_receber.get_status_display()})."
        )
    if recebimentos_count:
        sinais_consolidacao.append(f"{recebimentos_count} recebimento(s)/baixa(s) registrado(s).")
    if creditos_count:
        sinais_consolidacao.append(f"{creditos_count} movimento(s) de credito vinculado(s).")
    if venda_antiga:
        sinais_consolidacao.append("Venda de data anterior ao dia de hoje.")
    if entregas_count:
        sinais_consolidacao.append(f"{entregas_count} vinculo(s) de entrega/checklist encontrado(s).")
    if eventos_count:
        sinais_consolidacao.append(f"{eventos_count} registro(s) no historico da venda.")

    itens_nota = sorted(
        list(venda.itens.all()),
        key=lambda item: ((item.produto.nome if item.produto else "Produto nao identificado").casefold(), item.id),
    )
    motivos_cancelamento = (
        "Cliente desistiu da compra",
        "Pedido duplicado",
        "Venda lançada por engano / cliente errado",
        "Outro motivo",
    )
    motivo = ""
    motivo_padrao = ""
    observacao_cancelamento = ""
    confirmacao_cancelamento = ""
    ciencia_cancelamento = ""
    destino_financeiro = ""
    destinos_financeiros_recebimento = {
        "credito_cliente": "gerar credito para o cliente",
        "devolucao_manual": "registrar devolucao manual ao cliente",
        "pendencia_financeira": "deixar como pendencia financeira",
    }

    if request.method == "POST":
        motivo_padrao = request.POST.get("motivo_padrao", "").strip()
        observacao_cancelamento = request.POST.get("observacao_cancelamento", "").strip()
        confirmacao_cancelamento = request.POST.get("confirmacao_cancelamento", "")
        ciencia_cancelamento = request.POST.get("ciencia_cancelamento", "")
        destino_financeiro = request.POST.get("destino_financeiro", "").strip()
        confirmacao_normalizada = confirmacao_cancelamento.strip().upper()
        total_recebido = (
            conta_receber.recebimentos.aggregate(total=Sum("valor")).get("total")
            if conta_receber
            else Decimal("0.00")
        ) or Decimal("0.00")
        total_recebido = total_recebido.quantize(Decimal("0.01"))
        if motivo_padrao not in motivos_cancelamento:
            messages.warning(request, "Informe o motivo do cancelamento.")
        elif motivo_padrao == "Outro motivo" and not observacao_cancelamento:
            messages.warning(request, "Informe a observacao adicional para outro motivo.")
        elif confirmacao_normalizada != "CANCELAR":
            messages.warning(request, "Digite CANCELAR exatamente para confirmar o cancelamento da venda.")
        elif ciencia_cancelamento != "1":
            messages.warning(request, "Marque a ciencia de que a venda nao sera apagada e os itens ficarao no historico.")
        elif recebimentos_count and destino_financeiro not in destinos_financeiros_recebimento:
            messages.warning(request, "Escolha o destino financeiro do valor ja recebido antes de cancelar a venda.")
        elif recebimentos_count and destino_financeiro == "credito_cliente" and not venda.cliente:
            messages.warning(request, "Nao e possivel gerar credito porque a venda nao tem cliente vinculado.")
        else:
            motivo = motivo_padrao
            if observacao_cancelamento:
                motivo = f"{motivo_padrao} - Observação: {observacao_cancelamento}"
            with transaction.atomic():
                resumo_financeiro = ""
                resumo_estoque = _devolver_estoque_cancelamento_venda(venda)
                if conta_receber and recebimentos_count:
                    primeiro_recebimento = conta_receber.recebimentos.order_by("data_recebimento", "id").first()
                    if destino_financeiro == "credito_cliente":
                        CreditoCliente.objects.create(
                            cliente=venda.cliente,
                            valor=total_recebido,
                            tipo=CreditoCliente.TIPO_CREDITO_GERADO,
                            origem_conta_receber=conta_receber,
                            origem_recebimento=primeiro_recebimento,
                            observacao=(
                                f"Credito gerado pelo cancelamento da venda #{venda.id}. "
                                f"Valor ja recebido preservado: {_formatar_moeda(total_recebido)}."
                            ),
                        )
                        resumo_financeiro = (
                            f" Valor ja pago ({_formatar_moeda(total_recebido)}) virou credito do cliente. "
                            "Recebimentos antigos preservados."
                        )
                        observacao_conta = (
                            f"Cancelada por venda nao realizada. Valor ja recebido "
                            f"({_formatar_moeda(total_recebido)}) transformado em credito do cliente."
                        )
                    elif destino_financeiro == "devolucao_manual":
                        resumo_financeiro = (
                            f" Valor ja pago ({_formatar_moeda(total_recebido)}) marcado como devolucao manual ao cliente. "
                            "Recebimentos antigos preservados; caixa nao foi alterado nesta fase."
                        )
                        observacao_conta = (
                            f"Cancelada por venda nao realizada. Valor ja recebido "
                            f"({_formatar_moeda(total_recebido)}) marcado como devolucao manual ao cliente."
                        )
                    else:
                        resumo_financeiro = (
                            f" Valor ja pago ({_formatar_moeda(total_recebido)}) ficou como pendencia financeira para resolucao posterior. "
                            "Recebimentos antigos preservados."
                        )
                        observacao_conta = (
                            f"Cancelada por venda nao realizada. Valor ja recebido "
                            f"({_formatar_moeda(total_recebido)}) ficou como pendencia financeira para resolucao posterior."
                        )

                    conta_receber.status = ContaReceber.STATUS_CANCELADA
                    conta_receber.valor_em_aberto = Decimal("0.00")
                    conta_receber.observacao = observacao_conta
                    conta_receber.save(update_fields=["status", "valor_em_aberto", "observacao", "atualizado_em"])

                venda.cancelada = True
                venda.cancelada_em = timezone.now()
                venda.motivo_cancelamento = motivo
                venda.save(update_fields=["cancelada", "cancelada_em", "motivo_cancelamento", "atualizado_em"])
                _registrar_evento_venda(
                    venda,
                    "venda_cancelada",
                    (
                        "Venda cancelada / venda nao realizada. "
                        f"Motivo: {motivo}. "
                        "Itens preservados para historico. "
                        f"{resumo_financeiro} "
                        f"{resumo_estoque} "
                        "Conta a receber vinculada cancelada/zerada quando existente. Caixa nao foi alterado nesta fase."
                    ),
                    canal="sistema",
                    usuario=venda.operador,
                )
                if not (conta_receber and recebimentos_count):
                    _sincronizar_conta_receber(venda, "venda cancelada")

            messages.success(request, "Venda marcada como CANCELADA / VENDA NAO REALIZADA.")
            return redirect(_url_com_retorno(detalhe_url, retorno_url))

    return render(
        request,
        "estoque/venda_cancelar.html",
        {
            "venda": venda,
            "itens_nota": itens_nota,
            "motivo": motivo,
            "motivo_padrao": motivo_padrao,
            "observacao_cancelamento": observacao_cancelamento,
            "motivos_cancelamento": motivos_cancelamento,
            "confirmacao_cancelamento": confirmacao_cancelamento,
            "ciencia_cancelamento": ciencia_cancelamento,
            "destino_financeiro": destino_financeiro,
            "destinos_financeiros_recebimento": destinos_financeiros_recebimento,
            "conta_receber": conta_receber,
            "contexto_venda_quitada": contexto_venda_quitada,
            "recebimentos_count": recebimentos_count,
            "creditos_count": creditos_count,
            "entregas_count": entregas_count,
            "eventos_count": eventos_count,
            "venda_antiga": venda_antiga,
            "sinais_consolidacao": sinais_consolidacao,
            "tem_sinais_consolidacao": bool(sinais_consolidacao),
            "retorno_url": retorno_url,
            "retorno_querystring": _querystring_retorno(retorno_url),
        },
    )


def venda_criar_entrega(request, pk):
    venda = get_object_or_404(Venda, pk=pk)
    bloqueio = _bloquear_venda_cancelada(request, venda)
    if bloqueio:
        return bloqueio
    
    # Verificar se já existe entrega para esta venda
    if EntregaRotaItem.objects.filter(venda=venda).exists():
        messages.warning(request, "Esta venda já possui entrega criada.")
        return redirect("estoque:venda_detalhe", pk=venda.pk)
    
    # Criar entrega unitária
    with transaction.atomic():
        rota = EntregaRota.objects.create(
            data=timezone.localdate(),
            tipo=EntregaRota.TIPO_UNITARIA,
            observacao=f"Entrega criada automaticamente da venda #{venda.pk}",
        )
        EntregaRotaItem.objects.create(
            rota=rota,
            venda=venda,
            ordem_entrega=1,
        )
    
    messages.success(request, f"Entrega unitária #{rota.pk} criada para a venda #{venda.pk}.")
    return redirect("estoque:venda_detalhe", pk=venda.pk)


def venda_whatsapp_pdf(request, pk):
    venda = get_object_or_404(
        Venda.objects.select_related("cliente").prefetch_related("itens__produto"),
        pk=pk,
    )
    buffer = _gerar_nota_whatsapp_pdf(venda)
    response = HttpResponse(buffer.getvalue(), content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="nota-whatsapp-venda-{venda.id}.pdf"'
    return response


def venda_whatsapp_imagem(request, pk):
    venda = get_object_or_404(
        Venda.objects.select_related("cliente").prefetch_related("itens__produto"),
        pk=pk,
    )
    buffer = _gerar_nota_whatsapp_imagem(venda)
    response = HttpResponse(buffer.getvalue(), content_type="image/png")
    response["Content-Disposition"] = f'inline; filename="nota-whatsapp-venda-{venda.id}.png"'
    return response


@ensure_csrf_cookie
def preparar_whatsapp_venda(request, pk):
    venda = get_object_or_404(
        Venda.objects.select_related("cliente"),
        pk=pk,
    )
    bloqueio = _bloquear_venda_cancelada(request, venda)
    if bloqueio:
        return bloqueio
    whatsapp_url = montar_link_whatsapp_venda(venda)
    return render(
        request,
        "estoque/preparar_whatsapp.html",
        {
            "venda": venda,
            "whatsapp_url": whatsapp_url,
        },
    )


@require_POST
def registrar_impressao(request, pk):
    venda = get_object_or_404(Venda, pk=pk)
    _registrar_evento_venda(
        venda,
        "nota_impressa",
        "Nota impressa.",
        canal="impressao",
    )
    return JsonResponse({"sucesso": True, "mensagem": "Registro de impressão salvo."})


@require_POST
def registrar_whatsapp_aberto(request, pk):
    venda = get_object_or_404(Venda, pk=pk)
    if venda.cancelada:
        return JsonResponse({"sucesso": False, "mensagem": "Venda cancelada. WhatsApp bloqueado."}, status=400)
    numero_usado = ""
    try:
        dados = json.loads(request.body.decode("utf-8") or "{}")
        numero_usado = "".join(ch for ch in str(dados.get("numero_usado") or "") if ch.isdigit())
    except (json.JSONDecodeError, UnicodeDecodeError):
        numero_usado = ""

    numero_whatsapp, origem_numero = _dados_numero_whatsapp_evento(venda, numero_usado)

    if venda.whatsapp_status != Venda.WHATSAPP_ENVIADO_CONFIRMADO:
        venda.whatsapp_status = Venda.WHATSAPP_ABERTO
        venda.whatsapp_aberto_em = timezone.now()
        if numero_whatsapp:
            venda.whatsapp_numero_usado = numero_whatsapp
        venda.save(update_fields=[
            "whatsapp_status",
            "whatsapp_aberto_em",
            "whatsapp_numero_usado",
            "atualizado_em",
        ])

    _registrar_evento_venda(
        venda,
        "whatsapp_aberto",
        "WhatsApp aberto para envio.",
        canal="whatsapp",
        numero_whatsapp=numero_whatsapp,
        origem_numero=origem_numero,
    )
    return JsonResponse({
        "sucesso": True,
        "mensagem": "Registro de abertura no WhatsApp salvo.",
        "whatsapp_status": venda.whatsapp_status,
        "whatsapp_status_texto": venda.whatsapp_status_texto,
    })


@require_POST
def registrar_checklist_whatsapp_aberto(request, pk):
    venda = get_object_or_404(Venda, pk=pk)
    if venda.cancelada:
        return JsonResponse({"sucesso": False, "mensagem": "Venda cancelada. Checklist bloqueado."}, status=400)
    try:
        dados = json.loads(request.body.decode("utf-8") or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        dados = {}

    numero_usado = "".join(ch for ch in str(dados.get("numero_usado") or "") if ch.isdigit())
    rota_id = str(dados.get("rota_id") or "").strip()
    rota_item_id = str(dados.get("rota_item_id") or "").strip()
    numero_whatsapp, origem_numero = _dados_numero_whatsapp_evento(venda, numero_usado)

    detalhes = []
    if rota_id:
        detalhes.append(f"rota/entrega #{rota_id}")
    if rota_item_id:
        detalhes.append(f"bloco #{rota_item_id}")
    detalhes_texto = f" ({', '.join(detalhes)})." if detalhes else "."

    _registrar_evento_venda(
        venda,
        "checklist_cliente_whatsapp_aberto",
        f"WhatsApp aberto para envio do checklist ao cliente{detalhes_texto}",
        canal="whatsapp_checklist",
        numero_whatsapp=numero_whatsapp,
        origem_numero=origem_numero,
    )
    return JsonResponse({
        "sucesso": True,
        "mensagem": "Registro de abertura do checklist no WhatsApp salvo.",
    })


@require_POST
def confirmar_checklist_whatsapp(request, pk):
    venda = get_object_or_404(Venda, pk=pk)
    if venda.cancelada:
        return JsonResponse({"sucesso": False, "mensagem": "Venda cancelada. Checklist bloqueado."}, status=400)
    try:
        dados = json.loads(request.body.decode("utf-8") or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        dados = {}

    numero_usado = "".join(ch for ch in str(dados.get("numero_usado") or "") if ch.isdigit())
    rota_id = str(dados.get("rota_id") or "").strip()
    rota_item_id = str(dados.get("rota_item_id") or "").strip()
    numero_whatsapp, origem_numero = _dados_numero_whatsapp_evento(venda, numero_usado)

    detalhes = []
    if rota_id:
        detalhes.append(f"rota/entrega #{rota_id}")
    if rota_item_id:
        detalhes.append(f"bloco #{rota_item_id}")
    detalhes_texto = f" ({', '.join(detalhes)})." if detalhes else "."
    descricao = f"Checklist enviado ao cliente por WhatsApp{detalhes_texto}"

    ja_existe = EventoVenda.objects.filter(
        venda=venda,
        tipo_evento="checklist_cliente_enviado",
        canal="whatsapp_checklist",
        descricao=descricao,
    ).exists()
    if not ja_existe:
        _registrar_evento_venda(
            venda,
            "checklist_cliente_enviado",
            descricao,
            canal="whatsapp_checklist",
            numero_whatsapp=numero_whatsapp,
            origem_numero=origem_numero,
        )
    return JsonResponse({
        "sucesso": True,
        "mensagem": "Checklist marcado como enviado ao cliente.",
        "already_exists": ja_existe,
    })


@require_POST
def confirmar_whatsapp(request, pk):
    venda = get_object_or_404(Venda, pk=pk)
    if venda.cancelada:
        return JsonResponse({"sucesso": False, "mensagem": "Venda cancelada. WhatsApp bloqueado."}, status=400)
    ultimo_whatsapp_aberto = venda.eventos.filter(
        canal="whatsapp",
        tipo_evento="whatsapp_aberto",
    ).first()
    numero_whatsapp = (
        (ultimo_whatsapp_aberto.numero_whatsapp if ultimo_whatsapp_aberto else "")
        or venda.whatsapp_numero_usado
        or ""
    )
    origem_numero = (
        (ultimo_whatsapp_aberto.origem_numero if ultimo_whatsapp_aberto else "")
        or ""
    )
    if not numero_whatsapp:
        numero_whatsapp, origem_numero = _dados_numero_whatsapp_evento(venda)
    if not origem_numero:
        origem_numero = EventoVenda.ORIGEM_NUMERO_DESCONHECIDO
    venda.whatsapp_status = Venda.WHATSAPP_ENVIADO_CONFIRMADO
    venda.whatsapp_confirmado_em = timezone.now()
    venda.save(update_fields=[
        "whatsapp_status",
        "whatsapp_confirmado_em",
        "atualizado_em",
    ])
    _registrar_evento_venda(
        venda,
        "whatsapp_confirmado",
        "Envio confirmado manualmente.",
        canal="whatsapp",
        numero_whatsapp=numero_whatsapp,
        origem_numero=origem_numero,
    )
    return JsonResponse({
        "sucesso": True,
        "mensagem": "Confirmacao de envio via WhatsApp salva.",
        "whatsapp_status": venda.whatsapp_status,
        "whatsapp_status_texto": venda.whatsapp_status_texto,
        "whatsapp_status_selos": _status_whatsapp_consulta_venda(venda),
        "comunicacao_descricao": "Envio confirmado manualmente.",
        "comunicacao_numero": numero_whatsapp,
        "comunicacao_origem": _rotulo_origem_numero_whatsapp(origem_numero),
    })


def _formatar_moeda(valor):
    numero = Decimal(valor or 0)
    texto = f"{numero:,.2f}"
    return "R$ " + texto.replace(",", "X").replace(".", ",").replace("X", ".")


def _formatar_quantidade(valor):
    numero = Decimal(valor or 0).quantize(Decimal("0.001"))
    return f"{numero:f}".rstrip("0").rstrip(",").rstrip(".")


def _fonte_nota_whatsapp(tamanho, negrito=False):
    nomes = ["segoeuib.ttf", "arialbd.ttf"] if negrito else ["segoeui.ttf", "arial.ttf"]
    for nome in nomes:
        caminho = Path("C:/Windows/Fonts") / nome
        if caminho.exists():
            return ImageFont.truetype(str(caminho), tamanho)
    return ImageFont.load_default()


def _texto_largura(draw, texto, fonte):
    caixa = draw.textbbox((0, 0), str(texto), font=fonte)
    return caixa[2] - caixa[0]


def _quebrar_texto(draw, texto, fonte, largura_maxima):
    texto = str(texto or "-")
    linhas = []
    for bloco in texto.splitlines() or [""]:
        palavras = bloco.split()
        if not palavras:
            linhas.append("")
            continue
        linha = ""
        for palavra in palavras:
            teste = f"{linha} {palavra}".strip()
            if _texto_largura(draw, teste, fonte) <= largura_maxima:
                linha = teste
                continue
            if linha:
                linhas.append(linha)
            if _texto_largura(draw, palavra, fonte) <= largura_maxima:
                linha = palavra
            else:
                partes = textwrap.wrap(palavra, width=18)
                linhas.extend(partes[:-1])
                linha = partes[-1] if partes else palavra
        if linha:
            linhas.append(linha)
    return linhas


def _gerar_paginas_nota_whatsapp(venda):
    largura, altura = 1080, 1600
    margem = 42
    fundo = "#f6f1e8"
    texto = "#1f2933"
    suave = "#64748b"
    verde = "#14532d"
    borda = "#ead7b0"
    card = "#fffdf8"

    fonte_empresa = _fonte_nota_whatsapp(28, True)
    fonte_titulo = _fonte_nota_whatsapp(40, True)
    fonte_subtitulo = _fonte_nota_whatsapp(26, True)
    fonte_label = _fonte_nota_whatsapp(19, True)
    fonte_intro = _fonte_nota_whatsapp(27)
    fonte_texto_negrito = _fonte_nota_whatsapp(25, True)
    fonte_tabela = _fonte_nota_whatsapp(23)
    fonte_tabela_negrito = _fonte_nota_whatsapp(23, True)
    fonte_total = _fonte_nota_whatsapp(38, True)

    paginas = []
    pagina_numero = 0

    def nova_pagina(continua=False):
        nonlocal pagina_numero
        pagina_numero += 1
        imagem = Image.new("RGB", (largura, altura), fundo)
        draw = ImageDraw.Draw(imagem)
        draw.rounded_rectangle(
            (margem, margem, largura - margem, altura - margem),
            radius=28,
            fill=card,
            outline=borda,
            width=3,
        )
        y_atual = margem + 28
        draw.text((margem + 30, y_atual), "LA Neiva", fill=verde, font=fonte_empresa)
        y_atual += 36
        titulo = "Nota de Venda"
        if continua:
            titulo += " (contin.)"
        draw.text((margem + 30, y_atual), titulo, fill=texto, font=fonte_titulo)
        numero_venda = f"Venda #{venda.id}"
        numero_largura = _texto_largura(draw, numero_venda, fonte_subtitulo)
        draw.text((largura - margem - 30 - numero_largura, margem + 40), numero_venda, fill=texto, font=fonte_subtitulo)
        draw.text((margem + 30, altura - margem - 42), f"Pagina {pagina_numero}", fill=suave, font=fonte_label)
        y_atual += 58
        return imagem, draw, y_atual

    imagem, draw, y = nova_pagina()

    def adicionar_pagina_se_precisar(altura_necessaria):
        nonlocal imagem, draw, y
        if y + altura_necessaria <= altura - margem - 74:
            return
        paginas.append(imagem)
        imagem, draw, y = nova_pagina(continua=True)

    def desenhar_campo(x, y_campo, largura_campo, label, valor):
        draw.rounded_rectangle(
            (x, y_campo, x + largura_campo, y_campo + 78),
            radius=15,
            fill="#f8fafc",
            outline="#e2e8f0",
            width=2,
        )
        draw.text((x + 16, y_campo + 10), label.upper(), fill=suave, font=fonte_label)
        linhas = _quebrar_texto(draw, valor, fonte_texto_negrito, largura_campo - 40)
        for indice, linha in enumerate(linhas[:1]):
            draw.text((x + 16, y_campo + 38 + indice * 28), linha, fill=texto, font=fonte_texto_negrito)

    cliente = venda.cliente.nome if venda.cliente else "Consumidor"
    data_venda = venda.data_venda.strftime("%d/%m/%Y")
    vencimento = venda.data_vencimento.strftime("%d/%m/%Y") if venda.data_vencimento else "-"
    pagamento = venda.tipo_pagamento or "-"

    x1 = margem + 30
    draw.rounded_rectangle(
        (x1, y, largura - margem - 30, y + 146),
        radius=18,
        fill="#fff8e8",
        outline="#ead19a",
        width=2,
    )
    draw.text((x1 + 22, y + 18), f"Nota de Venda #{venda.id}", fill=texto, font=fonte_subtitulo)
    draw.text((x1 + 22, y + 56), f"Cliente: {cliente}", fill=texto, font=fonte_texto_negrito)
    draw.text((x1 + 22, y + 90), f"Total: {_formatar_moeda(venda.total)}", fill=verde, font=fonte_texto_negrito)
    draw.text((x1 + 22, y + 118), "Segue a nota referente à venda abaixo.", fill=suave, font=fonte_intro)
    y += 164

    contexto_pedido_parcial = _contexto_venda_pedido_parcial(venda)

    largura_campo = (largura - margem * 2 - 60 - 22) // 2
    x2 = x1 + largura_campo + 22
    desenhar_campo(x1, y, largura_campo, "Cliente", cliente)
    desenhar_campo(x2, y, largura_campo, "Data", data_venda)
    y += 92
    desenhar_campo(x1, y, largura_campo, "Pagamento", pagamento)
    desenhar_campo(x2, y, largura_campo, "Vencimento", vencimento)
    y += 106

    if contexto_pedido_parcial:
        largura_aviso = largura - margem * 2 - 60
        linhas_aviso = []
        linhas_aviso.extend(
            _quebrar_texto(
                draw,
                contexto_pedido_parcial["mensagem"],
                fonte_tabela_negrito,
                largura_aviso - 54,
            )
        )
        if contexto_pedido_parcial["itens_pendentes"]:
            linhas_aviso.append("Itens pendentes:")
            for item_pendente in contexto_pedido_parcial["itens_pendentes"]:
                linhas_aviso.extend(
                    _quebrar_texto(draw, f"- {item_pendente}", fonte_tabela, largura_aviso - 64)
                )
        altura_aviso = 30 + len(linhas_aviso) * 29
        adicionar_pagina_se_precisar(altura_aviso + 18)
        draw.rounded_rectangle(
            (x1, y, largura - margem - 30, y + altura_aviso),
            radius=16,
            fill="#eff6ff",
            outline="#60a5fa",
            width=3,
        )
        draw.rectangle((x1, y + 10, x1 + 8, y + altura_aviso - 10), fill="#2563eb")
        aviso_y = y + 16
        for indice, linha in enumerate(linhas_aviso):
            fonte_linha = fonte_tabela_negrito if indice == 0 else fonte_tabela
            draw.text((x1 + 24, aviso_y), linha, fill="#1e3a8a", font=fonte_linha)
            aviso_y += 29
        y += altura_aviso + 18

    draw.text((x1, y), "Itens da venda", fill=texto, font=fonte_subtitulo)
    y += 42

    def desenhar_item(indice, item):
        nonlocal y
        nome = item.produto.nome if item.produto else "Produto nao identificado"
        quantidade = _formatar_quantidade(item.quantidade)
        unidade = item.unidade or "-"
        preco = _formatar_moeda(item.preco_unitario)
        subtotal = _formatar_moeda(item.valor_total)
        resumo = f"{quantidade} {unidade} × {preco}"
        subtotal_largura = _texto_largura(draw, subtotal, fonte_tabela_negrito)
        x_subtotal = largura - margem - 52 - subtotal_largura
        largura_nome = 850
        linhas_nome = _quebrar_texto(draw, f"{indice}. {nome}", fonte_tabela_negrito, largura_nome)
        linhas_nome = linhas_nome[:2]
        altura_item = 74 if len(linhas_nome) == 1 else 98
        adicionar_pagina_se_precisar(altura_item)
        topo = y
        draw.rounded_rectangle(
            (x1, topo, largura - margem - 30, topo + altura_item - 8),
            radius=14,
            fill="#ffffff",
            outline="#edf1f6",
            width=2,
        )
        texto_y = topo + 13
        for linha in linhas_nome:
            draw.text((x1 + 24, texto_y), linha, fill=texto, font=fonte_tabela_negrito)
            texto_y += 28

        resumo_y = topo + 41 + (len(linhas_nome) - 1) * 24
        draw.text((x1 + 24, resumo_y), resumo, fill="#42526a", font=fonte_tabela)
        draw.text((x_subtotal, resumo_y), subtotal, fill=texto, font=fonte_tabela_negrito)
        y = topo + altura_item + 4

    for indice, item in enumerate(venda.itens.all(), start=1):
        desenhar_item(indice, item)

    adicionar_pagina_se_precisar(116)
    draw.rounded_rectangle(
        (x1, y + 8, largura - margem - 30, y + 92),
        radius=20,
        fill="#e8f5e9",
        outline="#b7e4c7",
        width=2,
    )
    draw.text((x1 + 22, y + 35), "Total da venda", fill=verde, font=fonte_subtitulo)
    total = _formatar_moeda(venda.total)
    total_largura = _texto_largura(draw, total, fonte_total)
    draw.text((largura - margem - 54 - total_largura, y + 27), total, fill=verde, font=fonte_total)

    paginas.append(imagem)
    return paginas


def _gerar_nota_whatsapp_pdf(venda):
    paginas = _gerar_paginas_nota_whatsapp(venda)
    pdf = BytesIO()
    paginas[0].save(pdf, format="PDF", save_all=True, append_images=paginas[1:], resolution=150.0)
    pdf.seek(0)
    return pdf


def _gerar_nota_whatsapp_imagem(venda):
    paginas = _gerar_paginas_nota_whatsapp(venda)
    if len(paginas) == 1:
        imagem_final = paginas[0]
    else:
        largura = max(pagina.width for pagina in paginas)
        espaco = 24
        altura = sum(pagina.height for pagina in paginas) + espaco * (len(paginas) - 1)
        imagem_final = Image.new("RGB", (largura, altura), "#f6f1e8")
        y = 0
        for pagina in paginas:
            imagem_final.paste(pagina, ((largura - pagina.width) // 2, y))
            y += pagina.height + espaco

    png = BytesIO()
    imagem_final.save(png, format="PNG")
    png.seek(0)
    return png


def _gerar_cobranca_cliente_imagem(cliente, financeiro, cobranca):
    largura = 1080
    margem = 42
    contas = cobranca.get("contas", [])
    altura = max(980, 540 + max(len(contas), 1) * 146)

    fundo = "#f6f1e8"
    card = "#fffdf8"
    texto = "#172033"
    suave = "#64748b"
    azul = "#1e3a8a"
    vermelho = "#991b1b"
    borda = "#dbe5f0"

    fonte_empresa = _fonte_nota_whatsapp(30, True)
    fonte_titulo = _fonte_nota_whatsapp(44, True)
    fonte_subtitulo = _fonte_nota_whatsapp(28, True)
    fonte_label = _fonte_nota_whatsapp(20, True)
    fonte_texto = _fonte_nota_whatsapp(25)
    fonte_texto_negrito = _fonte_nota_whatsapp(25, True)
    fonte_rodape = _fonte_nota_whatsapp(26, True)

    imagem = Image.new("RGB", (largura, altura), fundo)
    draw = ImageDraw.Draw(imagem)

    draw.rounded_rectangle(
        (margem, margem, largura - margem, altura - margem),
        radius=28,
        fill=card,
        outline="#ead7b0",
        width=3,
    )

    x = margem + 30
    direita = largura - margem - 30
    y = margem + 28

    draw.text((x, y), "L A Neiva", fill=azul, font=fonte_empresa)
    y += 42
    draw.text((x, y), "Cobrança do cliente", fill=texto, font=fonte_titulo)
    y += 66

    draw.rounded_rectangle((x, y, direita, y + 116), radius=18, fill="#eff6ff", outline="#bfdbfe", width=2)
    draw.text((x + 22, y + 18), f"Cliente: {cliente.nome}", fill=texto, font=fonte_texto_negrito)
    draw.text((x + 22, y + 58), f"WhatsApp: {cliente.whatsapp or '-'}", fill=suave, font=fonte_texto)
    y += 138

    abertas_total = _formatar_moeda(Decimal(financeiro["contas_abertas_total"]))
    vencidas_total = _formatar_moeda(Decimal(financeiro["contas_vencidas_total"]))

    def desenhar_resumo(x_campo, y_campo, largura_campo, label, valor, alerta=False):
        draw.rounded_rectangle(
            (x_campo, y_campo, x_campo + largura_campo, y_campo + 82),
            radius=15,
            fill="#fef2f2" if alerta else "#f8fafc",
            outline="#fecaca" if alerta else "#e2e8f0",
            width=2,
        )
        draw.text((x_campo + 15, y_campo + 11), label.upper(), fill=vermelho if alerta else suave, font=fonte_label)
        draw.text((x_campo + 15, y_campo + 42), valor, fill=vermelho if alerta else texto, font=fonte_texto_negrito)

    def desenhar_resumo_vencidas(x_campo, y_campo, largura_campo, quantidade, valor):
        draw.rounded_rectangle(
            (x_campo, y_campo, x_campo + largura_campo, y_campo + 106),
            radius=15,
            fill="#fef2f2",
            outline="#fecaca",
            width=2,
        )
        quantidade_texto = f"{quantidade} conta" if quantidade == 1 else f"{quantidade} contas"
        draw.text((x_campo + 15, y_campo + 10), "CONTAS VENCIDAS", fill=vermelho, font=fonte_label)
        draw.text((x_campo + 15, y_campo + 39), quantidade_texto, fill=vermelho, font=fonte_texto_negrito)
        draw.text((x_campo + 15, y_campo + 65), valor, fill=vermelho, font=fonte_subtitulo)

    largura_campo = (direita - x - 20) // 2
    x2 = x + largura_campo + 20
    desenhar_resumo(
        x,
        y,
        largura_campo,
        "Contas em aberto",
        f"{financeiro['contas_abertas_qtd']} / {abertas_total}",
    )
    desenhar_resumo_vencidas(x2, y, largura_campo, financeiro["contas_vencidas_qtd"], vencidas_total)
    y += 124
    if cobranca.get("maior_atraso_dias"):
        desenhar_resumo(x, y, largura_campo, "Maior atraso", f"{cobranca['maior_atraso_dias']} dias", True)
        y += 100

    draw.text((x, y), "Contas em aberto", fill=texto, font=fonte_subtitulo)
    y += 44

    if not contas:
        draw.rounded_rectangle((x, y, direita, y + 82), radius=14, fill="#ffffff", outline=borda, width=2)
        draw.text((x + 22, y + 25), "Nenhuma conta em aberto.", fill=suave, font=fonte_texto)
        y += 100
    else:
        for conta in contas:
            vencida = bool(conta.get("vencida"))
            fill = "#fef2f2" if vencida else "#eff6ff"
            outline = "#fecaca" if vencida else "#bfdbfe"
            status_cor = vermelho if vencida else azul
            titulo = conta.get("titulo") or "Conta"
            status = conta.get("status") or "Em dia"
            meta = (
                f"Data: {conta.get('data') or 'Data não informada'} | "
                f"Vencimento: {conta.get('vencimento') or 'Vencimento não informado'} | "
                f"Valor em aberto: {conta.get('valor') or 'R$ 0,00'}"
            )
            linhas_meta = _quebrar_texto(draw, meta, fonte_texto, direita - x - 44)[:2]
            altura_card = 82 + len(linhas_meta) * 30
            draw.rounded_rectangle((x, y, direita, y + altura_card), radius=14, fill=fill, outline=outline, width=2)
            draw.text((x + 20, y + 13), titulo, fill=texto, font=fonte_texto_negrito)
            status_largura = _texto_largura(draw, status, fonte_label)
            draw.rounded_rectangle((direita - status_largura - 46, y + 12, direita - 20, y + 45), radius=16, fill="#fee2e2" if vencida else "#dbeafe")
            draw.text((direita - status_largura - 33, y + 18), status, fill=status_cor, font=fonte_label)
            y_linha = y + 55
            for linha in linhas_meta:
                draw.text((x + 20, y_linha), linha, fill=suave, font=fonte_texto)
                y_linha += 30
            y += altura_card + 14

    draw.text((direita - _texto_largura(draw, "LA Neiva", fonte_rodape), altura - margem - 62), "LA Neiva", fill=azul, font=fonte_rodape)

    png = BytesIO()
    imagem.save(png, format="PNG")
    png.seek(0)
    return png


def _decimal_comprovante(valor):
    try:
        return Decimal(str(valor or "0")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return Decimal("0.00")


def _gerar_comprovante_recebimento_imagem(dados):
    largura = 1080
    margem = 42
    fundo = "#f6f1e8"
    texto = "#1f2933"
    suave = "#64748b"
    verde = "#14532d"
    borda = "#ead7b0"
    card = "#fffdf8"

    fonte_empresa = _fonte_nota_whatsapp(30, True)
    fonte_titulo = _fonte_nota_whatsapp(42, True)
    fonte_subtitulo = _fonte_nota_whatsapp(27, True)
    fonte_label = _fonte_nota_whatsapp(20, True)
    fonte_texto = _fonte_nota_whatsapp(25)
    fonte_texto_negrito = _fonte_nota_whatsapp(26, True)
    fonte_total = _fonte_nota_whatsapp(36, True)
    fonte_rodape = _fonte_nota_whatsapp(24, True)

    contas = dados.get("contas", [])
    contas_abertas = dados.get("contas_abertas", [])
    altura_contas = max(len(contas), 1) * 142
    altura_contas_abertas = max(len(contas_abertas), 1) * 104
    altura = max(1500, 1030 + altura_contas + altura_contas_abertas)
    imagem = Image.new("RGB", (largura, altura), fundo)
    draw = ImageDraw.Draw(imagem)

    draw.rounded_rectangle(
        (margem, margem, largura - margem, altura - margem),
        radius=28,
        fill=card,
        outline=borda,
        width=3,
    )

    x = margem + 30
    direita = largura - margem - 30
    y = margem + 28

    draw.text((x, y), "L A Neiva", fill=verde, font=fonte_empresa)
    y += 40
    draw.text((x, y), "Comprovante de Pagamento", fill=texto, font=fonte_titulo)
    y += 62

    cliente = dados.get("cliente_nome") or "Cliente"
    draw.rounded_rectangle((x, y, direita, y + 142), radius=18, fill="#fff8e8", outline="#ead19a", width=2)
    draw.text((x + 22, y + 18), f"Cliente: {cliente}", fill=texto, font=fonte_texto_negrito)
    draw.text((x + 22, y + 56), f"Data: {dados.get('data_recebimento') or '-'}", fill=texto, font=fonte_texto)
    draw.text((x + 22, y + 92), f"Forma de pagamento: {dados.get('forma_pagamento') or '-'}", fill=suave, font=fonte_texto)
    y += 164

    def desenhar_campo(x_campo, y_campo, largura_campo, label, valor, destaque=False):
        draw.rounded_rectangle(
            (x_campo, y_campo, x_campo + largura_campo, y_campo + 86),
            radius=15,
            fill="#f8fafc" if not destaque else "#e8f5e9",
            outline="#e2e8f0" if not destaque else "#b7e4c7",
            width=2,
        )
        draw.text((x_campo + 16, y_campo + 12), label.upper(), fill=suave, font=fonte_label)
        draw.text((x_campo + 16, y_campo + 43), valor, fill=verde if destaque else texto, font=fonte_texto_negrito)

    largura_campo = (direita - x - 22) // 2
    x2 = x + largura_campo + 22
    saldo_anterior = _decimal_comprovante(dados.get("saldo_anterior"))
    valor_pago = _decimal_comprovante(dados.get("valor_pago"))
    saldo_atual = _decimal_comprovante(dados.get("saldo_atual"))
    credito_gerado = _decimal_comprovante(dados.get("credito_gerado"))

    desenhar_campo(x, y, largura_campo, "Saldo anterior", _formatar_moeda(saldo_anterior))
    desenhar_campo(x2, y, largura_campo, "Valor pago", _formatar_moeda(valor_pago), destaque=True)
    y += 104
    desenhar_campo(x, y, largura_campo, "Saldo apos pagamento", _formatar_moeda(saldo_atual), destaque=saldo_atual <= Decimal("0.00"))
    if credito_gerado > Decimal("0.00"):
        desenhar_campo(x2, y, largura_campo, "Credito gerado", _formatar_moeda(credito_gerado), destaque=True)
    else:
        desenhar_campo(x2, y, largura_campo, "Credito gerado", "-", destaque=False)
    y += 124

    draw.text((x, y), "Aplicacao do pagamento", fill=texto, font=fonte_subtitulo)
    y += 42

    if not contas:
        draw.rounded_rectangle((x, y, direita, y + 74), radius=14, fill="#ffffff", outline="#edf1f6", width=2)
        draw.text((x + 24, y + 23), "Nenhuma conta identificada com seguranca.", fill=suave, font=fonte_texto)
        y += 92
    else:
        for conta in contas:
            venda = conta.get("venda_id") or conta.get("conta_id") or "-"
            saldo_antes = _formatar_moeda(_decimal_comprovante(conta.get("saldo_antes")))
            valor = _formatar_moeda(_decimal_comprovante(conta.get("valor_aplicado")))
            saldo_restante_conta = _decimal_comprovante(conta.get("saldo_restante"))
            status = "quitada" if conta.get("quitada") else "parcial"
            fill_status = "#166534" if conta.get("quitada") else "#92400e"
            data_nota = conta.get("data_nota") or "-"
            rotulo_saldo_antes = "Valor da nota" if conta.get("nota_inteira_antes") else "Saldo da nota antes do pagamento"

            draw.rounded_rectangle((x, y, direita, y + 130), radius=14, fill="#ffffff", outline="#edf1f6", width=2)
            titulo_conta = f"Venda/Nota #{venda} - {data_nota}"
            linhas_nota = _quebrar_texto(draw, titulo_conta, fonte_texto_negrito, 610)
            draw.text((x + 24, y + 14), linhas_nota[0], fill=texto, font=fonte_texto_negrito)
            draw.text((x + 24, y + 50), f"{rotulo_saldo_antes}: {saldo_antes}", fill=texto, font=fonte_texto)
            draw.text((x + 24, y + 80), f"Valor abatido agora: {valor}", fill=texto, font=fonte_texto)
            if not conta.get("quitada"):
                draw.text((x + 24, y + 110), f"Restante desta nota: {_formatar_moeda(saldo_restante_conta)}", fill=suave, font=fonte_label)
            else:
                draw.text((x + 24, y + 110), "Status: quitada", fill=suave, font=fonte_label)
            draw.rounded_rectangle((direita - 170, y + 15, direita - 24, y + 49), radius=16, fill="#ecfdf3" if conta.get("quitada") else "#fef3c7")
            status_largura = _texto_largura(draw, status, fonte_label)
            draw.text((direita - 97 - status_largura // 2, y + 21), status, fill=fill_status, font=fonte_label)
            y += 142

    y += 12
    draw.text((x, y), "Contas em aberto apos pagamento", fill=texto, font=fonte_subtitulo)
    y += 42

    if not contas_abertas:
        draw.rounded_rectangle((x, y, direita, y + 74), radius=14, fill="#ffffff", outline="#edf1f6", width=2)
        draw.text((x + 24, y + 23), "Nenhuma conta em aberto apos este pagamento.", fill=verde, font=fonte_texto)
        y += 92
    else:
        for conta in contas_abertas:
            venda = conta.get("venda_id") or conta.get("conta_id") or "-"
            data_nota = conta.get("data_nota") or "-"
            saldo_conta = _formatar_moeda(_decimal_comprovante(conta.get("saldo_atual")))
            dias_aberto = int(conta.get("dias_aberto") or 0)
            em_atraso = bool(conta.get("em_atraso"))
            cor_dias = "#b91c1c" if em_atraso else "#1e3a8a"
            fundo_dias = "#fee2e2" if em_atraso else "#eff6ff"
            texto_dias = f"{dias_aberto} dia{'s' if dias_aberto != 1 else ''} em aberto"

            draw.rounded_rectangle((x, y, direita, y + 92), radius=14, fill="#ffffff", outline="#edf1f6", width=2)
            titulo_conta = f"Venda/Nota #{venda} - {data_nota}"
            linhas_nota = _quebrar_texto(draw, titulo_conta, fonte_texto_negrito, 590)
            draw.text((x + 24, y + 13), linhas_nota[0], fill=texto, font=fonte_texto_negrito)
            draw.text((x + 24, y + 49), f"Saldo atual: {saldo_conta}", fill=texto, font=fonte_texto)
            draw.rounded_rectangle((direita - 260, y + 28, direita - 24, y + 62), radius=17, fill=fundo_dias)
            dias_largura = _texto_largura(draw, texto_dias, fonte_label)
            draw.text((direita - 142 - dias_largura // 2, y + 34), texto_dias, fill=cor_dias, font=fonte_label)
            y += 104

    y += 12
    draw.rounded_rectangle((x, y, direita, y + 132), radius=20, fill="#e8f5e9", outline="#b7e4c7", width=2)
    draw.text((x + 22, y + 26), "Total pago", fill=verde, font=fonte_subtitulo)
    total_pago = _formatar_moeda(valor_pago)
    total_pago_largura = _texto_largura(draw, total_pago, fonte_total)
    draw.text((direita - 22 - total_pago_largura, y + 18), total_pago, fill=verde, font=fonte_total)
    draw.text((x + 22, y + 82), "Saldo total da divida apos pagamento", fill=verde, font=fonte_label)
    total = _formatar_moeda(saldo_atual)
    total_largura = _texto_largura(draw, total, fonte_texto_negrito)
    draw.text((direita - 22 - total_largura, y + 78), total, fill=verde, font=fonte_texto_negrito)
    y += 166

    draw.text((x, y), "Obrigado.", fill=texto, font=fonte_texto)
    y += 34
    draw.text((x, y), "L A Neiva", fill=verde, font=fonte_rodape)

    png = BytesIO()
    imagem.save(png, format="PNG")
    png.seek(0)
    return png


def _normalizar_numero_whatsapp_evento(numero):
    digitos = "".join(ch for ch in str(numero or "") if ch.isdigit())
    if not digitos:
        return ""
    if not digitos.startswith("55") and len(digitos) in (10, 11):
        digitos = "55" + digitos
    return digitos


def _numero_whatsapp_cadastro_venda(venda):
    cliente = venda.cliente if venda else None
    if not cliente:
        return ""
    numero = Cliente.normalizar_whatsapp(cliente.whatsapp_normalizado or cliente.whatsapp)
    return _normalizar_numero_whatsapp_evento(numero)


def _dados_numero_whatsapp_evento(venda, numero_usado=""):
    numero_manual = _normalizar_numero_whatsapp_evento(numero_usado)
    if numero_manual:
        return numero_manual, EventoVenda.ORIGEM_NUMERO_AVULSO

    numero_cadastro = _numero_whatsapp_cadastro_venda(venda)
    if numero_cadastro:
        return numero_cadastro, EventoVenda.ORIGEM_NUMERO_CADASTRO

    return "", EventoVenda.ORIGEM_NUMERO_DESCONHECIDO


def _rotulo_origem_numero_whatsapp(origem):
    return dict(EventoVenda.ORIGEM_NUMERO_CHOICES).get(origem or "", "")


def _registrar_evento_venda(
    venda,
    tipo_evento,
    descricao,
    canal="sistema",
    usuario=None,
    numero_whatsapp=None,
    origem_numero=None,
):
    if not venda:
        return None
    usuario_texto = usuario if usuario is not None else (venda.operador or "")
    return EventoVenda.objects.create(
        venda=venda,
        tipo_evento=tipo_evento,
        descricao=descricao,
        canal=canal,
        usuario=usuario_texto,
        numero_whatsapp=numero_whatsapp or None,
        origem_numero=origem_numero or None,
    )


def _evento_pedido_parcial_da_venda(venda):
    eventos_prefetch = getattr(venda, "_prefetched_objects_cache", {}).get("eventos")
    if eventos_prefetch is not None:
        eventos = [
            evento
            for evento in eventos_prefetch
            if evento.tipo_evento == "pedido_parcial"
        ]
        if eventos:
            return sorted(eventos, key=lambda evento: (evento.criado_em, evento.id), reverse=True)[0]
        return None
    return (
        EventoVenda.objects.filter(venda=venda, tipo_evento="pedido_parcial")
        .order_by("-criado_em", "-id")
        .first()
    )


def _contexto_venda_pedido_parcial(venda):
    evento = _evento_pedido_parcial_da_venda(venda)
    if not evento or not evento.descricao:
        pedido_inferido = _pedido_parcial_inferido_da_venda(venda)
        if not pedido_inferido:
            return None
        return _contexto_venda_pedido_parcial_por_pedido(pedido_inferido)

    pedido_id = None
    pedido_match = re.search(r"Pedido #(\d+)", evento.descricao)
    if pedido_match:
        pedido_id = pedido_match.group(1)

    itens_pendentes = []
    for linha in evento.descricao.splitlines():
        linha = linha.strip()
        if linha.startswith("- "):
            itens_pendentes.append(linha[2:].strip())

    if not pedido_id:
        return None

    return _contexto_venda_pedido_parcial_base(pedido_id, itens_pendentes)


def _contexto_venda_pedido_parcial_base(pedido_id, itens_pendentes):
    return {
        "pedido_id": str(pedido_id),
        "mensagem": (
            f"Venda parcial gerada a partir do Pedido #{pedido_id}. "
            "Esta nota contém os itens disponíveis agora. Os itens pendentes continuam "
            "registrados no pedido para entrega/venda futura."
        ),
        "itens_pendentes": itens_pendentes,
    }


def _contexto_venda_pedido_parcial_por_pedido(pedido):
    itens_pendentes = []
    for item in pedido.itens.all():
        if (item.quantidade or Decimal("0.000")) <= 0:
            continue
        produto_nome = item.produto.nome if item.produto else "Produto nao identificado"
        quantidade = _formatar_quantidade(item.quantidade)
        unidade = f" {item.unidade}" if item.unidade else ""
        itens_pendentes.append(f"{produto_nome} - {quantidade}{unidade}")
    return _contexto_venda_pedido_parcial_base(pedido.id, itens_pendentes)


def _pedido_parcial_inferido_da_venda(venda):
    if not venda or not venda.cliente_id:
        return None

    from .models import Pedido

    venda_produto_ids = {
        produto_id
        for produto_id in venda.itens.values_list("produto_id", flat=True)
        if produto_id
    }
    if not venda_produto_ids:
        return None

    candidatos = (
        Pedido.objects.filter(cliente_id=venda.cliente_id, status=Pedido.STATUS_PARCIAL)
        .prefetch_related("itens__produto")
        .order_by("-atualizado_em", "-id")
    )
    melhor_pedido = None
    melhor_pontuacao = (0, 0)

    for pedido in candidatos:
        if pedido.data_pedido and venda.data_venda and pedido.data_pedido > venda.data_venda:
            continue
        itens = list(pedido.itens.all())
        if not any((item.quantidade or Decimal("0.000")) > 0 for item in itens):
            continue
        produto_ids_pedido = {item.produto_id for item in itens if item.produto_id}
        produtos_em_comum = venda_produto_ids & produto_ids_pedido
        if not produtos_em_comum:
            continue
        produtos_zerados = {
            item.produto_id
            for item in itens
            if item.produto_id and (item.quantidade or Decimal("0.000")) <= 0
        }
        produtos_vendidos_zerados = venda_produto_ids & produtos_zerados
        pontuacao = (len(produtos_vendidos_zerados), len(produtos_em_comum))
        if pontuacao > melhor_pontuacao:
            melhor_pedido = pedido
            melhor_pontuacao = pontuacao

    return melhor_pedido


def montar_link_whatsapp_venda(venda):
    cliente = venda.cliente
    if not cliente:
        return ""

    numero = Cliente.normalizar_whatsapp(cliente.whatsapp_normalizado or cliente.whatsapp)
    if not numero:
        return ""

    if len(numero) in (10, 11):
        numero = "55" + numero

    linhas = ["Segue nota em imagem."]
    contexto_pedido_parcial = _contexto_venda_pedido_parcial(venda)
    if contexto_pedido_parcial:
        linhas.extend(["", contexto_pedido_parcial["mensagem"]])
        if contexto_pedido_parcial["itens_pendentes"]:
            linhas.append("Itens pendentes:")
            linhas.extend(
                f"- {item_pendente}"
                for item_pendente in contexto_pedido_parcial["itens_pendentes"]
            )

    mensagem = "\n".join(linhas)
    return f"https://web.whatsapp.com/send?phone={numero}&text={quote(mensagem)}"


def pedidos(request):
    """Listar pedidos com filtros básicos."""
    from .models import Pedido
    
    pedidos_lista = Pedido.objects.select_related("cliente").order_by("-id")
    
    # Filtro por status
    status = request.GET.get("status", "")
    if status:
        pedidos_lista = pedidos_lista.filter(status=status)
    
    # Filtro por cliente
    cliente_id = request.GET.get("cliente_id", "")
    if cliente_id:
        pedidos_lista = pedidos_lista.filter(cliente_id=cliente_id)

    localidade = request.GET.get("localidade", "").strip()
    if localidade:
        pedidos_lista = pedidos_lista.filter(
            Q(cliente__bairro__icontains=localidade) |
            Q(cliente__cidade__icontains=localidade)
        )
    
    # Filtro por data
    data_inicio = request.GET.get("data_inicio", "")
    data_fim = request.GET.get("data_fim", "")
    data_inicio_obj = parse_date(data_inicio) if data_inicio else None
    data_fim_obj = parse_date(data_fim) if data_fim else None
    if data_inicio_obj and data_fim_obj:
        pedidos_lista = pedidos_lista.filter(data_pedido__gte=data_inicio_obj, data_pedido__lte=data_fim_obj)
    elif data_inicio_obj:
        pedidos_lista = pedidos_lista.filter(data_pedido=data_inicio_obj)
    elif data_fim_obj:
        pedidos_lista = pedidos_lista.filter(data_pedido__lte=data_fim_obj)
    
    clientes = Cliente.objects.filter(ativo=True).order_by("nome")
    localidades = []
    localidades_vistas = set()
    for cliente in Cliente.objects.filter(ativo=True).order_by("bairro", "cidade", "nome"):
        for valor in (cliente.bairro, cliente.cidade):
            valor = (valor or "").strip()
            chave = valor.lower()
            if valor and chave not in localidades_vistas:
                localidades.append(valor)
                localidades_vistas.add(chave)
    
    return render(request, "estoque/pedidos_lista.html", {
        "pedidos": pedidos_lista,
        "clientes": clientes,
        "localidades": localidades,
        "status_choices": Pedido.STATUS_CHOICES,
        "status_filtro": status,
        "cliente_filtro": cliente_id,
        "localidade_filtro": localidade,
        "data_inicio": data_inicio,
        "data_fim": data_fim,
    })


def _formatar_decimal_pedido(valor, casas=3):
    texto = f"{Decimal(valor or 0):.{casas}f}".rstrip("0").rstrip(".")
    return texto.replace(".", ",")


def _formatar_moeda_pedido(valor):
    return "R$ " + f"{Decimal(valor or 0):.2f}".replace(".", ",")


def _decimal_pedido(valor, casas, padrao="0"):
    try:
        decimal = Decimal(str(valor if valor is not None else padrao).replace(",", "."))
    except (InvalidOperation, ValueError, TypeError):
        decimal = Decimal(padrao)
    quantizador = Decimal("1").scaleb(-casas)
    return decimal.quantize(quantizador)


def _operadores_pedido_queryset():
    return Funcionario.objects.filter(
        ativo=True,
        pode_operar_sistema=True,
    ).only("id", "nome").order_by("nome")


def _validar_dados_pedido_post(request):
    data_pedido_str = request.POST.get("data_pedido", "")
    cliente_id = request.POST.get("cliente_id", "")
    data_prevista_entrega_str = request.POST.get("data_prevista_entrega", "")
    operador, resposta_operador = _operador_pedido_por_post(request)
    if resposta_operador:
        return None, resposta_operador
    observacao = request.POST.get("observacao", "").strip()

    if not data_pedido_str:
        return None, JsonResponse({"sucesso": False, "mensagem": "Data do pedido é obrigatória."})
    if not cliente_id:
        return None, JsonResponse({"sucesso": False, "mensagem": "Cliente é obrigatório."})

    try:
        data_pedido = parse_date(data_pedido_str)
        if not data_pedido:
            raise ValueError("Data inválida")
    except Exception:
        return None, JsonResponse({"sucesso": False, "mensagem": "Data do pedido inválida."})

    try:
        cliente_id_int = int(cliente_id)
        cliente = Cliente.objects.filter(pk=cliente_id_int, ativo=True).first()
        if not cliente:
            return None, JsonResponse({"sucesso": False, "mensagem": "Cliente inválido."})
    except Exception:
        return None, JsonResponse({"sucesso": False, "mensagem": "Cliente inválido."})

    data_prevista_entrega = None
    if data_prevista_entrega_str:
        data_prevista_entrega = parse_date(data_prevista_entrega_str)

    itens_data = request.POST.get("itens_json", "")
    if not itens_data:
        return None, JsonResponse({"sucesso": False, "mensagem": "Adicione pelo menos um item ao pedido."})

    try:
        itens = json.loads(itens_data)
        if not itens:
            return None, JsonResponse({"sucesso": False, "mensagem": "Adicione pelo menos um item ao pedido."})
    except Exception:
        return None, JsonResponse({"sucesso": False, "mensagem": "Dados de itens inválidos."})

    return {
        "cliente": cliente,
        "data_pedido": data_pedido,
        "data_prevista_entrega": data_prevista_entrega,
        "operador": operador,
        "observacao": observacao,
        "itens": itens,
    }, None


def _operador_pedido_por_post(request):
    operador = request.POST.get("operador", "").strip()
    if not operador:
        return "", None

    if not operador.isdigit():
        return operador, None

    funcionario = _operadores_pedido_queryset().filter(pk=int(operador)).first()
    if not funcionario:
        return "", JsonResponse({
            "sucesso": False,
            "mensagem": "Operador invalido. Selecione um funcionario ativo marcado como operador do sistema.",
        }, status=400)

    return funcionario.nome, None


def _item_pedido_inicial(item):
    produto = item.produto
    return {
        "item_id": item.id,
        "produto_id": produto.id if produto else "",
        "produto_nome": produto.nome if produto else "Produto nao identificado",
        "quantidade": f"{Decimal(item.quantidade or 0):.3f}",
        "unidade": item.unidade or "",
        "preco_unitario": f"{Decimal(item.preco_unitario or 0):.2f}",
        "valor_total": f"{Decimal(item.valor_total or 0):.2f}",
        "estoque_no_momento": item.estoque_no_momento if item.estoque_no_momento is not None else "",
        "observacao": item.observacao or "",
    }


def _salvar_itens_pedido(pedido, itens, item_ids_editaveis=None):
    from .models import ItemPedido

    total = Decimal("0.00")
    itens_validos = []
    ids_editaveis = set(item_ids_editaveis) if item_ids_editaveis is not None else None

    for item in itens:
        try:
            produto_id = int(item.get("produto_id", 0))
        except (TypeError, ValueError):
            continue

        produto = Produto.objects.filter(pk=produto_id, excluido=False).first()
        if not produto:
            continue

        quantidade = _decimal_pedido(item.get("quantidade"), 3)
        preco_unitario = _decimal_pedido(item.get("preco_unitario"), 2)
        if quantidade <= 0 or preco_unitario <= 0:
            continue

        valor_total = (quantidade * preco_unitario).quantize(Decimal("0.01"))
        total += valor_total
        itens_validos.append((item, produto, quantidade, preco_unitario, valor_total))

    if not itens_validos:
        raise ValueError("Adicione pelo menos um item ao pedido.")

    if ids_editaveis is None:
        pedido.itens.all().delete()
    else:
        pedido.itens.exclude(id__in=ids_editaveis).update(
            quantidade=Decimal("0.000"),
            valor_total=Decimal("0.00"),
        )
        enviados = {
            int(item.get("item_id"))
            for item, _produto, _quantidade, _preco_unitario, _valor_total in itens_validos
            if str(item.get("item_id") or "").isdigit()
        }
        ItemPedido.objects.filter(pedido=pedido, id__in=ids_editaveis).exclude(id__in=enviados).delete()

    for item, produto, quantidade, preco_unitario, valor_total in itens_validos:
        item_id = item.get("item_id")
        item_existente = None
        if str(item_id or "").isdigit():
            item_existente = ItemPedido.objects.filter(pedido=pedido, id=int(item_id)).first()
            if ids_editaveis is not None and item_existente and item_existente.id not in ids_editaveis:
                item_existente = None

        dados_item = {
            "produto": produto,
            "quantidade": quantidade,
            "unidade": item.get("unidade", "").strip(),
            "preco_unitario": preco_unitario,
            "valor_total": valor_total,
            "estoque_no_momento": int(produto.quantidade or 0),
            "observacao": item.get("observacao", "").strip() or None,
        }
        if item_existente:
            for campo, valor in dados_item.items():
                setattr(item_existente, campo, valor)
            item_existente.save()
        else:
            ItemPedido.objects.create(pedido=pedido, **dados_item)

    pedido.total = total.quantize(Decimal("0.01"))


def _sugestoes_ultimas_compras_cliente(cliente_id):
    vendas_ids = list(
        Venda.objects.filter(cliente_id=cliente_id, cancelada=False)
        .order_by("-data_venda", "-id")
        .values_list("id", flat=True)[:6]
    )
    if not vendas_ids:
        return []

    sugestoes = {}
    itens = (
        ItemVenda.objects.filter(venda_id__in=vendas_ids, produto__isnull=False)
        .select_related("venda", "produto")
        .order_by("-venda__data_venda", "-venda_id", "id")
    )
    for item in itens:
        produto = item.produto
        chave = produto.id
        if chave not in sugestoes:
            sugestoes[chave] = {
                "produto_id": produto.id,
                "produto": produto.nome,
                "quantidade": _formatar_decimal_pedido(item.quantidade),
                "preco": _formatar_moeda_pedido(item.preco_unitario),
                "preco_valor": str(item.preco_unitario),
                "unidade": produto.unidade_venda_1 or produto.unidade_compra or item.unidade or "",
                "data": item.venda.data_venda.strftime("%d/%m/%Y"),
                "ultima_data_ordem": item.venda.data_venda,
                "ultima_venda_id": item.venda_id,
                "vendas": set(),
                "estoque": produto.quantidade if produto.quantidade is not None else "-",
            }
        sugestoes[chave]["vendas"].add(item.venda_id)

    resultado = []
    for sugestao in sugestoes.values():
        frequencia = len(sugestao["vendas"])
        resultado.append({
            "produto_id": sugestao["produto_id"],
            "produto": sugestao["produto"],
            "quantidade": sugestao["quantidade"],
            "preco": sugestao["preco"],
            "preco_valor": sugestao["preco_valor"],
            "unidade": sugestao["unidade"],
            "data": sugestao["data"],
            "frequencia": frequencia,
            "estoque": sugestao["estoque"],
            "_ultima_data_ordem": sugestao["ultima_data_ordem"],
            "_ultima_venda_id": sugestao["ultima_venda_id"],
        })

    resultado.sort(key=lambda item: (item["frequencia"], item["_ultima_data_ordem"], item["_ultima_venda_id"]), reverse=True)
    for item in resultado:
        item.pop("_ultima_data_ordem", None)
        item.pop("_ultima_venda_id", None)
    return resultado


def pedido_criar(request):
    """Criar novo pedido."""
    from .models import Pedido, ItemPedido

    sugestoes_cliente_id = request.GET.get("sugestoes_cliente_id")
    if request.method == "GET" and sugestoes_cliente_id:
        try:
            cliente_id = int(sugestoes_cliente_id)
        except (TypeError, ValueError):
            return JsonResponse({"sugestoes": []})
        return JsonResponse({"sugestoes": _sugestoes_ultimas_compras_cliente(cliente_id)})
    
    if request.method == "POST":
        try:
            data_pedido_str = request.POST.get("data_pedido", "")
            cliente_id = request.POST.get("cliente_id", "")
            data_prevista_entrega_str = request.POST.get("data_prevista_entrega", "")
            operador, resposta_operador = _operador_pedido_por_post(request)
            if resposta_operador:
                return resposta_operador
            observacao = request.POST.get("observacao", "").strip()
            
            # Validar dados obrigatórios
            if not data_pedido_str:
                return JsonResponse({"sucesso": False, "mensagem": "Data do pedido é obrigatória."})
            if not cliente_id:
                return JsonResponse({"sucesso": False, "mensagem": "Cliente é obrigatório."})
            
            try:
                data_pedido = parse_date(data_pedido_str)
                if not data_pedido:
                    raise ValueError("Data inválida")
            except:
                return JsonResponse({"sucesso": False, "mensagem": "Data do pedido inválida."})
            
            try:
                cliente_id_int = int(cliente_id)
                cliente = Cliente.objects.filter(pk=cliente_id_int, ativo=True).first()
                if not cliente:
                    return JsonResponse({"sucesso": False, "mensagem": "Cliente inválido."})
            except:
                return JsonResponse({"sucesso": False, "mensagem": "Cliente inválido."})
            
            data_prevista_entrega = None
            if data_prevista_entrega_str:
                try:
                    data_prevista_entrega = parse_date(data_prevista_entrega_str)
                except:
                    pass
            
            # Obter itens do POST
            itens_data = request.POST.get("itens_json", "")
            if not itens_data:
                return JsonResponse({"sucesso": False, "mensagem": "Adicione pelo menos um item ao pedido."})
            
            try:
                itens = json.loads(itens_data)
                if not itens:
                    return JsonResponse({"sucesso": False, "mensagem": "Adicione pelo menos um item ao pedido."})
            except:
                return JsonResponse({"sucesso": False, "mensagem": "Dados de itens inválidos."})
            
            # Criar pedido
            with transaction.atomic():
                total = Decimal(0)
                for item in itens:
                    try:
                        valor_total = Decimal(str(item.get("valor_total", 0)))
                        total += valor_total
                    except:
                        pass
                
                pedido = Pedido.objects.create(
                    cliente=cliente,
                    data_pedido=data_pedido,
                    data_prevista_entrega=data_prevista_entrega,
                    operador=operador,
                    observacao=observacao or None,
                    total=total,
                )
                
                # Criar itens do pedido
                for item in itens:
                    try:
                        produto_id = int(item.get("produto_id", 0))
                        produto = Produto.objects.filter(pk=produto_id, excluido=False).first()
                        if not produto:
                            continue
                        
                        quantidade = Decimal(str(item.get("quantidade", 0)))
                        unidade = item.get("unidade", "").strip()
                        preco_unitario = Decimal(str(item.get("preco_unitario", 0)))
                        valor_total = Decimal(str(item.get("valor_total", 0)))
                        estoque_no_momento = int(produto.quantidade or 0)
                        observacao_item = item.get("observacao", "").strip()
                        
                        ItemPedido.objects.create(
                            pedido=pedido,
                            produto=produto,
                            quantidade=quantidade,
                            unidade=unidade,
                            preco_unitario=preco_unitario,
                            valor_total=valor_total,
                            estoque_no_momento=estoque_no_momento,
                            observacao=observacao_item or None,
                        )
                    except Exception as e:
                        logger.exception(f"Erro ao criar item do pedido: {e}")
                        continue
            
            return JsonResponse({
                "sucesso": True,
                "pedido_id": pedido.id,
                "mensagem": f"Pedido #{pedido.id} criado com sucesso.",
                "redirect_url": (
                    f"{reverse('estoque:vendas')}?pedido_id={pedido.id}"
                    if request.POST.get("proxima_acao") == "enviar_venda"
                    else reverse("estoque:pedido_detalhe", args=[pedido.id])
                ),
            })
        
        except Exception as e:
            logger.exception(f"Erro ao criar pedido: {e}")
            return JsonResponse({"sucesso": False, "mensagem": "Erro ao criar pedido."}, status=500)
    
    # GET: mostrar formulário de criação
    produtos = Produto.objects.filter(excluido=False).order_by("nome")
    clientes = Cliente.objects.filter(ativo=True).order_by("nome")
    operadores_pedido = list(_operadores_pedido_queryset())

    return render(request, "estoque/pedido_criar.html", {
        "produtos": produtos,
        "clientes": clientes,
        "operadores_pedido": operadores_pedido,
    })


def pedido_editar(request, pk):
    """Editar pedido aberto ou saldo pendente de pedido parcial."""
    from .models import Pedido

    pedido = get_object_or_404(
        Pedido.objects.select_related("cliente").prefetch_related("itens__produto"),
        pk=pk,
    )

    if pedido.status == Pedido.STATUS_CONVERTIDO_EM_VENDA:
        if request.method == "POST":
            return JsonResponse(
                {
                    "sucesso": False,
                    "mensagem": "Pedido totalmente convertido em venda nao pode ser editado livremente.",
                },
                status=400,
            )
        messages.warning(request, "Pedido totalmente convertido em venda nao pode ser editado livremente.")
        return redirect("estoque:pedido_detalhe", pk=pedido.pk)

    if pedido.status not in [Pedido.STATUS_ABERTO, Pedido.STATUS_PARCIAL]:
        if request.method == "POST":
            return JsonResponse(
                {"sucesso": False, "mensagem": "Este pedido nao esta disponivel para edicao."},
                status=400,
            )
        messages.warning(request, "Este pedido nao esta disponivel para edicao.")
        return redirect("estoque:pedido_detalhe", pk=pedido.pk)

    itens_pedido = list(pedido.itens.all())
    if pedido.status == Pedido.STATUS_PARCIAL:
        itens_editaveis, _total_pendente = _itens_pendentes_exibicao_pedido_parcial(pedido, itens_pedido)
    else:
        itens_editaveis = itens_pedido
    item_ids_editaveis = {item.id for item in itens_editaveis if item.id}

    sugestoes_cliente_id = request.GET.get("sugestoes_cliente_id")
    if request.method == "GET" and sugestoes_cliente_id:
        try:
            cliente_id = int(sugestoes_cliente_id)
        except (TypeError, ValueError):
            return JsonResponse({"sugestoes": []})
        return JsonResponse({"sugestoes": _sugestoes_ultimas_compras_cliente(cliente_id)})

    if request.method == "POST":
        dados, resposta_erro = _validar_dados_pedido_post(request)
        if resposta_erro:
            return resposta_erro

        try:
            with transaction.atomic():
                pedido = Pedido.objects.select_for_update().get(pk=pedido.pk)
                if pedido.status == Pedido.STATUS_CONVERTIDO_EM_VENDA:
                    return JsonResponse(
                        {
                            "sucesso": False,
                            "mensagem": "Pedido totalmente convertido em venda nao pode ser editado livremente.",
                        },
                        status=400,
                    )
                if pedido.status not in [Pedido.STATUS_ABERTO, Pedido.STATUS_PARCIAL]:
                    return JsonResponse(
                        {"sucesso": False, "mensagem": "Este pedido nao esta disponivel para edicao."},
                        status=400,
                    )

                pedido.cliente = dados["cliente"]
                pedido.data_pedido = dados["data_pedido"]
                pedido.data_prevista_entrega = dados["data_prevista_entrega"]
                pedido.operador = dados["operador"]
                pedido.observacao = dados["observacao"] or None
                ids_para_editar = None if pedido.status == Pedido.STATUS_ABERTO else item_ids_editaveis
                _salvar_itens_pedido(pedido, dados["itens"], ids_para_editar)
                pedido.save(
                    update_fields=[
                        "cliente",
                        "data_pedido",
                        "data_prevista_entrega",
                        "operador",
                        "observacao",
                        "total",
                        "atualizado_em",
                    ]
                )
        except ValueError as exc:
            return JsonResponse({"sucesso": False, "mensagem": str(exc)}, status=400)
        except Exception as exc:
            logger.exception(f"Erro ao editar pedido: {exc}")
            return JsonResponse({"sucesso": False, "mensagem": "Erro ao editar pedido."}, status=500)

        return JsonResponse({
            "sucesso": True,
            "pedido_id": pedido.id,
            "mensagem": f"Pedido #{pedido.id} atualizado com sucesso.",
            "redirect_url": f"{reverse('estoque:pedido_detalhe', args=[pedido.id])}?pedido_editado=1",
        })

    produtos = Produto.objects.filter(excluido=False).order_by("nome")
    clientes = Cliente.objects.filter(ativo=True).order_by("nome")
    operadores_pedido = list(_operadores_pedido_queryset())
    operador_atual_habilitado = bool(
        pedido.operador and any(funcionario.nome == pedido.operador for funcionario in operadores_pedido)
    )

    return render(request, "estoque/pedido_criar.html", {
        "produtos": produtos,
        "clientes": clientes,
        "operadores_pedido": operadores_pedido,
        "operador_atual_habilitado": operador_atual_habilitado,
        "pedido": pedido,
        "pedido_modo_edicao": True,
        "pedido_itens_iniciais": [_item_pedido_inicial(item) for item in itens_editaveis],
        "pedido_url_salvar": reverse("estoque:pedido_editar", args=[pedido.id]),
    })


@require_POST
def pedido_cancelar(request, pk):
    """Cancelar pedido sem apagar o registro nem os itens."""
    from .models import Pedido

    with transaction.atomic():
        pedido = get_object_or_404(Pedido.objects.select_for_update(), pk=pk)

        if pedido.status == Pedido.STATUS_CANCELADO:
            messages.warning(request, f"Pedido #{pedido.id} ja esta cancelado.")
            return redirect("estoque:pedido_detalhe", pk=pedido.pk)

        if pedido.status == Pedido.STATUS_CONVERTIDO_EM_VENDA:
            messages.warning(
                request,
                "Pedido totalmente convertido em venda nao pode ser cancelado livremente.",
            )
            return redirect("estoque:pedido_detalhe", pk=pedido.pk)

        if pedido.status not in [Pedido.STATUS_ABERTO, Pedido.STATUS_PARCIAL]:
            messages.warning(request, "Este pedido nao esta disponivel para cancelamento.")
            return redirect("estoque:pedido_detalhe", pk=pedido.pk)

        pedido.status = Pedido.STATUS_CANCELADO
        pedido.save(update_fields=["status", "atualizado_em"])

    messages.success(request, f"Pedido #{pedido.id} cancelado com sucesso. Histórico preservado.")
    return redirect(f"{reverse('estoque:pedido_detalhe', args=[pedido.id])}?pedido_cancelado=1")


def _itens_pendentes_exibicao_pedido_parcial(pedido, itens_pedido):
    itens_positivos = [item for item in itens_pedido if item.quantidade > 0]
    total_positivo = sum((item.valor_total for item in itens_positivos), Decimal("0.00"))

    if not pedido.cliente_id or not itens_positivos:
        return itens_positivos, total_positivo

    # Pedidos parciais novos ja carregam o saldo pendente nos ItemPedido.
    # Este fallback atende pedidos parciais antigos desta etapa, que ficaram com
    # os itens originais apesar de uma venda parcial ja ter sido gravada.
    if any(item.quantidade <= 0 for item in itens_pedido):
        return itens_positivos, total_positivo

    candidatos = (
        Venda.objects.prefetch_related("itens__produto")
        .filter(
            cliente_id=pedido.cliente_id,
            cancelada=False,
            total__lt=pedido.total,
            criado_em__gte=pedido.criado_em,
            criado_em__lte=pedido.atualizado_em + timedelta(minutes=5),
        )
        .order_by("-id")[:10]
    )
    itens_por_produto = {
        item.produto_id: item
        for item in itens_pedido
        if item.produto_id
    }

    for venda in candidatos:
        vendidos_por_produto = {}
        venda_compativel = True
        for item_venda in venda.itens.all():
            item_pedido = itens_por_produto.get(item_venda.produto_id)
            if (
                not item_pedido
                or item_venda.preco_unitario != item_pedido.preco_unitario
                or item_venda.quantidade > item_pedido.quantidade
            ):
                venda_compativel = False
                break
            vendidos_por_produto[item_venda.produto_id] = (
                vendidos_por_produto.get(item_venda.produto_id, Decimal("0.000"))
                + item_venda.quantidade
            )

        if not venda_compativel or not vendidos_por_produto:
            continue

        itens_pendentes = []
        total_pendente = Decimal("0.00")
        for item_pedido in itens_pedido:
            quantidade_vendida = vendidos_por_produto.get(item_pedido.produto_id, Decimal("0.000"))
            quantidade_pendente = max(item_pedido.quantidade - quantidade_vendida, Decimal("0.000"))
            if quantidade_pendente <= 0:
                continue

            item_exibicao = copy.copy(item_pedido)
            item_exibicao.quantidade = quantidade_pendente.quantize(Decimal("0.001"))
            item_exibicao.valor_total = (
                item_exibicao.quantidade * item_exibicao.preco_unitario
            ).quantize(Decimal("0.01"))
            itens_pendentes.append(item_exibicao)
            total_pendente += item_exibicao.valor_total

        return itens_pendentes, total_pendente.quantize(Decimal("0.01"))

    return itens_positivos, total_positivo


def pedido_detalhe(request, pk):
    """Mostrar detalhe do pedido."""
    from .models import Pedido
    
    pedido = get_object_or_404(
        Pedido.objects.select_related("cliente").prefetch_related("itens__produto"),
        pk=pk,
    )
    itens_pedido = list(pedido.itens.all())
    if pedido.status == Pedido.STATUS_PARCIAL:
        itens_exibidos, total_exibido = _itens_pendentes_exibicao_pedido_parcial(pedido, itens_pedido)
        titulo_itens = "Itens pendentes do pedido"
        rotulo_total = "Total pendente"
    else:
        itens_exibidos = itens_pedido
        total_exibido = pedido.total
        titulo_itens = "Itens do Pedido"
        rotulo_total = "Total do Pedido"

    for item in itens_exibidos:
        item.quantidade_formatada = _formatar_decimal_pedido(item.quantidade)
    
    return render(request, "estoque/pedido_detalhe.html", {
        "pedido": pedido,
        "itens_exibidos": itens_exibidos,
        "total_exibido": total_exibido,
        "titulo_itens": titulo_itens,
        "rotulo_total": rotulo_total,
        "pode_editar_pedido": pedido.status in [Pedido.STATUS_ABERTO, Pedido.STATUS_PARCIAL],
        "pode_cancelar_pedido": pedido.status in [Pedido.STATUS_ABERTO, Pedido.STATUS_PARCIAL],
        "pedido_editado": request.GET.get("pedido_editado") == "1",
        "pedido_cancelado": request.GET.get("pedido_cancelado") == "1",
    })


def contas_pagar(request):
    termo = request.GET.get("q", "").strip()
    status = request.GET.get("status", "abertas").strip()

    contas_base = (
        ContaPagar.objects
        .select_related("fornecedor", "compra")
        .prefetch_related(Prefetch("pagamentos", queryset=PagamentoContaPagar.objects.order_by("-data_pagamento", "-id")))
        .order_by("data_vencimento", "id")
    )

    if termo:
        contas_base = contas_base.filter(
            Q(fornecedor__nome__icontains=termo)
            | Q(compra__id__icontains=termo)
            | Q(observacao__icontains=termo)
        )

    if status == "abertas":
        contas = contas_base.filter(status__in=[ContaPagar.STATUS_ABERTA, ContaPagar.STATUS_PARCIAL])
    elif status in {ContaPagar.STATUS_ABERTA, ContaPagar.STATUS_PARCIAL, ContaPagar.STATUS_PAGA, ContaPagar.STATUS_CANCELADA}:
        contas = contas_base.filter(status=status)
    else:
        status = "todas"
        contas = contas_base

    contas_abertas = contas.filter(status__in=[ContaPagar.STATUS_ABERTA, ContaPagar.STATUS_PARCIAL])
    total_aberto = contas_abertas.aggregate(total=Sum("valor_em_aberto")).get("total") or Decimal("0.00")
    total_original = contas.aggregate(total=Sum("valor_original")).get("total") or Decimal("0.00")

    return render(
        request,
        "estoque/contas_pagar.html",
        {
            "contas": contas,
            "termo": termo,
            "status": status,
            "total_aberto": total_aberto,
            "total_original": total_original,
            "status_choices": ContaPagar.STATUS_CHOICES,
        },
    )


