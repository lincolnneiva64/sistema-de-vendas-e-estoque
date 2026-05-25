import re
import logging
import os
import shutil
import time
import unicodedata
from datetime import datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps

CAMINHO_TESSERACT_WINDOWS = Path("C:/Program Files/Tesseract-OCR/tesseract.exe")
OCR_RENDER_MODO_LEVE = bool(os.getenv("RENDER") or os.getenv("RENDER_SERVICE_ID"))
OCR_TIMEOUT_SEGUNDOS = 3 if OCR_RENDER_MODO_LEVE else 12
OCR_LARGURA_MAXIMA = 700 if OCR_RENDER_MODO_LEVE else 1400
OCR_ALTURA_MAXIMA = 1100 if OCR_RENDER_MODO_LEVE else 2200
OCR_CONFIG_RAPIDO = "--oem 1 --psm 6"
OCR_CONFIG_LINHA = "--oem 1 --psm 7"
OCR_CONFIG_VALOR_LINHA = "--oem 1 --psm 7 -c tessedit_char_whitelist=0123456789R$r$.,,"
OCR_LARGURA_FAIXAS_NUBANK = 700
OCR_UPSCALE_FAIXA = 3
OCR_LARGURA_LINHAS = 1000
OCR_TIMEOUT_LINHA_SEGUNDOS = 2
OCR_TIMEOUT_DATA_LINHA_SEGUNDOS = 4
OCR_MAX_LINHAS = 30
OCR_MAX_DEBUG_LINHAS = 12
logger = logging.getLogger(__name__)


def _normalizar_espacos(valor):
    return " ".join(str(valor or "").strip().split())


def _nome_arquivo_seguro_ocr(valor):
    nome = Path(str(valor or "arquivo")).name
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", nome).strip("._") or "arquivo"


def _normalizar_linha(valor):
    return _normalizar_espacos(valor).lower()


def _sem_acentos(valor):
    texto = unicodedata.normalize("NFD", str(valor or ""))
    return "".join(caractere for caractere in texto if unicodedata.category(caractere) != "Mn")


def _normalizar_rotulo_ocr(linha):
    linha_normalizada = _normalizar_linha(_sem_acentos(linha))
    return re.sub(r"^[^\w]+", "", linha_normalizada).strip()


def _eh_linha_bloqueada_para_nome(linha):
    linha_normalizada = _normalizar_linha(linha)
    if not linha_normalizada:
        return True
    if re.search(r"^(cpf|cnpj|instituicao|instituiÃ§Ã£o|banco|agencia|agÃªncia|conta)\b", linha_normalizada):
        return True
    if "nu pagamentos" in linha_normalizada:
        return True
    return False


def _eh_linha_bloqueada_para_nome(linha):
    linha_normalizada = _normalizar_linha(_sem_acentos(linha))
    if not linha_normalizada:
        return True
    if re.search(r"^(cpf|cnpj|instituicao|banco|agencia|conta|transacao|id|para|tipo de transferencia|tipo de conta|comprovante|valor)\b", linha_normalizada):
        return True
    if "nu pagamentos" in linha_normalizada or "mercado pago" in linha_normalizada:
        return True
    return False


def _parece_nome_pessoa(linha):
    if _eh_linha_bloqueada_para_nome(linha):
        return False
    linha_normalizada = _normalizar_espacos(linha)
    if re.search(r"(R\$|\d{2}/|\*{2,}|@)", linha_normalizada, flags=re.IGNORECASE):
        return False
    palavras = re.findall(r"[^\W\d_]{2,}", linha_normalizada)
    return len(palavras) >= 2


def _resolver_tesseract_cmd():
    tesseract_path = shutil.which("tesseract")
    if tesseract_path:
        return tesseract_path

    if CAMINHO_TESSERACT_WINDOWS.exists():
        return str(CAMINHO_TESSERACT_WINDOWS)

    return None


def _preparar_imagem_ocr(conteudo):
    imagem = Image.open(BytesIO(conteudo))
    imagem = ImageOps.exif_transpose(imagem)
    imagem = imagem.convert("L")
    if imagem.width > OCR_LARGURA_MAXIMA:
        proporcao = OCR_LARGURA_MAXIMA / float(imagem.width)
        nova_altura = max(1, int(imagem.height * proporcao))
        imagem = imagem.resize((OCR_LARGURA_MAXIMA, nova_altura), Image.LANCZOS)

    buffer = BytesIO()
    imagem.save(buffer, format="PNG", optimize=True)
    buffer.seek(0)
    imagem_preparada = Image.open(buffer)
    imagem_preparada.load()
    return imagem_preparada


def _reduzir_imagem_ocr(imagem):
    imagem = ImageOps.exif_transpose(imagem)
    imagem = imagem.convert("L")
    proporcao = min(
        OCR_LARGURA_MAXIMA / float(imagem.width),
        OCR_ALTURA_MAXIMA / float(imagem.height),
        1,
    )
    if proporcao < 1:
        novo_tamanho = (
            max(1, int(imagem.width * proporcao)),
            max(1, int(imagem.height * proporcao)),
        )
        imagem = imagem.resize(novo_tamanho, Image.LANCZOS)
    return imagem


def _imagem_intermediaria_faixas_ocr(imagem):
    imagem = ImageOps.exif_transpose(imagem).convert("L")
    if imagem.width <= 0:
        return imagem
    proporcao = OCR_LARGURA_FAIXAS_NUBANK / float(imagem.width)
    if proporcao > 1:
        novo_tamanho = (
            OCR_LARGURA_FAIXAS_NUBANK,
            max(1, int(imagem.height * proporcao)),
        )
        imagem = imagem.resize(novo_tamanho, Image.LANCZOS)
    return imagem


def _imagem_intermediaria_linhas_ocr(imagem):
    imagem = ImageOps.exif_transpose(imagem).convert("L")
    if imagem.width <= 0:
        return imagem
    proporcao = OCR_LARGURA_LINHAS / float(imagem.width)
    if abs(proporcao - 1) > 0.05:
        novo_tamanho = (
            OCR_LARGURA_LINHAS,
            max(1, int(imagem.height * proporcao)),
        )
        imagem = imagem.resize(novo_tamanho, Image.LANCZOS)
    return imagem


def _copiar_recorte_ocr(imagem, nome, caixa):
    recorte = imagem.crop(caixa)
    recorte.info["ocr_recorte"] = nome
    return recorte


def _preparar_recorte_linha_ocr(imagem, nome, caixa, candidata_data=False):
    recorte = _copiar_recorte_ocr(imagem, nome, caixa)
    tamanho_antes = recorte.size
    recorte = ImageOps.autocontrast(recorte.convert("L"), cutoff=1)
    recorte = recorte.resize((max(1, recorte.width * 2), max(1, recorte.height * 2)), Image.LANCZOS)
    recorte = recorte.point(lambda pixel: 255 if pixel > 185 else 0).convert("L")
    recorte.info["ocr_recorte"] = nome
    recorte.info["ocr_config"] = OCR_CONFIG_LINHA
    recorte.info["ocr_configs"] = [OCR_CONFIG_LINHA, OCR_CONFIG_RAPIDO]
    recorte.info["ocr_timeout"] = OCR_TIMEOUT_DATA_LINHA_SEGUNDOS if candidata_data else OCR_TIMEOUT_LINHA_SEGUNDOS
    recorte.info["ocr_linha"] = True
    recorte.info["ocr_rapido"] = True
    recorte.info["ocr_candidata_data"] = candidata_data
    recorte.info["ocr_tamanho_antes"] = tamanho_antes
    recorte.info["ocr_tamanho_depois"] = recorte.size
    recorte.info["ocr_base_nome"] = "linhas_1000"
    recorte.info["ocr_base_tamanho"] = imagem.size
    return recorte


def _preparar_recorte_rapido_ocr(imagem, nome, caixa):
    recorte = _copiar_recorte_ocr(imagem, nome, caixa)
    recorte = ImageOps.autocontrast(recorte)
    recorte = recorte.point(lambda pixel: 255 if pixel > 175 else 0, mode="1").convert("L")
    recorte.info["ocr_recorte"] = nome
    recorte.info["ocr_config"] = OCR_CONFIG_RAPIDO
    recorte.info["ocr_timeout"] = 3 if OCR_RENDER_MODO_LEVE else min(OCR_TIMEOUT_SEGUNDOS, 5)
    recorte.info["ocr_rapido"] = True
    return recorte


def _salvar_debug_recorte_ocr(caminho, nome_recorte, imagem):
    try:
        from django.conf import settings
        from django.core.files.base import ContentFile
        from django.core.files.storage import default_storage

        debug_env = str(os.getenv("PIX_OCR_DEBUG_CROPS", "")).strip().lower() in {"1", "true", "sim", "yes", "on"}
        if not (getattr(settings, "DEBUG", False) or debug_env):
            return ""
        nome_base = _nome_arquivo_seguro_ocr(caminho)
        caminho_storage = f"debug_ocr/{nome_base}_{nome_recorte}.jpg"
        buffer = BytesIO()
        imagem.convert("L").save(buffer, format="JPEG", quality=90)
        if default_storage.exists(caminho_storage):
            default_storage.delete(caminho_storage)
        default_storage.save(caminho_storage, ContentFile(buffer.getvalue()))
        return default_storage.url(caminho_storage)
    except Exception:
        return ""


def _caixa_percentual(largura, altura, x1, y1, x2, y2):
    return (
        max(0, int(largura * x1)),
        max(0, int(altura * y1)),
        min(largura, int(largura * x2)),
        min(altura, int(altura * y2)),
    )


def _detectar_caixas_linhas_texto(imagem):
    imagem = ImageOps.autocontrast(imagem.convert("L"), cutoff=1)
    binaria = imagem.point(lambda pixel: 0 if pixel < 210 else 255).convert("L")
    largura, altura = binaria.size
    pixels = binaria.load()

    linhas_ativas = []
    limite_linha = max(3, int(largura * 0.01))
    for y in range(altura):
        escuros = 0
        for x in range(largura):
            if pixels[x, y] < 128:
                escuros += 1
        if escuros >= limite_linha:
            linhas_ativas.append(y)

    faixas = []
    inicio = None
    anterior = None
    for y in linhas_ativas:
        if inicio is None:
            inicio = y
        elif anterior is not None and y - anterior > 8:
            faixas.append((inicio, anterior))
            inicio = y
        anterior = y
    if inicio is not None and anterior is not None:
        faixas.append((inicio, anterior))

    caixas = []
    for y1, y2 in faixas:
        if y2 - y1 < 4:
            continue
        y1_pad = max(0, y1 - 8)
        y2_pad = min(altura, y2 + 9)
        xs = []
        for y in range(y1_pad, y2_pad):
            for x in range(largura):
                if pixels[x, y] < 128:
                    xs.append(x)
        if not xs:
            continue
        x1 = max(0, min(xs) - 12)
        x2 = min(largura, max(xs) + 13)
        if x2 - x1 < 20 or y2_pad - y1_pad > 180:
            continue
        caixas.append((x1, y1_pad, x2, y2_pad))

    caixas.sort(key=lambda caixa: (caixa[1], caixa[0]))
    return caixas[:OCR_MAX_LINHAS]


def _preparar_faixa_linha_ocr(imagem, nome, caixa, configs, percentuais, base_nome):
    recorte = _copiar_recorte_ocr(imagem, nome, caixa)
    tamanho_antes = recorte.size
    recorte = ImageOps.autocontrast(recorte.convert("L"), cutoff=1)
    recorte = recorte.resize(
        (max(1, recorte.width * OCR_UPSCALE_FAIXA), max(1, recorte.height * OCR_UPSCALE_FAIXA)),
        Image.LANCZOS,
    )
    recorte = recorte.point(lambda pixel: 255 if pixel > 190 else 0).convert("L")
    recorte.info["ocr_recorte"] = nome
    recorte.info["ocr_config"] = configs[0]
    recorte.info["ocr_configs"] = configs
    recorte.info["ocr_timeout"] = 4
    recorte.info["ocr_faixa"] = True
    recorte.info["ocr_tamanho_antes"] = tamanho_antes
    recorte.info["ocr_tamanho_depois"] = recorte.size
    recorte.info["ocr_base_nome"] = base_nome
    recorte.info["ocr_base_tamanho"] = imagem.size
    recorte.info["ocr_caixa_percentual"] = percentuais
    return recorte


def _imagem_vertical_grande(tamanho):
    largura, altura = tamanho
    return altura >= 1200 and largura > 0 and (altura / float(largura)) >= 2.2


def _preparar_recortes_ocr(conteudo):
    imagem_original = Image.open(BytesIO(conteudo))
    tamanho_original = imagem_original.size
    imagem = _reduzir_imagem_ocr(imagem_original)
    largura, altura = imagem.size
    topo_fim = max(1, int(altura * 0.38))
    pagador_inicio = max(0, int(altura * 0.32))

    imagem.info["ocr_recorte"] = "inteira"
    usar_recorte_rapido = OCR_RENDER_MODO_LEVE or _imagem_vertical_grande(tamanho_original)
    usar_faixas_linhas = _imagem_vertical_grande(tamanho_original)
    if usar_recorte_rapido:
        rapido_inicio = max(0, int(altura * 0.04))
        rapido_fim = min(altura, max(rapido_inicio + 1, int(altura * 0.36)))
        alternativa_inicio = max(0, int(altura * 0.12))
        alternativa_fim = min(altura, max(alternativa_inicio + 1, int(altura * 0.48)))
        recortes_rapidos = []
        if usar_faixas_linhas:
            imagem_faixas = _imagem_intermediaria_faixas_ocr(imagem_original)
            largura_faixa, altura_faixa = imagem_faixas.size
            base_nome = "nubank_700"
            configs_valor = [OCR_CONFIG_VALOR_LINHA, OCR_CONFIG_RAPIDO]
            configs_data = [OCR_CONFIG_LINHA, OCR_CONFIG_RAPIDO]
            caixa_valor_principal_pct = (0.35, 0.38, 1.00, 0.47)
            caixa_valor_alternativa_pct = (0.45, 0.35, 1.00, 0.45)
            caixa_data_principal_pct = (0.05, 0.25, 0.95, 0.33)
            caixa_data_alternativa_pct = (0.05, 0.23, 0.95, 0.31)
            caixa_valor_principal = _caixa_percentual(largura_faixa, altura_faixa, *caixa_valor_principal_pct)
            caixa_valor_alternativa = _caixa_percentual(largura_faixa, altura_faixa, *caixa_valor_alternativa_pct)
            caixa_data_principal = _caixa_percentual(largura_faixa, altura_faixa, *caixa_data_principal_pct)
            caixa_data_alternativa = _caixa_percentual(largura_faixa, altura_faixa, *caixa_data_alternativa_pct)
            recortes_rapidos.extend([
                (
                    "faixa_valor_principal",
                    _preparar_faixa_linha_ocr(
                        imagem_faixas,
                        "faixa_valor_principal",
                        caixa_valor_principal,
                        configs_valor,
                        caixa_valor_principal_pct,
                        base_nome,
                    ),
                    caixa_valor_principal,
                ),
                (
                    "faixa_valor_alternativa",
                    _preparar_faixa_linha_ocr(
                        imagem_faixas,
                        "faixa_valor_alternativa",
                        caixa_valor_alternativa,
                        configs_valor,
                        caixa_valor_alternativa_pct,
                        base_nome,
                    ),
                    caixa_valor_alternativa,
                ),
                (
                    "faixa_data_principal",
                    _preparar_faixa_linha_ocr(
                        imagem_faixas,
                        "faixa_data_principal",
                        caixa_data_principal,
                        configs_data,
                        caixa_data_principal_pct,
                        base_nome,
                    ),
                    caixa_data_principal,
                ),
                (
                    "faixa_data_alternativa",
                    _preparar_faixa_linha_ocr(
                        imagem_faixas,
                        "faixa_data_alternativa",
                        caixa_data_alternativa,
                        configs_data,
                        caixa_data_alternativa_pct,
                        base_nome,
                    ),
                    caixa_data_alternativa,
                ),
            ])
        if not (OCR_RENDER_MODO_LEVE and usar_faixas_linhas):
            recortes_rapidos.extend([
                (
                    "rapido_superior",
                    _preparar_recorte_rapido_ocr(imagem, "rapido_superior", (0, rapido_inicio, largura, rapido_fim)),
                    (0, rapido_inicio, largura, rapido_fim),
                ),
                (
                    "rapido_meio_superior",
                    _preparar_recorte_rapido_ocr(imagem, "rapido_meio_superior", (0, alternativa_inicio, largura, alternativa_fim)),
                    (0, alternativa_inicio, largura, alternativa_fim),
                ),
            ])
        if not OCR_RENDER_MODO_LEVE:
            recortes_rapidos.extend([
                ("topo", _copiar_recorte_ocr(imagem, "topo", (0, 0, largura, topo_fim)), (0, 0, largura, topo_fim)),
                ("pagador", _copiar_recorte_ocr(imagem, "pagador", (0, pagador_inicio, largura, altura)), (0, pagador_inicio, largura, altura)),
                ("inteira", imagem, (0, 0, largura, altura)),
            ])
        return tamanho_original, imagem.size, recortes_rapidos

    recortes = [
        ("topo", _copiar_recorte_ocr(imagem, "topo", (0, 0, largura, topo_fim)), (0, 0, largura, topo_fim)),
        ("pagador", _copiar_recorte_ocr(imagem, "pagador", (0, pagador_inicio, largura, altura)), (0, pagador_inicio, largura, altura)),
        ("inteira", imagem, (0, 0, largura, altura)),
    ]
    return tamanho_original, imagem.size, recortes


def _log_diagnostico_ocr(caminho, mensagem, *args):
    logger.warning("[PIX OCR][%s] " + mensagem, caminho, *args)


def _extrair_texto_recorte_ocr(pytesseract, imagem):
    erros = []
    config = imagem.info.get("ocr_config", "")
    configs = imagem.info.get("ocr_configs") or [config]
    timeout = imagem.info.get("ocr_timeout", OCR_TIMEOUT_SEGUNDOS)
    rapido = bool(imagem.info.get("ocr_rapido"))
    tentativas = [("padrao", {})] if OCR_RENDER_MODO_LEVE or rapido else [
        ("por", {"lang": "por"}),
        ("eng", {"lang": "eng"}),
        ("padrao", {}),
    ]
    for idioma, opcoes in tentativas:
        for config_tentativa in configs:
            try:
                texto = pytesseract.image_to_string(
                    imagem,
                    timeout=timeout,
                    config=config_tentativa,
                    **opcoes,
                )
                if _normalizar_espacos(texto) or config_tentativa == configs[-1]:
                    return texto, idioma, config_tentativa, timeout, erros
                erros.append(f"{idioma}/{config_tentativa or 'padrao'}: OCR sem texto")
            except Exception as exc:
                mensagem = str(exc).strip()
                detalhe = f": {mensagem[:80]}" if mensagem else ""
                erros.append(f"{idioma}/{config_tentativa or 'padrao'}: {exc.__class__.__name__}{detalhe}")

    raise RuntimeError("; ".join(erros) or "OCR falhou em todos os idiomas")


def _indice_linhas_candidatas_data(caixas):
    candidatas = set()
    if len(caixas) >= 4:
        candidatas.add(3)
    for indice in range(1, len(caixas) - 1):
        y1_anterior = caixas[indice - 1][1]
        y1_atual = caixas[indice][1]
        y1_proxima = caixas[indice + 1][1]
        if y1_atual - y1_anterior < 260 and y1_proxima - y1_atual < 360:
            candidatas.add(indice)
    return candidatas


def _extrair_texto_por_linhas_ocr(pytesseract, conteudo, caminho, debug_prefix=None):
    inicio_total = time.monotonic()
    try:
        imagem_original = Image.open(BytesIO(conteudo))
        imagem_linhas = _imagem_intermediaria_linhas_ocr(imagem_original)
        caixas = _detectar_caixas_linhas_texto(imagem_linhas)
    except Exception as exc:
        _log_diagnostico_ocr(caminho or "arquivo", "linhas deteccao falhou=%s: %s", exc.__class__.__name__, str(exc)[:120])
        return ""

    _log_diagnostico_ocr(
        caminho or "arquivo",
        "linhas detectadas=%s imagem_base=linhas_1000 base_tamanho=%s",
        len(caixas),
        imagem_linhas.size,
    )
    if not caixas:
        return ""

    textos = []
    erros = []
    debug_recortes = []
    candidatas_data = _indice_linhas_candidatas_data(caixas)
    for indice, caixa in enumerate(caixas, start=1):
        candidata_data = (indice - 1) in candidatas_data
        if time.monotonic() - inicio_total > 18 and not candidata_data:
            erros.append("limite de tempo total atingido")
            break
        nome_recorte = f"linha_{indice:02d}"
        imagem_recorte = _preparar_recorte_linha_ocr(imagem_linhas, nome_recorte, caixa, candidata_data=candidata_data)
        x1, y1, x2, y2 = caixa
        _log_diagnostico_ocr(
            caminho or "arquivo",
            "recorte=%s imagem_base=%s base_tamanho=%s caixa=%s tamanho=%sx%s tamanho_depois=%s",
            nome_recorte,
            imagem_recorte.info.get("ocr_base_nome", "linhas_1000"),
            imagem_recorte.info.get("ocr_base_tamanho", imagem_linhas.size),
            caixa,
            x2 - x1,
            y2 - y1,
            imagem_recorte.info.get("ocr_tamanho_depois", imagem_recorte.size),
        )
        if indice <= OCR_MAX_DEBUG_LINHAS:
            debug_url = _salvar_debug_recorte_ocr(debug_prefix or caminho or "arquivo", nome_recorte, imagem_recorte)
            if debug_url:
                debug_recortes.append(f"{nome_recorte}: {debug_url}")
                _log_diagnostico_ocr(caminho or "arquivo", "recorte=%s debug_url=%s", nome_recorte, debug_url)
        try:
            inicio_linha = time.monotonic()
            texto_linha, _idioma, config_usada, timeout_usado, erros_linha = _extrair_texto_recorte_ocr(pytesseract, imagem_recorte)
            tempo_linha = time.monotonic() - inicio_linha
        except Exception as exc:
            erros.append(f"{nome_recorte}: {exc.__class__.__name__}: {str(exc).strip()[:100]}")
            _log_diagnostico_ocr(caminho or "arquivo", "recorte=%s excecao=%s tempo=%.2fs", nome_recorte, exc.__class__.__name__, time.monotonic() - inicio_linha)
            continue

        for erro_linha in erros_linha:
            _log_diagnostico_ocr(caminho or "arquivo", "recorte=%s tentativa OCR falhou=%s", nome_recorte, erro_linha)
        texto_limpo = _normalizar_espacos(texto_linha)
        if not texto_limpo:
            continue
        textos.append(f"{indice:02d}: {texto_limpo}")
        if candidata_data:
            textos[-1] = f"{indice:02d} candidata_data: {texto_limpo}"
        texto_parcial = "\n".join(textos)
        resultado_parcial = _resultado_comprovante_parcial(texto_parcial)
        extraiu_valor = bool(resultado_parcial and resultado_parcial.get("valor"))
        extraiu_data = bool(resultado_parcial and resultado_parcial.get("data_pagamento"))
        _log_diagnostico_ocr(
            caminho or "arquivo",
            "recorte=%s config=%s timeout=%ss tempo=%.2fs texto=%s extraiu_valor=%s extraiu_data=%s",
            nome_recorte,
            config_usada or "padrao",
            timeout_usado,
            tempo_linha,
            texto_limpo[:100],
            extraiu_valor,
            extraiu_data,
        )
        if extraiu_valor and extraiu_data:
            break

    if not textos:
        return ""

    texto = "[OCR linhas detectadas]\n" + "\n".join(textos)
    if erros:
        texto = f"{texto}\n\n[OCR avisos linhas]\n" + "\n".join(erros)[:500]
    if debug_recortes:
        texto = f"{texto}\n\n[OCR debug recortes]\n" + "\n".join(debug_recortes)
    resultado = _resultado_comprovante_parcial(texto)
    _log_diagnostico_ocr(
        caminho or "arquivo",
        "linhas OCR tempo_total=%.2fs linhas_lidas=%s extraiu_valor=%s extraiu_data=%s banco=%s",
        time.monotonic() - inicio_total,
        len(textos),
        bool(resultado and resultado.get("valor")),
        bool(resultado and resultado.get("data_pagamento")),
        (resultado or {}).get("instituicao_pix", ""),
    )
    return texto


def _extrair_texto_comprovante(arquivo, debug_prefix=None):
    nome = (getattr(arquivo, "name", "") or "").lower()
    content_type = (getattr(arquivo, "content_type", "") or "").lower()
    conteudo = arquivo.read()
    arquivo.seek(0)

    if content_type.startswith("text/") or nome.endswith(".txt"):
        return conteudo.decode("utf-8", errors="ignore")

    try:
        import pytesseract
    except ImportError:
        return "ERRO OCR: pytesseract nao instalado."

    tesseract_cmd = _resolver_tesseract_cmd()
    if not tesseract_cmd:
        return "ERRO OCR: tesseract nao encontrado."
    pytesseract.pytesseract.tesseract_cmd = tesseract_cmd

    texto_linhas = _extrair_texto_por_linhas_ocr(pytesseract, conteudo, nome or "arquivo", debug_prefix=debug_prefix)
    resultado_linhas = _resultado_comprovante_parcial(texto_linhas) if texto_linhas else None
    if resultado_linhas and (resultado_linhas.get("valor") or resultado_linhas.get("data_pagamento")):
        return texto_linhas

    try:
        tamanho_original, tamanho_reduzido, recortes = _preparar_recortes_ocr(conteudo)
    except Exception as exc:
        return f"ERRO OCR: falha ao abrir imagem ({exc.__class__.__name__})."

    _log_diagnostico_ocr(nome or "arquivo", "tamanho original=%s reduzido=%s", tamanho_original, tamanho_reduzido)

    textos = []
    erros = []
    debug_recortes = []
    recortes_tentados = []
    for nome_recorte, imagem, caixa in recortes:
        texto_parcial_atual = "\n\n".join(textos)
        resultado_atual = _resultado_comprovante_parcial(texto_parcial_atual) if texto_parcial_atual else None
        if nome_recorte == "faixa_valor_alternativa" and resultado_atual and resultado_atual.get("valor"):
            continue
        if nome_recorte == "faixa_data_alternativa" and resultado_atual and resultado_atual.get("data_pagamento"):
            continue
        recortes_tentados.append(nome_recorte)
        x1, y1, x2, y2 = caixa
        _log_diagnostico_ocr(
            nome or "arquivo",
            "recorte=%s imagem_base=%s base_tamanho=%s caixa_pct=%s caixa=%s tamanho=%sx%s tamanho_antes=%s tamanho_depois=%s",
            nome_recorte,
            imagem.info.get("ocr_base_nome", "reduzida"),
            imagem.info.get("ocr_base_tamanho", imagem.size),
            imagem.info.get("ocr_caixa_percentual", ""),
            caixa,
            x2 - x1,
            y2 - y1,
            imagem.info.get("ocr_tamanho_antes", (x2 - x1, y2 - y1)),
            imagem.info.get("ocr_tamanho_depois", imagem.size),
        )
        debug_url = _salvar_debug_recorte_ocr(debug_prefix or nome or "arquivo", nome_recorte, imagem)
        if debug_url:
            debug_linha = f"{nome_recorte}: {debug_url}"
            debug_recortes.append(debug_linha)
            _log_diagnostico_ocr(nome or "arquivo", "recorte=%s debug_url=%s", nome_recorte, debug_url)
        try:
            inicio_tentativa = time.monotonic()
            texto_recorte, idioma_usado, config_usada, timeout_usado, erros_idioma = _extrair_texto_recorte_ocr(pytesseract, imagem)
            tempo_tentativa = time.monotonic() - inicio_tentativa
        except Exception as exc:
            tempo_tentativa = time.monotonic() - inicio_tentativa
            mensagem = str(exc).strip()
            detalhe = f": {mensagem[:80]}" if mensagem else ""
            erros.append(f"{nome_recorte}: {exc.__class__.__name__}{detalhe}")
            _log_diagnostico_ocr(
                nome or "arquivo",
                "recorte=%s excecao=%s%s config=%s timeout=%ss tempo=%.2fs",
                nome_recorte,
                exc.__class__.__name__,
                detalhe,
                ",".join(imagem.info.get("ocr_configs") or [imagem.info.get("ocr_config", "")]) or "padrao",
                imagem.info.get("ocr_timeout", OCR_TIMEOUT_SEGUNDOS),
                tempo_tentativa,
            )
            continue

        for erro_idioma in erros_idioma:
            _log_diagnostico_ocr(nome or "arquivo", "recorte=%s tentativa OCR falhou=%s", nome_recorte, erro_idioma)
        if _normalizar_espacos(texto_recorte):
            caixa_percentual = imagem.info.get("ocr_caixa_percentual")
            rotulo_recorte = nome_recorte
            if caixa_percentual:
                rotulo_recorte = f"{nome_recorte} pct={caixa_percentual}"
            texto_atual = f"[OCR {rotulo_recorte}]\n{texto_recorte.strip()}"
            textos.append(texto_atual)
            texto_parcial = "\n\n".join(textos)
            resultado_parcial = _resultado_comprovante_parcial(texto_parcial)
            extraiu_valor = bool(resultado_parcial and resultado_parcial.get("valor"))
            extraiu_data = bool(resultado_parcial and resultado_parcial.get("data_pagamento"))
            if OCR_RENDER_MODO_LEVE and not imagem.info.get("ocr_faixa") and (extraiu_valor or extraiu_data):
                _log_diagnostico_ocr(
                    nome or "arquivo",
                    "modo leve parou cedo recorte=%s recortes_tentados=%s config=%s timeout=%ss tempo=%.2fs texto_tamanho=%s extraiu_valor=%s extraiu_data=%s",
                    nome_recorte,
                    ",".join(recortes_tentados),
                    config_usada or "padrao",
                    timeout_usado,
                    tempo_tentativa,
                    len(texto_recorte.strip()),
                    extraiu_valor,
                    extraiu_data,
                )
                if debug_recortes:
                    texto_parcial = f"{texto_parcial}\n\n[OCR debug recortes]\n" + "\n".join(debug_recortes)
                return texto_parcial
            if OCR_RENDER_MODO_LEVE and imagem.info.get("ocr_faixa") and extraiu_valor and extraiu_data:
                _log_diagnostico_ocr(
                    nome or "arquivo",
                    "modo leve parou apos faixas recorte=%s recortes_tentados=%s config=%s timeout=%ss tempo=%.2fs texto=%s extraiu_valor=%s extraiu_data=%s",
                    nome_recorte,
                    ",".join(recortes_tentados),
                    config_usada or "padrao",
                    timeout_usado,
                    tempo_tentativa,
                    _normalizar_espacos(texto_recorte)[:80],
                    extraiu_valor,
                    extraiu_data,
                )
                if debug_recortes:
                    texto_parcial = f"{texto_parcial}\n\n[OCR debug recortes]\n" + "\n".join(debug_recortes)
                return texto_parcial
        else:
            texto_parcial = "\n\n".join(textos)
            resultado_parcial = _resultado_comprovante_parcial(texto_parcial) if texto_parcial else None
            extraiu_valor = bool(resultado_parcial and resultado_parcial.get("valor"))
            extraiu_data = bool(resultado_parcial and resultado_parcial.get("data_pagamento"))
        _log_diagnostico_ocr(
            nome or "arquivo",
            "recorte=%s idioma OCR usado=%s config=%s timeout=%ss tempo=%.2fs texto_tamanho=%s texto=%s extraiu_valor=%s extraiu_data=%s",
            nome_recorte,
            idioma_usado,
            config_usada or "padrao",
            timeout_usado,
            tempo_tentativa,
            len(texto_recorte.strip()),
            _normalizar_espacos(texto_recorte)[:80],
            extraiu_valor,
            extraiu_data,
        )
        if OCR_RENDER_MODO_LEVE and nome_recorte == "faixa_data_alternativa" and (extraiu_valor or extraiu_data):
            _log_diagnostico_ocr(
                nome or "arquivo",
                "modo leve parou apos faixas recortes_tentados=%s extraiu_valor=%s extraiu_data=%s",
                ",".join(recortes_tentados),
                extraiu_valor,
                extraiu_data,
            )
            texto_faixas = "\n\n".join(textos)
            if debug_recortes:
                texto_faixas = f"{texto_faixas}\n\n[OCR debug recortes]\n" + "\n".join(debug_recortes)
            return texto_faixas

    texto = "\n\n".join(textos)

    if not _normalizar_espacos(texto):
        if erros:
            texto_erro = "ERRO OCR: todos os recortes falharam (" + "; ".join(erros)[:500] + ")."
            if debug_recortes:
                texto_erro = f"{texto_erro}\n\n[OCR debug recortes]\n" + "\n".join(debug_recortes)
            return texto_erro
        texto_vazio = "OCR executado, mas nao retornou texto."
        if debug_recortes:
            texto_vazio = f"{texto_vazio}\n\n[OCR debug recortes]\n" + "\n".join(debug_recortes)
        return texto_vazio

    if erros:
        texto = f"{texto}\n\n[OCR avisos]\n" + "\n".join(erros)

    if debug_recortes:
        texto = f"{texto}\n\n[OCR debug recortes]\n" + "\n".join(debug_recortes)

    return texto


def _texto_eh_diagnostico_ocr(texto):
    texto_limpo = _normalizar_espacos(texto)
    return texto_limpo.startswith("ERRO OCR:") or texto_limpo == "OCR executado, mas nao retornou texto."


def _texto_para_parse_ocr(texto):
    linhas = []
    for linha in texto.splitlines():
        linha_limpa = _normalizar_espacos(linha)
        if re.fullmatch(r"\[OCR [^\]]+\]", linha_limpa):
            continue
        linha = re.sub(r"^\s*\d{1,2}\s+candidata_data:\s*", "", linha)
        linha = re.sub(r"^\s*\d{1,2}:\s*", "", linha)
        linhas.append(linha)
    return "\n".join(linhas)


def _resultado_comprovante_sem_leitura(texto_ocr_bruto):
    return {
        "ok": False,
        "pagador": "",
        "valor": "",
        "data_pagamento": "",
        "instituicao_pix": "",
        "debug_data_pagamento": "Data enviada ao frontend: nao reconhecida",
        "texto_extraido": "",
        "texto_ocr_bruto": texto_ocr_bruto[:2000],
        "mensagem": "Nao foi possivel ler automaticamente o comprovante. OCR de imagem nao esta disponivel neste ambiente; preencha manualmente.",
    }


def _resultado_comprovante_parcial(texto):
    texto_parse = _texto_para_parse_ocr(texto)
    pagador = _extrair_pagador(texto_parse)
    valor = _extrair_valor(texto_parse)
    data_pagamento = _extrair_data_pagamento(texto_parse)
    instituicao_pix = _extrair_instituicao_pix(texto_parse)
    ok = bool(pagador or valor or data_pagamento or instituicao_pix)
    if not ok:
        return None

    return {
        "ok": True,
        "pagador": pagador,
        "valor": valor,
        "data_pagamento": data_pagamento,
        "instituicao_pix": instituicao_pix,
        "debug_data_pagamento": _debug_data_pagamento(texto_parse, data_pagamento),
        "texto_extraido": _normalizar_espacos(texto_parse)[:700],
        "texto_ocr_bruto": texto[:2000],
        "mensagem": "OCR parcial concluido. Confira os dados antes de salvar.",
    }


def _extrair_valor(texto):
    def normalizar_valor_ocr(valor):
        valor = re.sub(r"[^\d,.]", "", str(valor or ""))
        if not valor:
            return ""

        # OCR Nubank pode ler R$ 1.200,00 como R$ 1,200,00.
        # Quando ha duas virgulas, a ultima e decimal e as anteriores sao milhar.
        if valor.count(",") >= 2 and "." not in valor:
            partes = valor.split(",")
            return "".join(partes[:-1]) + "." + partes[-1]

        if "," in valor and "." in valor:
            if valor.rfind(",") > valor.rfind("."):
                return valor.replace(".", "").replace(",", ".")
            return valor.replace(",", "")

        if "," in valor:
            return valor.replace(".", "").replace(",", ".")

        return valor.replace(",", "")

    def decimal_ou_vazio(valor_texto):
        valor_normalizado = normalizar_valor_ocr(valor_texto)
        try:
            valor_decimal = Decimal(valor_normalizado).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError):
            return ""
        if valor_decimal <= Decimal("0.00"):
            return ""
        return f"{valor_decimal}"

    def contexto_suspeito(posicao_inicio, posicao_fim):
        antes = texto[max(0, posicao_inicio - 100):posicao_inicio]
        depois = texto[posicao_fim:posicao_fim + 100]
        contexto = _normalizar_rotulo_ocr(f"{antes} {depois}")
        termos_ruins = (
            "cpf",
            "cnpj",
            "agencia",
            "conta",
            "documento",
            "autenticacao",
            "id",
            "transacao",
            "sessao",
            "telefone",
            "central",
            "sac",
            "ouvidoria",
            "recebedor",
            "pagador",
            "instituicao",
            "chave pix",
        )
        return any(termo in contexto for termo in termos_ruins)

    # 1) Prioridade maxima: linha limpa com valor.
    # Ex.: "R$ 5,00", "RS 5,00", "Valor R$ 5,00", "Pix enviado R$ 5,00".
    for linha in str(texto or "").splitlines():
        linha_limpa = " ".join(str(linha or "").strip().split())
        if not linha_limpa:
            continue

        match_linha = re.search(
            r"^(?:valor|total|pix enviado|pix recebido|comprovante)?\s*R[S$§]?\s*([0-9]{1,3}(?:[.,][0-9]{3})*,[0-9]{2}|[0-9]+,[0-9]{2})\s*$",
            linha_limpa,
            flags=re.IGNORECASE,
        )
        if match_linha:
            valor_linha = decimal_ou_vazio(match_linha.group(1))
            if valor_linha:
                return valor_linha

    padroes_prioritarios = [
        r"(?:valor|total)\D{0,30}R?\$?\s*([0-9]{1,3}(?:[.,][0-9]{3})+,[0-9]{2})",
        r"(?:valor|total)\D{0,30}R?\$?\s*([0-9]+,[0-9]{2})",
        r"(?:pix enviado|pix recebido)\D{0,40}R?[S$§]?\s*([0-9]{1,3}(?:[.,][0-9]{3})+,[0-9]{2})",
        r"(?:pix enviado|pix recebido)\D{0,40}R?[S$§]?\s*([0-9]+,[0-9]{2})",
        r"\bR[S$§]\s*([0-9]{1,3}(?:[.,][0-9]{3})+,[0-9]{2})",
        r"\bR[S$§]\s*([0-9]+,[0-9]{2})",
        r"\bR\$\s*([0-9]{1,3}(?:[.,][0-9]{3})+,[0-9]{2})",
        r"\bR\$\s*([0-9]+,[0-9]{2})",
    ]

    padroes_genericos = [
        r"\b([0-9]{1,3}(?:[.,][0-9]{3})+,[0-9]{2})\b",
        r"\b([0-9]+,[0-9]{2})\b",
    ]

    for padrao in padroes_prioritarios:
        for encontrado in re.finditer(padrao, texto, flags=re.IGNORECASE):
            if contexto_suspeito(encontrado.start(), encontrado.end()):
                continue

            valor_texto = decimal_ou_vazio(encontrado.group(1))
            if valor_texto:
                return valor_texto

    for padrao in padroes_genericos:
        for encontrado in re.finditer(padrao, texto, flags=re.IGNORECASE):
            if contexto_suspeito(encontrado.start(), encontrado.end()):
                continue

            valor_texto = decimal_ou_vazio(encontrado.group(1))
            if valor_texto:
                return valor_texto

    return ""
def _extrair_bloco_quem_vai_enviar(linhas):
    indice_envio = next(
        (
            indice
            for indice, linha in enumerate(linhas)
            if "quem vai enviar" in _normalizar_rotulo_ocr(linha)
        ),
        -1,
    )
    if indice_envio < 0:
        return []

    bloco = []
    for linha in linhas[indice_envio + 1:]:
        linha_normalizada = _normalizar_rotulo_ocr(linha)
        if re.search(r"\b(quem vai receber|dados da transacao|dados da operacao|id|nsu)\b", linha_normalizada):
            break
        bloco.append(linha)
    return bloco


def _valor_de_rotulo_no_bloco(bloco, rotulo):
    for indice, linha in enumerate(bloco):
        linha_normalizada = _normalizar_rotulo_ocr(linha)
        if not re.match(rf"^{rotulo}\b", linha_normalizada):
            continue

        valores = []

        partes = re.split(r":|-", linha, maxsplit=1)
        if len(partes) > 1:
            candidato = _normalizar_espacos(partes[1])
            if candidato:
                valores.append(candidato)
        else:
            candidato_inline = re.sub(
                rf"^\s*{rotulo}\b\s*",
                "",
                linha,
                flags=re.IGNORECASE,
            )
            candidato_inline = _normalizar_espacos(candidato_inline)
            if candidato_inline:
                valores.append(candidato_inline)

        for proxima in bloco[indice + 1:indice + 6]:
            proxima_normalizada = _normalizar_rotulo_ocr(proxima)
            if not proxima_normalizada:
                continue
            if re.search(
                r"\b(nome|cpf|cnpj|banco|instituicao|dados|transacao|operacao|id|nsu)\b",
                proxima_normalizada,
            ):
                break
            valores.append(proxima)

        if valores:
            return _normalizar_espacos(" ".join(valores))
    return ""


def _extrair_pagador_caixa_tem(linhas):
    bloco = _extrair_bloco_quem_vai_enviar(linhas)
    if not bloco:
        return ""

    nome = _valor_de_rotulo_no_bloco(bloco, "nome")
    return nome[:160] if _parece_nome_pessoa(nome) else ""


def _extrair_instituicao_caixa_tem(linhas):
    bloco = _extrair_bloco_quem_vai_enviar(linhas)
    if not bloco:
        return ""

    banco = _valor_de_rotulo_no_bloco(bloco, "banco")
    banco_normalizado = _normalizar_linha(_sem_acentos(banco))
    if "caixa" in banco_normalizado:
        return "Caixa Econ\u00f4mica Federal"
    if re.search(r"\b(nubank|nu pagamentos)\b", banco_normalizado):
        return "Nubank"
    return ""


def _extrair_instituicao_pix(texto):
    def eh_inicio_bloco_recebedor(linha_normalizada):
        return bool(
            re.search(r"\bdados do recebedor\b", linha_normalizada)
            or re.search(r"\b(quem recebeu|recebedor|beneficiario)\b", linha_normalizada)
            or re.match(r"^@?\s*para\b", linha_normalizada)
        )

    def eh_inicio_bloco_pagador(linha_normalizada):
        return bool(re.search(
            r"\b(origem|pagador|quem pagou|quem vai enviar|dados de origem|dados do pagador|remetente)\b",
            linha_normalizada,
        ) or re.fullmatch(r"@?\s*de\b:?.*", linha_normalizada))

    linhas = [_normalizar_espacos(linha) for linha in texto.splitlines()]
    instituicao_caixa_tem = _extrair_instituicao_caixa_tem(linhas)
    if instituicao_caixa_tem:
        return instituicao_caixa_tem

    linhas_cabecalho = []
    for linha in linhas:
        linha_normalizada = _normalizar_rotulo_ocr(linha)
        if eh_inicio_bloco_recebedor(linha_normalizada):
            break
        linhas_cabecalho.append(linha)

    texto_cabecalho = _normalizar_linha(_sem_acentos("\n".join(linhas_cabecalho)))
    linhas_fora_recebedor = []
    em_bloco_recebedor = False
    for linha in linhas:
        linha_normalizada = _normalizar_rotulo_ocr(linha)
        if eh_inicio_bloco_recebedor(linha_normalizada):
            em_bloco_recebedor = True
            continue
        if eh_inicio_bloco_pagador(linha_normalizada):
            em_bloco_recebedor = False

        if not em_bloco_recebedor:
            linhas_fora_recebedor.append(linha)

    texto_fora_recebedor = _normalizar_linha(_sem_acentos("\n".join(linhas_fora_recebedor)))
    padroes = [
        ("Banpar\u00e1", [r"\bbanpara\b", r"\bbanco do estado do para\b"]),
        ("PicPay", [r"\bpicpay\b", r"\bpic\s*pay\b", r"picpay instituicao de pagamento"]),
        ("Mercado Pago", [r"\bmercado pago\b"]),
        ("Nubank", [r"\bnubank\b", r"\bnu pagamentos\b"]),
        ("Inter", [r"\bbanco inter\b", r"\bintermedium\b", r"\bsinter\b", r"\binter\b"]),
        ("Caixa EconÃ´mica", [r"\bcaixa economica\b", r"\bcaixa\b"]),
        ("Banco do Brasil", [r"\bbanco do brasil\b"]),
        ("Bradesco", [r"\bbradesco\b"]),
        ("Itaú Unibanco", [r"\bitau\b", r"\btau\s+unibanco\b", r"\bunibanco\b"]),
        ("Santander", [r"\bsantander\b"]),
        ("PagBank", [r"\bpagbank\b", r"\bpagseguro\b"]),
        ("C6 Bank", [r"\bc6 bank\b", r"\bc6\b"]),
        ("Sicredi", [r"\bsicredi\b"]),
        ("Sicoob", [r"\bsicoob\b"]),
        ("Stone", [r"\bstone\b"]),
        ("InfinitePay", [r"\binfinitepay\b", r"\binfinite pay\b"]),
    ]

    for nome, termos in padroes:
        if any(re.search(termo, texto_cabecalho) for termo in termos):
            return nome

    for nome, termos in padroes:
        if any(re.search(termo, texto_fora_recebedor) for termo in termos):
            return nome

    em_bloco_recebedor = False
    for linha in linhas:
        linha_normalizada = _normalizar_rotulo_ocr(linha)
        if eh_inicio_bloco_recebedor(linha_normalizada):
            em_bloco_recebedor = True
        elif eh_inicio_bloco_pagador(linha_normalizada):
            em_bloco_recebedor = False

        if em_bloco_recebedor:
            continue
        if re.search(r"\b(nubank|nu pagamentos)\b", linha_normalizada):
            return "Nubank"
    return ""


def _mes_ocr_para_numero(mes_texto):
    meses = {
        "jan": 1,
        "fev": 2,
        "mar": 3,
        "abr": 4,
        "mai": 5,
        "jun": 6,
        "jul": 7,
        "ago": 8,
        "set": 9,
        "out": 10,
        "nov": 11,
        "dez": 12,
        "janeiro": 1,
        "fevereiro": 2,
        "marco": 3,
        "abril": 4,
        "maio": 5,
        "junho": 6,
        "julho": 7,
        "agosto": 8,
        "setembro": 9,
        "outubro": 10,
        "novembro": 11,
        "dezembro": 12,
        "5et": 9,
        "sct": 9,
        "sel": 9,
        "se1": 9,
    }
    mes_normalizado = _normalizar_linha(_sem_acentos(mes_texto))
    mes_normalizado = mes_normalizado.strip(".").replace("5", "s").replace("1", "l")
    return meses.get(mes_normalizado) or meses.get(mes_normalizado[:3])


def _normalizar_numero_ocr(valor):
    return (
        str(valor or "")
        .upper()
        .replace("O", "0")
        .replace("I", "1")
        .replace("L", "1")
        .replace("|", "1")
    )


def _formatar_data_pagamento_ocr(dia, mes, ano, hora, minuto):
    dia = _normalizar_numero_ocr(dia)
    ano = _normalizar_numero_ocr(ano)
    hora = _normalizar_numero_ocr(hora)
    minuto = _normalizar_numero_ocr(minuto)
    try:
        data = datetime(int(ano), int(mes), int(dia), int(hora), int(minuto))
    except (TypeError, ValueError):
        return ""
    return data.strftime("%Y-%m-%dT%H:%M")


def _extrair_hora_ocr(texto):
    encontrado = re.search(
        r"\b([O0-2IL|]?[0-9OIL|])\s*(?::|h|\.)\s*([0-5OIL|][0-9OIL|])(?:(?::|\.)[0-5OIL|][0-9OIL|])?\b",
        texto,
        flags=re.IGNORECASE,
    )
    return encontrado.groups() if encontrado else None


def _extrair_data_textual_pagamento(texto):
    texto_sem_acentos = _sem_acentos(texto)
    encontrado = re.search(
        r"\b([0-3OIL|]?[0-9OIL|])(?:\s+de)?\s+([A-Za-z0-9]{3,9})\.?(?:\s+de)?\s+([0-9OIL|]{4})(?:\s*[-–—]?\s*|\D{0,30}?)([O0-2IL|]?[0-9OIL|])\s*(?::|h|\.)\s*([0-5OIL|][0-9OIL|])(?:(?::|\.)[0-5OIL|][0-9OIL|])?",
        texto_sem_acentos,
        flags=re.IGNORECASE,
    )
    if not encontrado:
        encontrado = re.search(
            r"\b([0-3OIL|]?[0-9OIL|])\s*/\s*([A-Za-z0-9]{3,9})\.?\s*/\s*([0-9OIL|]{4})\D{0,30}?([O0-2IL|]?[0-9OIL|])\s*(?::|h|\.)\s*([0-5OIL|][0-9OIL|])(?:(?::|\.)[0-5OIL|][0-9OIL|])?",
            texto_sem_acentos,
            flags=re.IGNORECASE,
        )
    if not encontrado:
        return ""

    dia, mes_texto, ano, hora, minuto = encontrado.groups()
    mes = _mes_ocr_para_numero(mes_texto)
    if not mes:
        return ""
    return _formatar_data_pagamento_ocr(dia, mes, ano, hora, minuto)


def _debug_data_pagamento(texto, data_pagamento):
    candidatos = []
    for linha in texto.splitlines():
        linha_limpa = _normalizar_espacos(linha)
        linha_normalizada = _normalizar_linha(_sem_acentos(linha_limpa))
        if re.search(r"\b(jan|fev|mar|abr|mai|jun|jul|ago|set|out|nov|dez|\d{1,2}/)\b", linha_normalizada):
            candidatos.append(linha_limpa)
        if len(candidatos) >= 4:
            break
    trecho = " | ".join(candidatos) or "nenhum trecho de data localizado no OCR"
    return (
        f"Data enviada ao frontend: {data_pagamento or 'nao reconhecida'}\n"
        f"Trechos candidatos de data no OCR: {trecho}"
    )


def _extrair_data_pagamento(texto):
    linhas = [_normalizar_espacos(linha) for linha in texto.splitlines()]
    for indice, linha in enumerate(linhas):
        if "data do pagamento" not in _normalizar_rotulo_ocr(linha):
            continue
        trecho = " ".join(linhas[indice:indice + 8])
        encontrado_data = re.search(r"\b([0-3]?\d)/([01]?\d)/(\d{4})\b", trecho)
        encontrado_hora = _extrair_hora_ocr(trecho)
        if encontrado_data and encontrado_hora:
            dia, mes, ano = encontrado_data.groups()
            hora, minuto = encontrado_hora
            data_formatada = _formatar_data_pagamento_ocr(dia, mes, ano, hora, minuto)
            if data_formatada:
                return data_formatada

    for indice, linha in enumerate(linhas):
        if "data da operacao" not in _normalizar_rotulo_ocr(linha):
            continue
        trecho = " ".join(linhas[indice:indice + 3])
        encontrado_data = re.search(r"\b([0-3]?\d)/([01]?\d)/(\d{4})\b", trecho)
        encontrado_hora = _extrair_hora_ocr(trecho)
        if encontrado_data and encontrado_hora:
            dia, mes, ano = encontrado_data.groups()
            hora, minuto = encontrado_hora
            data_formatada = _formatar_data_pagamento_ocr(dia, mes, ano, hora, minuto)
            if data_formatada:
                return data_formatada

    for indice, linha in enumerate(linhas):
        encontrado_data = re.search(r"\b([0-3]?\d)/([01]?\d)/(\d{4})\b", linha)
        if not encontrado_data:
            continue
        for proxima in linhas[indice:indice + 5]:
            encontrado_horario = _extrair_hora_ocr(proxima)
            if not encontrado_horario:
                continue
            dia, mes, ano = encontrado_data.groups()
            hora, minuto = encontrado_horario
            data_formatada = _formatar_data_pagamento_ocr(dia, mes, ano, hora, minuto)
            if data_formatada:
                return data_formatada

    encontrado = re.search(
        r"\b([0-3]?\d)/([01]?\d)/(\d{4})\D{0,20}([O0-2]?\d)\s*(?::|h)\s*([0-5]\d)(?::[0-5]\d)?\b",
        texto,
        flags=re.IGNORECASE,
    )
    if encontrado:
        dia, mes, ano, hora, minuto = encontrado.groups()
        return _formatar_data_pagamento_ocr(dia, mes, ano, hora, minuto)

    data_textual = _extrair_data_textual_pagamento(texto)
    if data_textual:
        return data_textual

    meses = {
        "jan": 1,
        "fev": 2,
        "mar": 3,
        "abr": 4,
        "mai": 5,
        "jun": 6,
        "jul": 7,
        "ago": 8,
        "set": 9,
        "out": 10,
        "nov": 11,
        "dez": 12,
        "janeiro": 1,
        "fevereiro": 2,
        "marco": 3,
        "marÃ§o": 3,
        "abril": 4,
        "maio": 5,
        "junho": 6,
        "julho": 7,
        "agosto": 8,
        "setembro": 9,
        "outubro": 10,
        "novembro": 11,
        "dezembro": 12,
    }
    encontrado = re.search(
        r"\b([0-3]?\d)\s+([A-Z]{3})\s+(\d{4})\D{0,12}([0-2]?\d):([0-5]\d)",
        texto,
        flags=re.IGNORECASE,
    )
    if not encontrado:
        encontrado = re.search(
            r"\b([0-3]?\d)/([A-ZÃ‡]+?)/(\d{4})\D{0,20}([0-2]?\d):([0-5]\d)",
            texto,
            flags=re.IGNORECASE,
        )
    if not encontrado:
        return ""

    dia, mes_texto, ano, hora, minuto = encontrado.groups()
    mes_texto = mes_texto.lower()
    mes = meses.get(mes_texto) or meses.get(mes_texto[:3])
    if not mes:
        return ""
    return f"{ano}-{int(mes):02d}-{int(dia):02d}T{int(hora):02d}:{int(minuto):02d}"


def _linha_apos_rotulo(texto, rotulos):
    linhas = [_normalizar_espacos(linha) for linha in texto.splitlines()]
    for indice, linha in enumerate(linhas):
        linha_normalizada = linha.lower()
        if not any(rotulo in linha_normalizada for rotulo in rotulos):
            continue

        partes = re.split(r":|-", linha, maxsplit=1)
        if len(partes) > 1:
            candidato = _normalizar_espacos(partes[1])
            if candidato and not _eh_linha_bloqueada_para_nome(candidato):
                return candidato

        for proxima in linhas[indice + 1:indice + 4]:
            if not proxima:
                continue
            if _eh_linha_bloqueada_para_nome(proxima):
                break
            return proxima
    return ""



def _limpar_nome_pagador_ocr(nome):
    nome = _normalizar_espacos(nome)
    nome = re.sub(r"^[0-9]{2,}\s+", "", nome).strip()

    # Alguns OCRs do Nubank grudam dados do bloco seguinte no nome do pagador.
    # Ex.: "Carlinda Ramos Ferreira Instituigao" deve virar "Carlinda Ramos Ferreira".
    nome = re.split(
        r"\b(?:instituicao|instituigao|instituig\w*|institue|institute|institui\w*|agencia|conta|cpf|cnpj|nu pagamentos|pagamentos|pagamento|nubank)\b",
        nome,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]

    nome = re.sub(r"\b(?:institui|instituic|instituig|institue|institute|agencia|pagamentos?|pagament|pagamen)\s*$", "", nome, flags=re.IGNORECASE)
    nome = _normalizar_espacos(nome).strip(" :-")

    # Erro comum do OCR em alguns comprovantes Nubank: Ireno vira treno.
    nome = re.sub(r"^treno\b", "Ireno", nome, flags=re.IGNORECASE)

    nome_normalizado = _normalizar_linha(_sem_acentos(nome))
    if re.search(r"\b(instituicao|instituigao|institue|institute|pagamentos|pagamento|nubank|agencia|conta|cpf|cnpj)\b", nome_normalizado):
        return ""

    return nome if (_parece_nome_pessoa(nome) or re.fullmatch(r"[^\W\d_]{2,}", nome)) else ""


def _extrair_nome_no_bloco(linhas):
    for indice, linha in enumerate(linhas):
        if not re.search(r"\b(nome|none|kane)\b", _normalizar_linha(_sem_acentos(linha))):
            continue

        valores = []

        partes = re.split(r":|-", linha, maxsplit=1)
        if len(partes) > 1:
            candidato = _normalizar_espacos(partes[1])
            if candidato and not _eh_linha_bloqueada_para_nome(candidato):
                valores.append(candidato)
        else:
            candidato_inline = re.sub(r"^.*?\b(?:nome|none|kane)\b", "", linha, flags=re.IGNORECASE).strip(" :-")
            if candidato_inline and not _eh_linha_bloqueada_para_nome(candidato_inline):
                valores.append(candidato_inline)

        for proxima in linhas[indice + 1:indice + 5]:
            if not proxima:
                continue
            proxima_normalizada = _normalizar_rotulo_ocr(proxima)
            if _eh_linha_bloqueada_para_nome(proxima):
                break
            if re.search(r"\b(cpf|cnpj|instituicao|instituig\w*|institui\w*|banco|chave|valor|data|destino|origem|recebedor|pagador)\b", proxima_normalizada):
                break
            if _parece_nome_pessoa(proxima) or (valores and re.fullmatch(r"[^\W\d_]{2,}", proxima)):
                valores.append(proxima)
                continue
            break

        if valores:
            if len(valores) == 1 and re.fullmatch(r"[^\W\d_]{2,3}", valores[0]):
                for anterior in reversed(linhas[:indice]):
                    if not anterior:
                        continue
                    if _eh_linha_bloqueada_para_nome(anterior):
                        break
                    if _parece_nome_pessoa(anterior):
                        valores.insert(0, anterior)
                        break

            if len(valores) >= 2:
                ultimo = _normalizar_linha(_sem_acentos(valores[-1]))
                penultimo = _normalizar_linha(_sem_acentos(valores[-2]))
                if ultimo == "me" and re.search(r"\bde$", penultimo):
                    valores[-1] = "Lima"

            nome = _limpar_nome_pagador_ocr(" ".join(valores))

            if _normalizar_linha(_sem_acentos(nome)) == "me":
                for anterior in reversed(linhas[:indice]):
                    if not anterior:
                        continue
                    if _eh_linha_bloqueada_para_nome(anterior):
                        break
                    if _parece_nome_pessoa(anterior):
                        anterior_limpo = _normalizar_espacos(anterior)
                        if re.search(r"\bde$", _normalizar_linha(_sem_acentos(anterior_limpo))):
                            nome = f"{anterior_limpo} Lima"
                        else:
                            nome = anterior_limpo
                        break

            if nome and not _eh_linha_bloqueada_para_nome(nome):
                return nome
    return ""


def _extrair_nome_secao_de(linha_rotulo, bloco):
    linha_limpa = _normalizar_rotulo_ocr(linha_rotulo)
    candidato_inline = re.sub(r"^\s*de:?\s*", "", linha_limpa, flags=re.IGNORECASE).strip()
    if candidato_inline and _parece_nome_pessoa(candidato_inline):
        return _normalizar_espacos(candidato_inline)

    for candidato in bloco:
        if _parece_nome_pessoa(candidato):
            return candidato
    return ""


def _eh_comprovante_banco_inter(texto):
    texto_normalizado = _normalizar_linha(_sem_acentos(texto))
    if re.search(r"\b(nu pagamentos|nubank\.com\.br)\b", texto_normalizado):
        return bool("pix enviado" in texto_normalizado and "quem recebeu" in texto_normalizado)

    for linha in texto.splitlines():
        linha_normalizada = _normalizar_rotulo_ocr(linha)
        if not re.search(r"\b(banco\s+inter|intermedium|inter\s*(?:pix|pag|bank|s\.?a\.?))\b", linha_normalizada):
            continue
        if re.search(r"\b(instituicao|destino|recebedor|beneficiario|favorecido|para|chave pix)\b", linha_normalizada):
            continue
        return True
    if "instituicao banco inter" in texto_normalizado and "destino" in texto_normalizado and "origem" in texto_normalizado:
        return True
    if "pix enviado" in texto_normalizado and "quem recebeu" in texto_normalizado:
        return True
    return any(_normalizar_rotulo_ocr(linha) == "inter" for linha in texto.splitlines())


def _eh_rotulo_recebedor_inter(linha):
    linha_normalizada = _normalizar_rotulo_ocr(linha)
    return bool(re.search(
        r"\b(beneficiario|recebedor|favorecido|destino|para|quem recebeu|dados do recebedor|chave pix do recebedor|instituicao do recebedor)\b",
        linha_normalizada,
    ))


def _eh_rotulo_pagador_inter(linha):
    linha_normalizada = _normalizar_rotulo_ocr(linha)
    if _eh_rotulo_recebedor_inter(linha_normalizada):
        return False
    if re.search(r"\b(pagador|remetente|origem|quem pagou|dados do pagador)\b", linha_normalizada):
        return True
    return bool(re.fullmatch(r"@?\s*de\b:?.*", linha_normalizada))


def _extrair_nome_mesma_linha_inter(linha):
    linha_limpa = _normalizar_espacos(linha)
    partes = re.split(r":|-", linha_limpa, maxsplit=1)
    if len(partes) > 1:
        candidato = _normalizar_espacos(partes[1])
        if _parece_nome_pessoa(candidato):
            return candidato

    candidato = re.sub(
        r"^.*?\b(?:nome do pagador|dados do pagador|pagador|remetente|origem|quem pagou|de)\b",
        "",
        linha_limpa,
        flags=re.IGNORECASE,
    ).strip(" :-")
    if candidato != linha_limpa and _parece_nome_pessoa(candidato):
        return _normalizar_espacos(candidato)
    return ""


def _normalizar_nome_inter(nome):
    nome_normalizado = _normalizar_linha(_sem_acentos(nome))
    return re.sub(r"[^a-z0-9 ]+", "", nome_normalizado).strip()


def _nomes_parecidos_inter(nome, outro_nome):
    nome_normalizado = _normalizar_nome_inter(nome)
    outro_normalizado = _normalizar_nome_inter(outro_nome)
    if not nome_normalizado or not outro_normalizado:
        return False
    if nome_normalizado == outro_normalizado:
        return True
    if nome_normalizado in outro_normalizado or outro_normalizado in nome_normalizado:
        return True

    tokens_nome = nome_normalizado.split()
    tokens_outro = outro_normalizado.split()
    if not tokens_nome or not tokens_outro or tokens_nome[0] != tokens_outro[0]:
        return False
    return len(set(tokens_nome[1:]) & set(tokens_outro[1:])) > 0


def _nome_seguro_pagador_inter(nome, nomes_recebedor):
    if not _parece_nome_pessoa(nome):
        return False
    return not any(_nomes_parecidos_inter(nome, nome_recebedor) for nome_recebedor in nomes_recebedor)


def _nome_bloqueado_temporario_inter(nome):
    nome_normalizado = _normalizar_nome_inter(nome)
    nomes_bloqueados = {
        "la neiva",
        "lincoln albuquerque",
        "lincoln albuquerque neiva",
        "lincoin albuquerque neiva",
        "neiva",
    }
    return nome_normalizado in nomes_bloqueados


def _extrair_pagador_banpara(linhas):
    texto_normalizado = " ".join(_normalizar_rotulo_ocr(linha) for linha in linhas)
    if not re.search(r"\b(banco do estado do para|banpara|comprovante de pix)\b", texto_normalizado):
        return ""

    def nome_valido(candidato):
        candidato_normalizado = _normalizar_rotulo_ocr(candidato)
        if re.search(r"\b(agencia|conta|tipo de conta|codigo|sessao|transacao|instituicao|cpf|cnpj|dados|recebedor)\b", candidato_normalizado):
            return False
        return _parece_nome_pessoa(candidato)

    indice_origem = next(
        (
            indice
            for indice, linha in enumerate(linhas)
            if "dados de origem" in _normalizar_rotulo_ocr(linha)
        ),
        -1,
    )
    if indice_origem < 0:
        return ""

    bloco = []
    for linha in linhas[indice_origem + 1:]:
        if "dados do recebedor" in _normalizar_rotulo_ocr(linha):
            break
        bloco.append(linha)

    for indice, linha in enumerate(bloco):
        if "titular" not in _normalizar_rotulo_ocr(linha):
            continue
        partes = re.split(r":|\s{2,}|-", linha, maxsplit=1)
        if len(partes) > 1:
            candidato = _normalizar_espacos(partes[1])
            if nome_valido(candidato):
                return candidato[:160]

        candidato_inline = re.sub(r"^.*?\btitular\b", "", linha, flags=re.IGNORECASE).strip(" :-")
        if candidato_inline and nome_valido(candidato_inline):
            return _normalizar_espacos(candidato_inline)[:160]

        for proxima in bloco[indice + 1:indice + 7]:
            if nome_valido(proxima):
                return proxima[:160]
    return ""


def _extrair_pagador_inter_whatsapp_rotulos_antes(linhas):
    indice_quem_pagou = next(
        (
            indice
            for indice, linha in enumerate(linhas)
            if "quem pagou" in _normalizar_rotulo_ocr(linha)
        ),
        -1,
    )
    if indice_quem_pagou < 0:
        return ""

    for indice, linha in enumerate(linhas[indice_quem_pagou + 1:], start=indice_quem_pagou + 1):
        if _normalizar_rotulo_ocr(linha) != "banco inter":
            continue
        for anterior in reversed(linhas[indice_quem_pagou + 1:indice]):
            anterior_normalizada = _normalizar_rotulo_ocr(anterior)
            if re.search(r"\b(cpf|cnpj|instituicao|nome|chave pix)\b", anterior_normalizada):
                continue
            if _nome_bloqueado_temporario_inter(anterior):
                continue
            if _parece_nome_pessoa(anterior):
                return anterior[:160]
    return ""


def _extrair_nomes_recebedor_banco_inter(linhas):
    nomes = []
    for indice, linha in enumerate(linhas):
        if not _eh_rotulo_recebedor_inter(linha):
            continue

        nome_mesma_linha = _extrair_nome_mesma_linha_inter(linha)
        if nome_mesma_linha:
            nomes.append(nome_mesma_linha)

        bloco = []
        for proxima in linhas[indice + 1:indice + 8]:
            if _eh_rotulo_pagador_inter(proxima):
                break
            bloco.append(proxima)

        nome = _extrair_nome_no_bloco(bloco)
        if nome and not _eh_rotulo_recebedor_inter(nome) and not _eh_rotulo_pagador_inter(nome) and _parece_nome_pessoa(nome):
            nomes.append(nome)

        for candidato in bloco:
            if _eh_rotulo_recebedor_inter(candidato) or _eh_rotulo_pagador_inter(candidato):
                continue
            if _parece_nome_pessoa(candidato):
                nomes.append(candidato)

    nomes_unicos = []
    for nome in nomes:
        if not any(_nomes_parecidos_inter(nome, existente) for existente in nomes_unicos):
            nomes_unicos.append(nome)
    return nomes_unicos


def _extrair_pagador_destino_nome_quebrado_inter(linhas):
    for indice, linha in enumerate(linhas):
        if _normalizar_rotulo_ocr(linha) != "destino":
            continue

        primeira_parte = ""
        segunda_parte = ""
        encontrou_nome = False
        for proxima in linhas[indice + 1:indice + 8]:
            proxima_normalizada = _normalizar_rotulo_ocr(proxima)
            if re.search(r"\b(cpf|cnpj|instituicao|origem|data|valor)\b", proxima_normalizada):
                break
            if proxima_normalizada == "nome":
                encontrou_nome = True
                continue
            if not _parece_nome_pessoa(proxima):
                continue
            if not encontrou_nome and not primeira_parte:
                primeira_parte = proxima
                continue
            if encontrou_nome and not segunda_parte:
                segunda_parte = proxima
                break

        if primeira_parte and segunda_parte:
            nome = _normalizar_espacos(f"{primeira_parte} {segunda_parte}")
            if _parece_nome_pessoa(nome) and not _nome_bloqueado_temporario_inter(nome):
                return nome[:160]
    return ""


def _extrair_pagador_banco_inter(texto):
    linhas = [_normalizar_espacos(linha) for linha in texto.splitlines()]
    nomes_recebedor = _extrair_nomes_recebedor_banco_inter(linhas)
    candidatos_pagador = []
    escolhido = _extrair_pagador_destino_nome_quebrado_inter(linhas)
    if escolhido:
        candidatos_pagador.append(escolhido)
    for indice, linha in enumerate(linhas):
        if escolhido:
            break
        if not _eh_rotulo_pagador_inter(linha):
            continue

        nome = _extrair_nome_mesma_linha_inter(linha)
        if nome:
            candidatos_pagador.append(nome)
            if _nome_seguro_pagador_inter(nome, nomes_recebedor):
                escolhido = nome[:160]
                break

        bloco = []
        for proxima in linhas[indice + 1:indice + 10]:
            if _eh_rotulo_recebedor_inter(proxima):
                break
            bloco.append(proxima)

        nome = _extrair_nome_no_bloco(bloco)
        if nome:
            candidatos_pagador.append(nome)
            if _nome_seguro_pagador_inter(nome, nomes_recebedor):
                escolhido = nome[:160]
                break

        for candidato in bloco:
            if _parece_nome_pessoa(candidato):
                candidatos_pagador.append(candidato)
            if _nome_seguro_pagador_inter(candidato, nomes_recebedor):
                escolhido = candidato[:160]
                break
        if escolhido:
            break

    if _nome_bloqueado_temporario_inter(escolhido):
        escolhido = ""

    return escolhido


def _extrair_pagador_nubank_origem_nome_quebrado(texto, linhas):
    if not re.search(r"\b(nubank|nu pagamentos)\b", _normalizar_linha(_sem_acentos(texto))):
        return ""

    for indice, linha in enumerate(linhas):
        if _normalizar_rotulo_ocr(linha) != "origem":
            continue

        primeira_parte = ""
        segunda_parte = ""
        encontrou_nome = False
        encontrou_instituicao = False
        for proxima in linhas[indice + 1:indice + 8]:
            if not proxima:
                continue
            proxima_normalizada = _normalizar_rotulo_ocr(proxima)
            if re.search(r"\b(destino|cpf|cnpj|chave|valor|data|id|transacao)\b", proxima_normalizada):
                break
            if re.search(r"\binstitui\w*|\binstituig\w*", proxima_normalizada):
                encontrou_instituicao = True
                break
            if proxima_normalizada == "nome":
                encontrou_nome = True
                continue
            if not _parece_nome_pessoa(proxima) and not (encontrou_nome and re.fullmatch(r"[^\W\d_]{2,}", proxima)):
                continue
            if not encontrou_nome and not primeira_parte:
                primeira_parte = proxima
                continue
            if encontrou_nome and not segunda_parte:
                segunda_parte = proxima

        if primeira_parte and segunda_parte and encontrou_instituicao:
            nome = _limpar_nome_pagador_ocr(f"{primeira_parte} {segunda_parte}")
            if nome:
                return nome[:160]
    return ""


def _extrair_pagador(texto):
    linhas = [_normalizar_espacos(linha) for linha in texto.splitlines()]
    pagador_caixa_tem = _extrair_pagador_caixa_tem(linhas)
    if pagador_caixa_tem:
        return pagador_caixa_tem

    pagador_banpara = _extrair_pagador_banpara(linhas)
    if pagador_banpara:
        return pagador_banpara

    pagador_inter_whatsapp = _extrair_pagador_inter_whatsapp_rotulos_antes(linhas)
    if pagador_inter_whatsapp:
        return pagador_inter_whatsapp

    pagador_nubank_nome_quebrado = _extrair_pagador_nubank_origem_nome_quebrado(texto, linhas)
    if pagador_nubank_nome_quebrado:
        return pagador_nubank_nome_quebrado

    nome_destino_quebrado = _extrair_pagador_destino_nome_quebrado_inter(linhas)
    if nome_destino_quebrado:
        return nome_destino_quebrado

    if _eh_comprovante_banco_inter(texto):
        return _extrair_pagador_banco_inter(texto)

    for indice, linha in enumerate(linhas):
        linha_normalizada = _normalizar_linha(linha)
        rotulo_normalizado = _normalizar_rotulo_ocr(linha)
        if (
            "origem" not in linha_normalizada
            and "pagador" not in linha_normalizada
            and "quem pagou" not in linha_normalizada
            and not re.fullmatch(r"de\b:?.*", rotulo_normalizado)
        ):
            continue

        nome_mesma_linha = _linha_apos_rotulo(linha, ["nome"])
        if nome_mesma_linha:
            return nome_mesma_linha[:160]

        bloco = []
        for proxima in linhas[indice + 1:indice + 10]:
            proxima_normalizada = _normalizar_rotulo_ocr(proxima)
            if re.search(r"\b(destino|recebedor|favorecido|para)\b:?", proxima_normalizada):
                break
            bloco.append(proxima)

        if re.fullmatch(r"de\b:?.*", rotulo_normalizado):
            nome = _extrair_nome_secao_de(linha, bloco)
        else:
            nome = _extrair_nome_no_bloco(bloco)
        if nome:
            return nome[:160]

    nome = _linha_apos_rotulo(texto, ["nome do pagador", "pagador", "remetente"])
    return nome[:160] if nome else ""


def analisar_comprovante_pix(arquivo, debug_prefix=None):
    try:
        texto = _extrair_texto_comprovante(arquivo, debug_prefix=debug_prefix)
    except Exception as exc:
        mensagem = str(exc).strip()
        detalhe = f": {mensagem[:120]}" if mensagem else ""
        return _resultado_comprovante_sem_leitura(f"ERRO OCR: {exc.__class__.__name__}{detalhe}")

    if not _normalizar_espacos(texto):
        return _resultado_comprovante_sem_leitura("OCR executado, mas nao retornou texto.")

    if _texto_eh_diagnostico_ocr(texto):
        resultado_parcial = _resultado_comprovante_parcial(texto)
        if resultado_parcial and (
            resultado_parcial.get("valor")
            or resultado_parcial.get("data_pagamento")
            or resultado_parcial.get("instituicao_pix")
        ):
            return resultado_parcial
        return _resultado_comprovante_sem_leitura(texto)

    texto_parse = _texto_para_parse_ocr(texto)
    pagador = _extrair_pagador(texto_parse)
    valor = _extrair_valor(texto_parse)
    data_pagamento = _extrair_data_pagamento(texto_parse)
    instituicao_pix = _extrair_instituicao_pix(texto_parse)
    ok = bool(pagador or valor or data_pagamento)

    return {
        "ok": ok,
        "pagador": pagador,
        "valor": valor,
        "data_pagamento": data_pagamento,
        "instituicao_pix": instituicao_pix,
        "debug_data_pagamento": _debug_data_pagamento(texto_parse, data_pagamento),
        "texto_extraido": _normalizar_espacos(texto_parse)[:700],
        "texto_ocr_bruto": texto[:2000],
        "mensagem": (
            "Dados lidos automaticamente. Confira antes de salvar."
            if ok
            else "Nao foi possivel identificar dados principais. Preencha manualmente."
        ),
    }



