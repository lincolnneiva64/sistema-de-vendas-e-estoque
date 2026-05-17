import re
import shutil
import unicodedata
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path

from PIL import Image

CAMINHO_TESSERACT_WINDOWS = Path("C:/Program Files/Tesseract-OCR/tesseract.exe")


def _normalizar_espacos(valor):
    return " ".join(str(valor or "").strip().split())


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
    if re.search(r"^(cpf|cnpj|instituicao|instituição|banco|agencia|agência|conta)\b", linha_normalizada):
        return True
    if "nu pagamentos" in linha_normalizada:
        return True
    return False


def _eh_linha_bloqueada_para_nome(linha):
    linha_normalizada = _normalizar_linha(_sem_acentos(linha))
    if not linha_normalizada:
        return True
    if re.search(r"^(cpf|cnpj|instituicao|banco|agencia|conta|transacao|id|para)\b", linha_normalizada):
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
    palavras = re.findall(r"[A-Za-zÀ-ÿ]{2,}", linha_normalizada)
    return len(palavras) >= 2


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
    linhas = [_normalizar_espacos(linha) for linha in texto.splitlines()]
    for linha in linhas:
        if "data da operacao" not in _normalizar_rotulo_ocr(linha):
            continue
        encontrado = re.search(
            r"\b([0-3]?\d)/([01]?\d)/(\d{4})\D{0,20}([O0-2]?\d)\s*[h:]\s*([0-5]\d)\b",
            linha,
            flags=re.IGNORECASE,
        )
        if not encontrado:
            continue
        dia, mes, ano, hora, minuto = encontrado.groups()
        hora = hora.upper().replace("O", "0")
        if int(hora) <= 23:
            return f"{ano}-{int(mes):02d}-{int(dia):02d}T{int(hora):02d}:{int(minuto):02d}"

    for indice, linha in enumerate(linhas):
        encontrado_data = re.search(r"\b([0-3]?\d)/([01]?\d)/(\d{4})\b", linha)
        if not encontrado_data:
            continue
        for proxima in linhas[indice:indice + 5]:
            encontrado_horario = re.search(r"\b([O0-2]?\d)\s*[h:]\s*([0-5]\d)\b", proxima, flags=re.IGNORECASE)
            if not encontrado_horario:
                continue
            dia, mes, ano = encontrado_data.groups()
            hora, minuto = encontrado_horario.groups()
            hora = hora.upper().replace("O", "0")
            if int(hora) > 23:
                continue
            return f"{ano}-{int(mes):02d}-{int(dia):02d}T{int(hora):02d}:{int(minuto):02d}"

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
        return False

    for linha in texto.splitlines():
        linha_normalizada = _normalizar_rotulo_ocr(linha)
        if not re.search(r"\b(banco\s+inter|intermedium|inter\s*(?:pix|pag|bank|s\.?a\.?))\b", linha_normalizada):
            continue
        if re.search(r"\b(instituicao|destino|recebedor|beneficiario|favorecido|para|chave pix)\b", linha_normalizada):
            continue
        return True
    if "instituicao banco inter" in texto_normalizado and "destino" in texto_normalizado and "origem" in texto_normalizado:
        return True
    return any(_normalizar_rotulo_ocr(linha) == "inter" for linha in texto.splitlines())


def _eh_rotulo_recebedor_inter(linha):
    linha_normalizada = _normalizar_rotulo_ocr(linha)
    return bool(re.search(
        r"\b(beneficiario|recebedor|favorecido|destino|para|dados do recebedor|chave pix do recebedor|instituicao do recebedor)\b",
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
        if re.search(r"\b(agencia|conta|tipo de conta|codigo|sessao|transacao|instituicao|cpf|cnpj)\b", candidato_normalizado):
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
        partes = re.split(r":|-", linha, maxsplit=1)
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


def _extrair_pagador(texto):
    linhas = [_normalizar_espacos(linha) for linha in texto.splitlines()]
    pagador_banpara = _extrair_pagador_banpara(linhas)
    if pagador_banpara:
        return pagador_banpara

    pagador_inter_whatsapp = _extrair_pagador_inter_whatsapp_rotulos_antes(linhas)
    if pagador_inter_whatsapp:
        return pagador_inter_whatsapp

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


def analisar_comprovante_pix(arquivo):
    texto = _extrair_texto_comprovante(arquivo)
    if not _normalizar_espacos(texto):
        return {
            "ok": False,
            "pagador": "",
            "valor": "",
            "data_pagamento": "",
            "texto_extraido": "",
            "texto_ocr_bruto": "",
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
        "texto_ocr_bruto": texto[:2000],
        "mensagem": (
            "Dados lidos automaticamente. Confira antes de salvar."
            if ok
            else "Nao foi possivel identificar dados principais. Preencha manualmente."
        ),
    }
