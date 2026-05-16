import re
from decimal import Decimal, InvalidOperation
from io import BytesIO

from PIL import Image


def _normalizar_espacos(valor):
    return " ".join(str(valor or "").strip().split())


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
        return ""

    try:
        imagem = Image.open(BytesIO(conteudo))
        return pytesseract.image_to_string(imagem, lang="por")
    except Exception:
        return ""


def _extrair_valor(texto):
    padroes = [
        r"(?:valor|total)\D{0,20}R?\$?\s*([0-9]{1,3}(?:\.[0-9]{3})*,[0-9]{2})",
        r"R\$\s*([0-9]{1,3}(?:\.[0-9]{3})*,[0-9]{2})",
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
    if not encontrado:
        return ""
    dia, mes, ano, hora, minuto = encontrado.groups()
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
            if candidato:
                return candidato

        for proxima in linhas[indice + 1:indice + 4]:
            if proxima and not re.search(r"^(cpf|cnpj|instituicao|banco|agencia|conta)\b", proxima, re.IGNORECASE):
                return proxima
    return ""


def _extrair_pagador(texto):
    trecho_origem = re.search(
        r"(?:origem|pagador|quem pagou)(.*?)(?:destino|recebedor|favorecido|$)",
        texto,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if trecho_origem:
        nome = _linha_apos_rotulo(trecho_origem.group(1), ["nome"])
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
            "mensagem": "Nao foi possivel ler automaticamente o comprovante. Preencha manualmente.",
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
