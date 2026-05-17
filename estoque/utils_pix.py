import re
import shutil
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path

from PIL import Image

CAMINHO_TESSERACT_WINDOWS = Path("C:/Program Files/Tesseract-OCR/tesseract.exe")


def _normalizar_espacos(valor):
    return " ".join(str(valor or "").strip().split())


def _normalizar_linha(valor):
    return _normalizar_espacos(valor).lower()


def _eh_linha_bloqueada_para_nome(linha):
    linha_normalizada = _normalizar_linha(linha)
    if not linha_normalizada:
        return True
    if re.search(r"^(cpf|cnpj|instituicao|instituição|banco|agencia|agência|conta)\b", linha_normalizada):
        return True
    if "nu pagamentos" in linha_normalizada:
        return True
    return False


def _resolver_tesseract_cmd():
    tesseract_path = shutil.which("tesseract")
    if tesseract_path:
        return tesseract_path

    if CAMINHO_TESSERACT_WINDOWS.exists():
        return str(CAMINHO_TESSERACT_WINDOWS)

    return None


def _extrair_texto_comprovante(arquivo):
    nome = (getattr(arquivo, "name", "") or "").lower()
    content_type = (getattr(arquivo, "content_type", "") or "").lower()
    conteudo = arquivo.read()
    arquivo.seek(0)

    if content_type.startswith("text/") or nome.endswith(".txt"):
        return conteudo.decode("utf-8", errors="ignore")

    try:
        import pytesseract
    except ImportError:
        # Sem um motor de OCR real no ambiente, imagens PNG/JPG nao geram texto.
        return ""

    tesseract_cmd = _resolver_tesseract_cmd()
    if not tesseract_cmd:
        return ""
    pytesseract.pytesseract.tesseract_cmd = tesseract_cmd

    try:
        imagem = Image.open(BytesIO(conteudo))
        try:
            return pytesseract.image_to_string(imagem, lang="por")
        except Exception:
            return pytesseract.image_to_string(imagem)
    except Exception:
        return ""


def _extrair_valor(texto):
    padroes = [
        r"(?:valor|total)\D{0,20}R?\$?\s*([0-9]{1,3}(?:\.[0-9]{3})*,[0-9]{2})",
        r"R\$\s*([0-9]{1,3}(?:\.[0-9]{3})*,[0-9]{2})",
        r"R\$\s*([0-9]{1,3}(?:\.[0-9]{3})*)\b",
        r"\b([0-9]{1,3}(?:\.[0-9]{3})*,[0-9]{2})\b",
    ]
    for padrao in padroes:
        encontrado = re.search(padrao, texto, flags=re.IGNORECASE)
        if not encontrado:
            continue
        valor_texto = encontrado.group(1).replace(".", "").replace(",", ".")
        try:
            return f"{Decimal(valor_texto).quantize(Decimal('0.01'))}"
        except (InvalidOperation, ValueError):
            continue
    return ""


def _extrair_data_pagamento(texto):
    encontrado = re.search(
        r"\b([0-3]?\d)/([01]?\d)/(\d{4})\D{0,12}([0-2]?\d):([0-5]\d)\b",
        texto,
    )
    if encontrado:
        dia, mes, ano, hora, minuto = encontrado.groups()
        return f"{ano}-{int(mes):02d}-{int(dia):02d}T{int(hora):02d}:{int(minuto):02d}"

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
        "março": 3,
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
            r"\b([0-3]?\d)/([A-ZÇ]+?)/(\d{4})\D{0,20}([0-2]?\d):([0-5]\d)",
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


def _extrair_nome_no_bloco(linhas):
    for indice, linha in enumerate(linhas):
        if not re.search(r"\bnome\b", _normalizar_linha(linha)):
            continue

        partes = re.split(r":|-", linha, maxsplit=1)
        if len(partes) > 1:
            candidato = _normalizar_espacos(partes[1])
            if candidato and not _eh_linha_bloqueada_para_nome(candidato):
                return candidato

        candidato_inline = re.sub(r"^.*?\bnome\b", "", linha, flags=re.IGNORECASE).strip(" :-")
        if candidato_inline and not _eh_linha_bloqueada_para_nome(candidato_inline):
            return _normalizar_espacos(candidato_inline)

        for proxima in linhas[indice + 1:]:
            if not proxima:
                continue
            if _eh_linha_bloqueada_para_nome(proxima):
                return ""
            return proxima
    return ""


def _extrair_pagador(texto):
    linhas = [_normalizar_espacos(linha) for linha in texto.splitlines()]
    for indice, linha in enumerate(linhas):
        linha_normalizada = _normalizar_linha(linha)
        if (
            "origem" not in linha_normalizada
            and "pagador" not in linha_normalizada
            and "quem pagou" not in linha_normalizada
            and not re.fullmatch(r"de:?", linha_normalizada)
        ):
            continue

        nome_mesma_linha = _linha_apos_rotulo(linha, ["nome"])
        if nome_mesma_linha:
            return nome_mesma_linha[:160]

        bloco = []
        for proxima in linhas[indice + 1:indice + 10]:
            proxima_normalizada = _normalizar_linha(proxima)
            if re.search(r"\b(destino|recebedor|favorecido|para)\b:?", proxima_normalizada):
                break
            bloco.append(proxima)

        if re.fullmatch(r"de:?", linha_normalizada):
            nome = ""
            for candidato in bloco:
                if _eh_linha_bloqueada_para_nome(candidato):
                    break
                nome = candidato
                break
        else:
            nome = _extrair_nome_no_bloco(bloco)
        if nome:
            return nome[:160]

    nome = _linha_apos_rotulo(texto, ["nome do pagador", "pagador", "remetente"])
    return nome[:160] if nome else ""


def analisar_comprovante_pix(arquivo):
    texto = _extrair_texto_comprovante(arquivo)
    if not _normalizar_espacos(texto):
        return {
            "ok": False,
            "pagador": "",
            "valor": "",
            "data_pagamento": "",
            "texto_extraido": "",
            "mensagem": "Nao foi possivel ler automaticamente o comprovante. OCR de imagem nao esta disponivel neste ambiente; preencha manualmente.",
        }

    pagador = _extrair_pagador(texto)
    valor = _extrair_valor(texto)
    data_pagamento = _extrair_data_pagamento(texto)
    ok = bool(pagador or valor or data_pagamento)

    return {
        "ok": ok,
        "pagador": pagador,
        "valor": valor,
        "data_pagamento": data_pagamento,
        "texto_extraido": _normalizar_espacos(texto)[:700],
        "mensagem": (
            "Dados lidos automaticamente. Confira antes de salvar."
            if ok
            else "Nao foi possivel identificar dados principais. Preencha manualmente."
        ),
    }
