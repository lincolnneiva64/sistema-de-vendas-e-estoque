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
ISQL_PADRAO = r"C:\Program Files (x86)\Firebird\Firebird_2_5\bin\isql.exe"
BANCO_PADRAO = r"C:\Ariramba\Dados\BDados.fdb"
# Trava temporaria de identidade/consistencia da fonte Firebird.
# 750 era a referencia validada anteriormente. Em 03/09/2026, a base
# operacional do desktop foi verificada diretamente e apresentou 748 produtos
# ativos. 748 passa a ser a assinatura operacional esperada; a trava continua
# impedindo a operacao contra uma copia Firebird diferente.
PRODUTOS_ATIVOS_FIREBIRD_ESPERADO = 748


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


def validar_fonte_firebird():
    cfg = credenciais_firebird()
    query = """
SET LIST OFF;
SET HEADING OFF;
SET ECHO OFF;
SET COUNT OFF;
SELECT COUNT(*) FROM PRODUTOS WHERE ST = 'A';
QUIT;
"""
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

    quantidade = _extrair_inteiro(resultado.stdout)
    if quantidade != PRODUTOS_ATIVOS_FIREBIRD_ESPERADO:
        raise ValueError(
            f"Fonte Firebird bloqueada: produtos ativos Firebird: {quantidade}; "
            f"esperado: {PRODUTOS_ATIVOS_FIREBIRD_ESPERADO}"
        )
    return quantidade


def gerar_previa(area, validar_fonte=True):
    if validar_fonte:
        validar_fonte_firebird()
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
    novas, alteradas, sem_alteracao, fechadas = engine.comparar(contas)
    resumo = {
        "novos": len(novas),
        "alterados": len(alteradas),
        "sem_alteracao": len(sem_alteracao),
        "fora_escopo": len(fora),
        "problemas": 0,
        "possiveis_fechamentos": len(fechadas),
    }
    secoes = [
        _secao("Novos", [_item_receber_nova(conta, clientes) for conta in novas]),
        _secao("Alterados", [_item_receber_alterada(item, clientes) for item in alteradas]),
        _secao("Sem alteracao", [_item_receber_igual(item, clientes) for item in sem_alteracao[:30]], total=len(sem_alteracao)),
        _secao("Fora do escopo / nao mapeados", [_item_fora_receber(conta) for conta in fora]),
        _secao("Problemas de unidade / conversao", []),
        _secao("Alertas: nao aparecem entre contas abertas do Firebird", [_item_receber_fechamento(conta) for conta in fechadas], destaque=True),
    ]
    return _preview("receber", resumo, secoes, {
        "contas": contas,
        "novas": novas,
        "alteradas": alteradas,
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
    chaves = ("novos", "alterados", "sem_alteracao", "fora_escopo", "problemas", "possiveis_fechamentos")
    return {chave: sum(item.get(chave, 0) for item in resumos) for chave in chaves}


def _extrair_inteiro(texto):
    for linha in texto.splitlines():
        linha = linha.strip()
        if linha.isdigit():
            return int(linha)
    raise ValueError("nao foi possivel validar a fonte Firebird: contagem de produtos ativa ausente")


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
            "Nao sera fechado automaticamente",
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
