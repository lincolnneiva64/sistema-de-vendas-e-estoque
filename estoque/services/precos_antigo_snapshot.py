import re
import unicodedata
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.conf import settings

from estoque.models import Produto


SNAPSHOT_VERSION = 1
SNAPSHOT_SOURCE = "firebird.PRODUTOS.precos"
SNAPSHOT_FILENAME = "precos_firebird_snapshot.json"

FILTROS_MATCH_VALIDOS = {
    "todos",
    "exatos",
    "provaveis",
    "nao-encontrados",
    "duvidosos",
}
FILTROS_STATUS_VALIDOS = {"todos", "pendentes", "conferidos"}
FILTROS_VALIDOS = FILTROS_MATCH_VALIDOS | FILTROS_STATUS_VALIDOS

UNIDADES_IGNORADAS_SUGESTAO = {
    "kg",
    "kgs",
    "cx",
    "caixa",
    "caixas",
    "un",
    "und",
    "unid",
    "unidade",
    "unidades",
    "ml",
    "lt",
    "lts",
    "l",
    "litro",
    "litros",
    "pct",
    "pcte",
    "pacote",
    "pacotes",
    "fd",
    "fardo",
    "fardos",
    "gr",
    "g",
    "mg",
    "m",
    "mt",
    "mts",
    "metro",
    "metros",
    "dz",
    "duzia",
    "pc",
    "pcs",
    "peca",
    "pecas",
    "sc",
    "saco",
    "sacos",
    "bd",
    "bandeja",
    "vd",
    "vidro",
    "gf",
    "garrafa",
}
TOKENS_IGNORADOS_SUGESTAO = UNIDADES_IGNORADAS_SUGESTAO | {
    "de",
    "da",
    "do",
    "das",
    "dos",
    "com",
    "sem",
    "para",
    "por",
    "tipo",
    "tam",
    "tamanho",
    "x",
}


def caminho_snapshot_padrao():
    caminho_configurado = getattr(settings, "PRECOS_FIREBIRD_SNAPSHOT_PATH", None)
    if caminho_configurado:
        return Path(caminho_configurado)
    return Path(settings.BASE_DIR) / "estoque" / "data" / SNAPSHOT_FILENAME


def normalizar_nome(valor):
    valor = unicodedata.normalize("NFKD", valor or "")
    valor = "".join(ch for ch in valor if not unicodedata.combining(ch))
    valor = re.sub(r"[^a-zA-Z0-9]+", " ", valor).strip().lower()
    return re.sub(r"\s+", " ", valor)


def tokens_nome_sugestao(valor):
    texto = normalizar_nome(valor)
    texto = re.sub(r"(?<=\d)(?=[a-z])|(?<=[a-z])(?=\d)", " ", texto)
    tokens = []
    for token in texto.split():
        if token in TOKENS_IGNORADOS_SUGESTAO:
            continue
        if len(token) <= 1 and not token.isdigit():
            continue
        tokens.append(token)
    return tokens


def chave_nome_sugestao(valor):
    return " ".join(tokens_nome_sugestao(valor))


def carregar_snapshot_precos(caminho=None):
    import json

    caminho = Path(caminho or caminho_snapshot_padrao())
    if not caminho.exists():
        return {
            "disponivel": False,
            "erro": f"Snapshot nao encontrado em {caminho}.",
            "metadata": {},
            "produtos": [],
        }

    with caminho.open("r", encoding="utf-8") as arquivo:
        dados = json.load(arquivo)

    if dados.get("versao") != SNAPSHOT_VERSION:
        return {
            "disponivel": False,
            "erro": "Versao do snapshot de precos antigos nao suportada.",
            "metadata": dados,
            "produtos": [],
        }

    produtos = dados.get("produtos") or []
    return {
        "disponivel": True,
        "erro": "",
        "metadata": dados,
        "produtos": produtos,
    }


def _decimal(valor):
    if valor in (None, ""):
        return None
    try:
        return Decimal(str(valor))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _money(valor):
    valor = _decimal(valor)
    if valor is None:
        return "-"
    return f"R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _money_signed(valor):
    valor = _decimal(valor)
    if valor is None:
        return "-"
    if valor == 0:
        return _money(valor)
    sinal = "+" if valor > 0 else "-"
    return f"{sinal}{_money(abs(valor))}"


def _percentual(novo, antigo):
    novo_decimal = _decimal(novo)
    antigo_decimal = _decimal(antigo)
    if novo_decimal is None or antigo_decimal in (None, Decimal("0")):
        return None
    return ((novo_decimal - antigo_decimal) / antigo_decimal) * Decimal("100")


def _unidade(valor):
    return (valor or "").strip().upper()


def _precificacao_antiga(registro, conjunto):
    dados = registro.get(conjunto) or {}
    return {
        "compra": _decimal(dados.get("compra")),
        "vista": _decimal(dados.get("vista")),
        "prazo": _decimal(dados.get("prazo")),
        "terceiro": _decimal(dados.get("terceiro")),
    }


def _precificacao_antiga_formatada(registro, conjunto):
    precos = _precificacao_antiga(registro, conjunto)
    return {
        chave: {"valor": valor, "fmt": _money(valor)}
        for chave, valor in precos.items()
    }


def _precos_novos(produto):
    return {
        "compra": _decimal(produto.preco_compra),
        "vista": _decimal(produto.preco_vista),
        "prazo": _decimal(produto.preco_prazo),
    }


def _precos_fracionados_novos(produto):
    return {
        "compra": _decimal(produto.preco_compra_fracionado),
        "vista": _decimal(produto.preco_vista_fracionado),
        "prazo": _decimal(produto.preco_prazo_fracionado),
    }


def _avaliar_unidade(produto, registro_antigo):
    unidade_principal_nova = _unidade(produto.unidade_venda_1 or produto.unidade_compra)
    unidade_compra_nova = _unidade(produto.unidade_compra)
    unidade_fracionada_nova = _unidade(produto.unidade_venda_2)
    unidades_novas_principais = {u for u in (unidade_principal_nova, unidade_compra_nova) if u}
    unidade_antiga_principal = _unidade(registro_antigo.get("unv"))
    unidade_antiga_fracionada = _unidade(registro_antigo.get("unf"))

    referencia_fracionada = None
    if produto.vende_fracionado and unidade_fracionada_nova and unidade_fracionada_nova == unidade_antiga_fracionada:
        referencia_fracionada = "fracionado"

    if unidade_antiga_principal and unidade_antiga_principal in unidades_novas_principais:
        return {
            "situacao": "seguro",
            "conjunto": "principal",
            "motivo": "unidade principal do novo coincide com UNV do antigo",
            "referencia_fracionada": referencia_fracionada,
        }

    if unidade_antiga_fracionada and unidade_antiga_fracionada in unidades_novas_principais:
        return {
            "situacao": "seguro",
            "conjunto": "fracionado",
            "motivo": "unidade principal do novo coincide com UNF do antigo",
            "referencia_fracionada": None,
        }

    return {
        "situacao": "duvidoso_unidade",
        "conjunto": None,
        "motivo": "unidades do novo nao coincidem com UNV nem UNF do antigo",
        "referencia_fracionada": referencia_fracionada,
    }


def _indices_snapshot(snapshot):
    por_codigo = {}
    por_nome = {}
    por_chave_sugestao = {}

    for registro in snapshot["produtos"]:
        codigo = str(registro.get("codigo") or "").strip()
        nome_normalizado = normalizar_nome(registro.get("nome") or "")
        chave_sugestao = chave_nome_sugestao(registro.get("nome") or "")
        if codigo:
            por_codigo[codigo] = registro
        if nome_normalizado:
            por_nome.setdefault(nome_normalizado, []).append(registro)
        if chave_sugestao:
            por_chave_sugestao.setdefault(chave_sugestao, []).append(registro)

    return por_codigo, por_nome, por_chave_sugestao


def _resolver_match(produto, por_codigo, por_nome):
    codigo_legado = (produto.codigo_legado or "").strip()
    if codigo_legado and codigo_legado in por_codigo:
        return "exato", por_codigo[codigo_legado], []

    candidatos_nome = por_nome.get(normalizar_nome(produto.nome), [])
    if len(candidatos_nome) == 1:
        return "provavel", candidatos_nome[0], candidatos_nome
    if len(candidatos_nome) > 1:
        return "ambiguo", None, candidatos_nome
    return "nao_encontrado", None, []


def buscar_registro_antigo_por_codigo(snapshot, codigo):
    codigo = str(codigo or "").strip()
    if not codigo:
        return None
    por_codigo, _por_nome, _por_chave = _indices_snapshot(snapshot)
    return por_codigo.get(codigo)


def produto_tem_match_seguro(produto, snapshot):
    por_codigo, por_nome, _por_chave = _indices_snapshot(snapshot)
    match_tipo, registro_antigo, candidatos = _resolver_match(produto, por_codigo, por_nome)
    linha = _linha_produto(produto, match_tipo, registro_antigo, candidatos)
    return linha["situacao"] == "seguro"


def _numeros_tokens(tokens):
    return {token for token in tokens if token.isdigit()}


def _avaliar_candidato_possivel(nome_produto, nome_antigo):
    tokens_produto = tokens_nome_sugestao(nome_produto)
    tokens_antigo = tokens_nome_sugestao(nome_antigo)
    if not tokens_produto or not tokens_antigo:
        return None

    produto_set = set(tokens_produto)
    antigo_set = set(tokens_antigo)
    intersecao = produto_set & antigo_set
    if len(intersecao) < 2:
        return None

    numeros_produto = _numeros_tokens(produto_set)
    numeros_antigo = _numeros_tokens(antigo_set)
    if numeros_produto and numeros_antigo:
        menor_conjunto = numeros_produto if len(numeros_produto) <= len(numeros_antigo) else numeros_antigo
        if not menor_conjunto.issubset(numeros_produto & numeros_antigo):
            return None

    cobertura_menor = len(intersecao) / min(len(produto_set), len(antigo_set))
    cobertura_maior = len(intersecao) / max(len(produto_set), len(antigo_set))
    if cobertura_menor < 0.6:
        return None
    return {
        "nivel": "possivel",
        "score": cobertura_maior,
        "motivo": "compartilha maioria dos tokens significativos sem conflito de quantidade/volume",
    }


def _sugestao_para_registro(registro, nivel, motivo, score):
    return {
        "codigo": registro.get("codigo"),
        "nome": registro.get("nome"),
        "st": registro.get("st"),
        "unv": registro.get("unv"),
        "unf": registro.get("unf"),
        "convf": registro.get("convf"),
        "principal": _precificacao_antiga_formatada(registro, "principal"),
        "fracionado": _precificacao_antiga_formatada(registro, "fracionado"),
        "nivel": nivel,
        "motivo": motivo,
        "score": score,
    }


def sugerir_correspondencias_antigas(produto, snapshot, por_chave_sugestao=None, limite=3):
    por_chave_sugestao = por_chave_sugestao or _indices_snapshot(snapshot)[2]
    chave_produto = chave_nome_sugestao(produto.nome)
    sugestoes = []
    vistos = set()

    registros_mesma_chave = por_chave_sugestao.get(chave_produto, []) if chave_produto else []
    if len(registros_mesma_chave) == 1:
        registro = registros_mesma_chave[0]
        sugestoes.append(
            _sugestao_para_registro(
                registro,
                "forte",
                "nome igual apos remover unidades/apresentacao, candidato unico",
                1,
            )
        )
        vistos.add(registro.get("codigo"))
    elif len(registros_mesma_chave) > 1:
        for registro in registros_mesma_chave:
            sugestoes.append(
                _sugestao_para_registro(
                    registro,
                    "possivel",
                    "nome igual apos remover unidades/apresentacao, mas ha mais de um candidato",
                    1,
                )
            )
            vistos.add(registro.get("codigo"))

    for registro in snapshot["produtos"]:
        codigo = registro.get("codigo")
        if codigo in vistos:
            continue
        avaliacao = _avaliar_candidato_possivel(produto.nome, registro.get("nome") or "")
        if not avaliacao:
            continue
        sugestoes.append(
            _sugestao_para_registro(
                registro,
                avaliacao["nivel"],
                avaliacao["motivo"],
                avaliacao["score"],
            )
        )

    sugestoes.sort(key=lambda item: (item["nivel"] != "forte", -item["score"], item["nome"] or ""))
    return sugestoes[:limite]


def _montar_comparacao(precos_novos, precos_antigos):
    comparacao = {}
    desvio_total = Decimal("0")

    for chave in ("compra", "vista", "prazo"):
        novo = precos_novos[chave]
        antigo = precos_antigos[chave]
        diferenca = novo - antigo if novo is not None and antigo is not None else None
        percentual = _percentual(novo, antigo)
        if diferenca is not None:
            desvio_total += abs(diferenca)
        comparacao[chave] = {
            "novo": novo,
            "antigo": antigo,
            "diferenca": diferenca,
            "percentual": percentual,
            "novo_fmt": _money(novo),
            "antigo_fmt": _money(antigo),
            "diferenca_fmt": _money_signed(diferenca) if diferenca is not None else "-",
            "percentual_fmt": f"{percentual:+.1f}%" if percentual is not None else "-",
        }

    return comparacao, desvio_total


def _linha_produto(produto, match_tipo, registro_antigo, candidatos):
    linha = {
        "produto": produto,
        "match_tipo": match_tipo,
        "situacao": match_tipo,
        "registro_antigo": registro_antigo,
        "candidatos": candidatos,
        "avaliacao_unidade": None,
        "comparacao": None,
        "comparacao_fracionada": None,
        "sugestoes_correspondencia": [],
        "desvio_total": None,
        "desvio_total_fmt": "-",
    }

    if not registro_antigo:
        return linha

    avaliacao = _avaliar_unidade(produto, registro_antigo)
    linha["avaliacao_unidade"] = avaliacao
    if avaliacao["situacao"] != "seguro":
        linha["situacao"] = "duvidoso"
        return linha

    precos_novos = _precos_novos(produto)
    precos_antigos = _precificacao_antiga(registro_antigo, avaliacao["conjunto"])
    comparacao, desvio_total = _montar_comparacao(precos_novos, precos_antigos)
    linha["comparacao"] = comparacao
    linha["desvio_total"] = desvio_total
    linha["desvio_total_fmt"] = _money(desvio_total)
    linha["situacao"] = "seguro"

    if avaliacao.get("referencia_fracionada"):
        linha["comparacao_fracionada"], _ = _montar_comparacao(
            _precos_fracionados_novos(produto),
            _precificacao_antiga(registro_antigo, "fracionado"),
        )

    return linha


def _passa_filtro(linha, filtro_match, filtro_status):
    if filtro_status == "pendentes" and linha["produto"].preco_conferido:
        return False
    if filtro_status == "conferidos" and not linha["produto"].preco_conferido:
        return False

    if filtro_match == "todos":
        return True
    if filtro_match == "exatos":
        return linha["match_tipo"] == "exato" and linha["situacao"] == "seguro"
    if filtro_match == "provaveis":
        return linha["match_tipo"] == "provavel" and linha["situacao"] == "seguro"
    if filtro_match == "nao-encontrados":
        return linha["match_tipo"] == "nao_encontrado"
    if filtro_match == "duvidosos":
        return linha["match_tipo"] == "ambiguo" or linha["situacao"] == "duvidoso"
    return True


def gerar_contexto_conferencia_precos(params=None):
    params = params or {}
    filtro = params.get("filtro") or "todos"
    status_preco = params.get("status_preco") or "todos"
    if filtro in FILTROS_STATUS_VALIDOS - {"todos"} and status_preco == "todos":
        status_preco = filtro
        filtro = "todos"
    if filtro not in FILTROS_MATCH_VALIDOS:
        filtro = "todos"
    if status_preco not in FILTROS_STATUS_VALIDOS:
        status_preco = "todos"
    busca = (params.get("busca") or "").strip()
    ordenar = params.get("ordenar") or "nome"

    snapshot = carregar_snapshot_precos()
    por_codigo, por_nome, por_chave_sugestao = _indices_snapshot(snapshot)

    produtos = Produto.objects.filter(excluido=False, ativo=True).order_by("nome", "id")
    if busca:
        produtos = produtos.filter(
            nome__icontains=busca
        ) | Produto.objects.filter(excluido=False, ativo=True, codigo_legado__icontains=busca)
        produtos = produtos.order_by("nome", "id")

    linhas_todas = []
    for produto in produtos:
        match_tipo, registro_antigo, candidatos = _resolver_match(produto, por_codigo, por_nome)
        linha = _linha_produto(produto, match_tipo, registro_antigo, candidatos)
        if linha["match_tipo"] == "nao_encontrado" or linha["match_tipo"] == "ambiguo" or linha["situacao"] == "duvidoso":
            linha["sugestoes_correspondencia"] = sugerir_correspondencias_antigas(
                produto,
                snapshot,
                por_chave_sugestao=por_chave_sugestao,
            )
        linhas_todas.append(linha)

    resumo = {
        "total": len(linhas_todas),
        "pendentes": sum(1 for linha in linhas_todas if not linha["produto"].preco_conferido),
        "conferidos": sum(1 for linha in linhas_todas if linha["produto"].preco_conferido),
        "exatos": sum(1 for linha in linhas_todas if linha["match_tipo"] == "exato" and linha["situacao"] == "seguro"),
        "provaveis": sum(1 for linha in linhas_todas if linha["match_tipo"] == "provavel" and linha["situacao"] == "seguro"),
        "nao_encontrados": sum(1 for linha in linhas_todas if linha["match_tipo"] == "nao_encontrado"),
        "duvidosos": sum(1 for linha in linhas_todas if linha["match_tipo"] == "ambiguo" or linha["situacao"] == "duvidoso"),
    }

    linhas = [linha for linha in linhas_todas if _passa_filtro(linha, filtro, status_preco)]
    if ordenar == "desvio":
        linhas.sort(key=lambda linha: linha["desvio_total"] or Decimal("-1"), reverse=True)

    return {
        "snapshot": snapshot,
        "linhas": linhas,
        "resumo": resumo,
        "filtro_atual": filtro,
        "status_preco_atual": status_preco,
        "busca_atual": busca,
        "ordenar_atual": ordenar,
        "filtros": FILTROS_MATCH_VALIDOS,
        "filtros_status_preco": FILTROS_STATUS_VALIDOS,
    }
