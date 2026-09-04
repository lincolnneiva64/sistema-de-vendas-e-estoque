import os
import subprocess
from dataclasses import dataclass
from decimal import Decimal
from importlib import import_module
from uuid import uuid4

from django.db import transaction


AREAS = {
    "estoque": "Atualizar Estoque",
    "receber": "Atualizar Contas a Receber",
    "pagar": "Atualizar Contas a Pagar",
    "tudo": "Atualizar Tudo",
}
CONFIRMACAO_TEXTO = "CONFIRMAR"
SESSION_TOKENS_KEY = "sincronizacao_firebird_tokens"
ISQL_PADRAO = r"C:\FIREBIRD_25_X64_MIGRACAO\bin\isql.exe"
BANCO_PADRAO = r"C:\Ariramba\Dados\BDados.fdb"
TABELAS_FIREBIRD_POR_AREA = {
    "estoque": {"PRODUTOS"},
    "receber": {"CRCLIENTES", "CLIENTES"},
    "pagar": {"CPAGAR"},
}


@dataclass
class ResultadoAplicacao:
    area: str
    titulo: str
    resumo: dict


def credenciais_firebird():
    return {
        "isql_path": os.getenv("FIREBIRD_ISQL", ISQL_PADRAO),
        "banco": os.getenv("FIREBIRD_DB", BANCO_PADRAO),
        "usuario": os.getenv("FIREBIRD_USER", "SYSDBA"),
        "senha": os.getenv("FIREBIRD_PASSWORD", "masterkey"),
    }


def registrar_token_previa(session, area):
    token = uuid4().hex
    tokens = session.get(SESSION_TOKENS_KEY, {})
    tokens[token] = area
    session[SESSION_TOKENS_KEY] = tokens
    session.modified = True
    return token


def consumir_token_previa(session, token, area):
    tokens = session.get(SESSION_TOKENS_KEY, {})
    if tokens.get(token) != area:
        return False
    tokens.pop(token, None)
    session[SESSION_TOKENS_KEY] = tokens
    session.modified = True
    return True


def validar_fonte_firebird(area=None):
    cfg = credenciais_firebird()
    areas = ("estoque", "receber", "pagar") if area in (None, "tudo") else (area,)
    tabelas_esperadas = set().union(*(TABELAS_FIREBIRD_POR_AREA[item] for item in areas))
    tabela_sql = ", ".join(f"'{tabela}'" for tabela in sorted(tabelas_esperadas))
    query = """
SET LIST OFF;
SET HEADING OFF;
SET ECHO OFF;
SET COUNT OFF;
SELECT 'TABLE=' || TRIM(RDB$RELATION_NAME)
FROM RDB$RELATIONS
WHERE RDB$RELATION_NAME IN ({tabela_sql});
QUIT;
""".format(tabela_sql=tabela_sql)
    caminho_configurado = os.path.normcase(os.path.normpath(os.path.abspath(cfg["banco"])))
    caminho_operacional = os.path.normcase(os.path.normpath(os.path.abspath(BANCO_PADRAO)))
    if caminho_configurado != caminho_operacional:
        raise ValueError(
            f"Fonte Firebird bloqueada: banco configurado diferente do operacional: {cfg['banco']}"
        )
    resultado = subprocess.run(
        [cfg["isql_path"], "-q", "-user", cfg["usuario"], "-password", cfg["senha"], cfg["banco"]],
        input=query,
        text=True,
        capture_output=True,
        encoding="cp1252",
        errors="replace",
        check=False,
    )
    if resultado.returncode != 0:
        raise RuntimeError(
            f"isql falhou ({resultado.returncode}): "
            f"{resultado.stderr.strip() or resultado.stdout.strip()}"
        )

    marcadores = _extrair_marcadores(resultado.stdout)
    tabelas_encontradas = marcadores.get("TABLE", set())
    faltantes = sorted(tabelas_esperadas - tabelas_encontradas)
    if faltantes:
        raise ValueError(f"Fonte Firebird bloqueada: tabelas esperadas ausentes: {', '.join(faltantes)}")
    return caminho_configurado


def gerar_previa(area, validar_fonte=True):
    if validar_fonte:
        validar_fonte_firebird(area)
    if area == "tudo":
        etapas = []
        for etapa in ("estoque", "receber", "pagar"):
            etapas.append(gerar_previa(etapa, validar_fonte=False))
        return _previa_tudo(etapas)
    if area == "estoque":
        return _previa_estoque()
    if area == "receber":
        return _previa_receber()
    if area == "pagar":
        return _previa_pagar()
    raise ValueError("area de sincronizacao invalida")


def aplicar_com_releitura(area):
    if area == "tudo":
        # Primeiro reler e validar as tres areas; so depois abrir escrita.
        previas = [gerar_previa(etapa) for etapa in ("estoque", "receber", "pagar")]
        for preview in previas:
            _validar_previa_antes_aplicar(preview)
        with transaction.atomic():
            resultados = [_aplicar_previa(preview, validar_preflight=False) for preview in previas]
        resumo = _resumo_tudo([resultado.resumo for resultado in resultados])
        return ResultadoAplicacao(area="tudo", titulo=AREAS["tudo"], resumo=resumo)

    preview = gerar_previa(area)
    return _aplicar_previa(preview)


def _validar_previa_antes_aplicar(preview):
    if preview["area"] == "pagar":
        validar_exclusoes = getattr(_engine_pagar(), "validar_exclusoes_explicitamente_configuradas", None)
        if validar_exclusoes:
            validar_exclusoes(preview["dados_aplicacao"]["ignoradas"])


def _aplicar_previa(preview, validar_preflight=True):
    if validar_preflight:
        _validar_previa_antes_aplicar(preview)
    dados = preview["dados_aplicacao"]
    area = preview["area"]
    if area == "estoque":
        _engine_estoque().aplicar(dados["comparacao"])
    elif area == "receber":
        _engine_receber().aplicar(
            dados["contas"],
            dados["novas"],
            dados["alteradas"],
            [conta["numero_legado"] for conta in dados["contas"]],
            dados.get("quitadas", []),
        )
    elif area == "pagar":
        _engine_pagar().aplicar(
            dados["contas"],
            dados["novas"],
            dados["alteradas"],
            [conta["documento"] for conta in dados["contas"]],
        )
    else:
        raise ValueError("area de sincronizacao invalida")
    return ResultadoAplicacao(area=area, titulo=preview["titulo"], resumo=preview["resumo"])


def _previa_estoque():
    engine = _engine_estoque()
    cfg = credenciais_firebird()
    produtos_firebird = engine.extrair_firebird(**cfg)
    comparacao = engine.carregar_comparacao(produtos_firebird)
    fora = comparacao["fora"]
    resumo = {
        "novos": 0,
        "alterados": len(comparacao["alterados"]),
        "sem_alteracao": len(comparacao["iguais"]),
        "fora_escopo": len(fora["firebird_sem_django"]) + len(fora["django_sem_firebird"]) + len(fora["django_sem_codigo"]),
        "problemas": len(comparacao["revisar"]) + len(comparacao["revisao_manual"]) + len(comparacao["bloqueados"]),
        "possiveis_fechamentos": 0,
    }
    secoes = [
        _secao("Novos", []),
        _secao("Alterados", [_item_estoque_alterado(item) for item in comparacao["alterados"]]),
        _secao("Sem alteracao", [_item_estoque_igual(item) for item in comparacao["iguais"][:30]], total=len(comparacao["iguais"])),
        _secao("Fora do escopo / nao mapeados", _itens_estoque_fora(fora)),
        _secao("Problemas de unidade / conversao", [_item_estoque_problema(item) for item in comparacao["revisar"]]),
        _secao("Revisao manual / excluidos", [_item_estoque_manual(item) for item in comparacao["revisao_manual"]]),
        _secao("Bloqueados por validacao", [_item_estoque_bloqueado(item) for item in comparacao["bloqueados"]]),
        _secao("Possiveis fechamentos", []),
    ]
    return _preview("estoque", resumo, secoes, {"comparacao": comparacao})


def _previa_receber():
    engine = _engine_receber()
    cfg = credenciais_firebird()
    clientes, cliente_ids = engine.carregar_universo()
    contas, fora = engine.extrair_firebird(**cfg, cliente_ids=cliente_ids)
    novas, alteradas, sem_alteracao, quitadas, alertas = engine.comparar(contas, **cfg)
    resumo = {
        "novos": len(novas),
        "alterados": len(alteradas),
        "sem_alteracao": len(sem_alteracao),
        "fora_escopo": len(fora),
        "problemas": 0,
        "possiveis_fechamentos": len(quitadas) + len(alertas),
        "quitadas_confirmadas": len(quitadas),
        "alertas_fechamento": len(alertas),
    }
    secoes = [
        _secao("Novos", [_item_receber_nova(conta, clientes) for conta in novas]),
        _secao("Alterados", [_item_receber_alterada(item, clientes) for item in alteradas]),
        _secao("Sem alteracao", [_item_receber_igual(item, clientes) for item in sem_alteracao[:30]], total=len(sem_alteracao)),
        _secao("Fora do escopo / nao mapeados", [_item_fora_receber(conta) for conta in fora]),
        _secao("Problemas de unidade / conversao", []),
        _secao("Quitadas no Firebird", [_item_receber_quitada(item) for item in quitadas], destaque=True),
        _secao("Alertas: quitacao nao confirmada no Firebird", [_item_receber_fechamento(conta) for conta in alertas], destaque=True),
    ]
    return _preview("receber", resumo, secoes, {
        "contas": contas,
        "novas": novas,
        "alteradas": alteradas,
        "quitadas": quitadas,
    })


def _previa_pagar():
    engine = _engine_pagar()
    cfg = credenciais_firebird()
    contas, ignoradas = engine.extrair_firebird(**cfg)
    fornecedores, novas, alteradas, sem_alteracao, fechadas = engine.carregar_django(contas)
    resumo = {
        "novos": len(novas),
        "alterados": len(alteradas),
        "sem_alteracao": len(sem_alteracao),
        "fora_escopo": len(ignoradas),
        "problemas": 0,
        "possiveis_fechamentos": len(fechadas),
    }
    secoes = [
        _secao("Novos", [_item_pagar_nova(conta, fornecedores) for conta in novas]),
        _secao("Alterados", [_item_pagar_alterada(item, fornecedores) for item in alteradas]),
        _secao("Sem alteracao", [_item_pagar_igual(item, fornecedores) for item in sem_alteracao[:30]], total=len(sem_alteracao)),
        _secao("Fora do escopo / nao mapeados", [_item_fora_pagar(conta) for conta in ignoradas]),
        _secao("Problemas de unidade / conversao", []),
        _secao("Possiveis fechamentos", [_item_pagar_fechamento(conta) for conta in fechadas], destaque=True),
    ]
    return _preview("pagar", resumo, secoes, {
        "contas": contas,
        "ignoradas": ignoradas,
        "novas": novas,
        "alteradas": alteradas,
    })


def _previa_tudo(etapas):
    return {
        "area": "tudo",
        "titulo": AREAS["tudo"],
        "resumo": _resumo_tudo([etapa["resumo"] for etapa in etapas]),
        "secoes": [],
        "etapas": etapas,
        "dados_aplicacao": {},
    }


def _resumo_tudo(resumos):
    chaves = ("novos", "alterados", "sem_alteracao", "fora_escopo", "problemas", "possiveis_fechamentos", "quitadas_confirmadas", "alertas_fechamento")
    return {chave: sum(item.get(chave, 0) for item in resumos) for chave in chaves}


def _extrair_marcadores(texto):
    marcadores = {}
    for linha in texto.splitlines():
        linha = linha.strip()
        if "=" not in linha:
            continue
        chave, valor = linha.split("=", 1)
        chave = chave.strip()
        valor = valor.strip()
        if chave == "TABLE" and valor:
            if chave == "TABLE":
                marcadores.setdefault(chave, set()).add(valor.upper())
    return marcadores


def _preview(area, resumo, secoes, dados):
    return {
        "area": area,
        "titulo": AREAS[area],
        "resumo": resumo,
        "secoes": secoes,
        "etapas": [],
        "dados_aplicacao": dados,
    }


def _secao(titulo, itens, total=None, destaque=False):
    return {
        "titulo": titulo,
        "itens": itens,
        "total": len(itens) if total is None else total,
        "destaque": destaque,
    }


def _item(titulo, detalhes):
    return {"titulo": titulo, "detalhes": [str(item) for item in detalhes if item not in (None, "")]}


def _dinheiro(valor):
    return f"R$ {(valor or Decimal('0.00')).quantize(Decimal('0.01')):.2f}".replace(".", ",")


def _qtd(valor):
    if valor is None:
        return "-"
    return f"{valor.quantize(Decimal('0.001')):.3f}"


def _item_estoque_alterado(item):
    produto = item["produto"]
    return _item(
        f"{produto.codigo_legado} - {produto.nome}",
        [
            f"Django: {_qtd(produto.quantidade or Decimal('0'))}",
            f"Firebird: {_qtd(item['quantidade_firebird'])}",
            f"Diferenca: {_qtd(item['diferenca'])}",
            f"Regra: {item['regra']}",
        ],
    )


def _item_estoque_igual(item):
    produto = item["produto"]
    return _item(f"{produto.codigo_legado} - {produto.nome}", [f"Quantidade: {_qtd(produto.quantidade or Decimal('0'))}"])


def _item_estoque_problema(item):
    produto = item["produto"]
    return _item(f"{produto.codigo_legado} - {produto.nome}", [item["regra"]])


def _item_estoque_manual(item):
    produto = item["produto"]
    return _item(f"{produto.codigo_legado} - {produto.nome}", [item["motivo"]])


def _item_estoque_bloqueado(item):
    produto = item["produto"]
    return _item(f"{produto.codigo_legado} - {produto.nome}", [item["motivo"]])


def _itens_estoque_fora(fora):
    itens = []
    itens.extend(_item(f"Firebird sem Django: {item['codigo']}", [item["nome"]]) for item in fora["firebird_sem_django"][:50])
    itens.extend(_item(f"Django sem Firebird: {produto.codigo_legado}", [produto.nome]) for produto in fora["django_sem_firebird"][:50])
    itens.extend(_item(f"Django sem codigo: {produto.id}", [produto.nome]) for produto in fora["django_sem_codigo"][:50])
    return itens


def _item_receber_nova(conta, clientes):
    return _item(conta["numero_legado"], [clientes[conta["cliente_id"]].nome, conta["vencimento"], _dinheiro(conta["saldo"])])


def _item_receber_alterada(item, clientes):
    django_conta, firebird, diferencas = item
    return _item(firebird["numero_legado"], [clientes[firebird["cliente_id"]].nome, f"Django: {_dinheiro(django_conta.valor_em_aberto)}", f"Firebird: {_dinheiro(firebird['saldo'])}", f"Campos: {', '.join(diferencas)}"])


def _item_receber_igual(item, clientes):
    _, firebird, _ = item
    return _item(firebird["numero_legado"], [clientes[firebird["cliente_id"]].nome, _dinheiro(firebird["saldo"])])


def _item_receber_fechamento(conta):
    nome = conta.cliente.nome if conta.cliente else "Cliente nao informado"
    return _item(
        conta.numero_legado,
        [
            nome,
            f"Saldo Django: {_dinheiro(conta.valor_em_aberto)}",
            "Nao aparece mais entre as contas abertas do Firebird",
            "Quitacao nao confirmada no Firebird; nao sera fechada",
        ],
    )


def _item_receber_quitada(item):
    conta, firebird = item
    nome = conta.cliente.nome if conta.cliente else "Cliente nao informado"
    return _item(
        conta.numero_legado,
        [
            nome,
            f"Conta confirmada em CRCLIENTES com VREST: {_dinheiro(firebird['saldo'])}",
            f"Valor original preservado: {_dinheiro(conta.valor_original)}",
            "Sera marcada como quitada; nenhum recebimento historico sera criado",
        ],
    )


def _item_fora_receber(conta):
    return _item(conta["numero_legado"], [f"CODI {conta['codigo'] or '(vazio)'}", conta["nome"]])


def _item_pagar_nova(conta, fornecedores):
    return _item(conta["documento"], [fornecedores[conta["fornecedor_id"]].nome, conta["vencimento"], _dinheiro(conta["valres"])])


def _item_pagar_alterada(item, fornecedores):
    django_conta, firebird, diferencas = item
    return _item(firebird["documento"], [fornecedores[firebird["fornecedor_id"]].nome, f"Django: {_dinheiro(django_conta.valor_em_aberto)}", f"Firebird: {_dinheiro(firebird['valres'])}", f"Campos: {', '.join(diferencas)}"])


def _item_pagar_igual(item, fornecedores):
    _, firebird = item
    return _item(firebird["documento"], [fornecedores[firebird["fornecedor_id"]].nome, _dinheiro(firebird["valres"])])


def _item_pagar_fechamento(conta):
    nome = conta.fornecedor.nome if conta.fornecedor else "Fornecedor nao informado"
    return _item(conta.documento_legado, [nome, f"Saldo Django: {_dinheiro(conta.valor_em_aberto)}", "Nao sera fechado automaticamente"])


def _item_fora_pagar(conta):
    motivo = conta.get("motivo_ignorada") or "Fornecedor fora do mapeamento autorizado"
    return _item(conta["documento"], [f"CODI {conta['codi'] or '(vazio)'}", conta["nome"], motivo])


def _engine_estoque():
    return import_module("atualizar_estoque_firebird")


def _engine_receber():
    return import_module("atualizar_contas_receber_firebird")


def _engine_pagar():
    return import_module("atualizar_contas_pagar_firebird")
