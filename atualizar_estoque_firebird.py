#!/usr/bin/env python
"""Sincronizacao incremental do estoque Firebird -> Django.

O modo padrao e dry-run. --aplicar habilita somente a atualizacao de
Produtos seguramente mapeados, com saldo absoluto e transacao atomica.
"""

import argparse
import copy
import os
import subprocess
from decimal import Decimal, InvalidOperation
from pathlib import Path

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "sistema.settings")

import django

django.setup()

from django.core.exceptions import ValidationError
from django.db import transaction

from estoque.models import Produto

BASE_DIR = Path(__file__).resolve().parent
ISQL_PADRAO = r"C:\Program Files (x86)\Firebird\Firebird_2_5\bin\isql.exe"
BANCO_PADRAO = r"C:\Ariramba\Dados\BDados.fdb"

REGRAS_MANUAIS = {
    "1.02.0017": ("42.000", "MANUAL: Copo 25/180 -> SALDOF"),
    "1.01.0107": ("0.000", "MANUAL: Rucula -> 0"),
    "1.02.0014": ("15.000", "MANUAL: Colher 1/50 -> 15 PCT"),
    "1.02.0018": ("25.000", "MANUAL: Copo 25/200 -> SALDOF"),
    "1.02.0019": ("3.000", "MANUAL: Copo 20/250 -> SALDOF"),
    "1.02.0020": ("4.000", "MANUAL: Copo 20/300 -> SALDOF"),
    "1.02.0060": ("0.000", "MANUAL: Saco Baguete 2Kg -> 0"),
    "1.02.0086": ("0.000", "MANUAL: Saco Baguete 1Kg -> 0"),
    "1.02.0144": ("18.000", "MANUAL: Copo 25/150 -> SALDOF"),
    "1.03.0040": ("1.000", "MANUAL: Lamen -> 1 UN"),
    "1.03.0322": ("0.000", "MANUAL: Conserva Pamp -> 0"),
    "1.12.0503": ("1.150", "MANUAL: Lamina Wilkinson -> 1.150 PCT"),
    "1.12.0523": ("0.900", "MANUAL: Lampada 9W -> 0.900 PCT"),
    "1.12.0526": ("0.000", "MANUAL: Lampada 12W -> 0"),
}
PRODUTOS_REVISAO_MANUAL = {
    "1.14.0004": "saldo Firebird -779,000 sem historico validavel",
}


def decimal_obrigatorio(texto, contexto, campo):
    if not (texto or "").strip():
        return None
    try:
        valor = Decimal((texto or "").strip().replace(",", "."))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{contexto}: {campo} invalido: {texto!r}")
    if not valor.is_finite():
        raise ValueError(f"{contexto}: {campo} deve ser finito")
    return valor.quantize(Decimal("0.001"))


def parse_linha(linha, numero):
    partes = [parte.strip() for parte in linha.split("|")]
    if len(partes) != 7:
        raise ValueError(f"linha {numero}: esperado formato com 7 campos")
    codigo, nome, unv, saldov, convf, unf, saldof = partes
    if not codigo:
        raise ValueError(f"linha {numero}: CODI vazio")
    return {
        "linha": numero,
        "codigo": codigo,
        "nome": nome,
        "unv": unv,
        "saldov": decimal_obrigatorio(saldov, f"linha {numero}", "SALDOV"),
        "convf": decimal_obrigatorio(convf, f"linha {numero}", "CONVF"),
        "unf": unf,
        "saldof": decimal_obrigatorio(saldof, f"linha {numero}", "SALDOF"),
    }


def extrair_firebird(isql_path, banco, usuario, senha):
    query = """
SET LIST OFF;
SET HEADING OFF;
SET ECHO OFF;
SET COUNT OFF;
SELECT
    TRIM(CODI) || '|' ||
    COALESCE(TRIM(NOME), '') || '|' ||
    COALESCE(TRIM(UNV), '') || '|' ||
    CAST(SALDOV AS VARCHAR(30)) || '|' ||
    CAST(CONVF AS VARCHAR(30)) || '|' ||
    COALESCE(TRIM(UNF), '') || '|' ||
    CAST(SALDOF AS VARCHAR(30))
FROM PRODUTOS
WHERE ST = 'A'
ORDER BY CODI;
QUIT;
"""
    resultado = subprocess.run(
        [isql_path, "-q", "-user", usuario, "-password", senha, banco],
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

    produtos = []
    erros = []
    for numero, linha in enumerate(resultado.stdout.splitlines(), start=1):
        linha = linha.strip()
        if not linha or linha.count("|") != 6:
            continue
        try:
            produtos.append(parse_linha(linha, numero))
        except ValueError as exc:
            erros.append(str(exc))
    if erros:
        raise ValueError("\n".join(erros))
    if len(produtos) != 750:
        raise ValueError(f"produtos ativos Firebird: {len(produtos)}; esperado: 750")
    codigos = [produto["codigo"] for produto in produtos]
    if len(codigos) != len(set(codigos)):
        raise ValueError("CODI duplicado na extracao Firebird")
    return produtos


def quantidade_e_regra(produto, firebird):
    codigo = (produto.codigo_legado or "").strip()
    if codigo in REGRAS_MANUAIS:
        quantidade, regra = REGRAS_MANUAIS[codigo]
        return Decimal(quantidade), regra

    unidade_django = str(produto.unidade_venda_1 or "").strip().upper()
    unv = firebird["unv"].upper()
    unf = firebird["unf"].upper()
    fator_novo = Decimal(str(produto.fator_conversao or 0))
    fator_antigo = firebird["convf"] or Decimal("0")

    if unidade_django and unidade_django == unv:
        if firebird["saldov"] is None:
            return None, "SALDOV nulo"
        return firebird["saldov"], "UN1 = UNV -> SALDOV"
    if unidade_django and unidade_django == unf:
        if firebird["saldof"] is None:
            return None, "SALDOF nulo"
        return firebird["saldof"], "UN1 = UNF -> SALDOF"
    if unidade_django == "MIL" and unv in ("ML", "MM"):
        if firebird["saldov"] is None:
            return None, "SALDOV nulo"
        return firebird["saldov"], "MIL equivalente a ML/MM -> SALDOV"
    if (
        unidade_django == "PCT"
        and unv in ("PC", "DZ", "CX", "CA")
        and fator_novo > 0
        and fator_antigo > 0
        and fator_novo == fator_antigo
    ):
        if firebird["saldov"] is None:
            return None, "SALDOV nulo"
        return firebird["saldov"], "PCT equivalente + mesmo fator -> SALDOV"
    return None, "UNIDADE/CONVERSAO REQUER ANALISE"


def validar_produto_para_quantidade(produto, quantidade):
    candidato = copy.copy(produto)
    candidato.quantidade = quantidade
    try:
        candidato.full_clean()
    except ValidationError as exc:
        return "; ".join(
            f"{campo}: {', '.join(mensagens)}"
            for campo, mensagens in exc.message_dict.items()
        )
    return ""


def carregar_comparacao(produtos_firebird):
    produtos_django = list(Produto.objects.filter(excluido=False, ativo=True))
    por_codigo = {}
    duplicados = []
    for produto in produtos_django:
        codigo = (produto.codigo_legado or "").strip()
        if not codigo:
            continue
        if codigo in por_codigo:
            duplicados.append(codigo)
        por_codigo[codigo] = produto
    if duplicados:
        raise ValueError(f"codigo_legado duplicado no Django: {sorted(set(duplicados))}")

    fire_por_codigo = {produto["codigo"]: produto for produto in produtos_firebird}
    alterados = []
    iguais = []
    revisar = []
    revisao_manual = []
    bloqueados = []
    for codigo, firebird in fire_por_codigo.items():
        produto = por_codigo.get(codigo)
        if produto is None:
            continue
        if codigo in PRODUTOS_REVISAO_MANUAL:
            revisao_manual.append({"produto": produto, "firebird": fire_por_codigo[codigo], "motivo": PRODUTOS_REVISAO_MANUAL[codigo]})
            continue
        quantidade, regra = quantidade_e_regra(produto, firebird)
        base = {"produto": produto, "firebird": firebird, "regra": regra}
        if quantidade is None:
            revisar.append(base)
            continue
        base["quantidade_firebird"] = quantidade
        base["diferenca"] = quantidade - (produto.quantidade or Decimal("0"))
        if quantidade != (produto.quantidade or Decimal("0")):
            motivo = validar_produto_para_quantidade(produto, quantidade)
            if motivo:
                base["motivo"] = motivo
                bloqueados.append(base)
            else:
                alterados.append(base)
        else:
            iguais.append(base)

    codigos_firebird = set(fire_por_codigo)
    codigos_django = set(por_codigo)
    fora = {
        "firebird_sem_django": [fire_por_codigo[codigo] for codigo in sorted(codigos_firebird - codigos_django)],
        "django_sem_firebird": [por_codigo[codigo] for codigo in sorted(codigos_django - codigos_firebird)],
        "django_sem_codigo": [produto for produto in produtos_django if not (produto.codigo_legado or "").strip()],
    }
    return {
        "produtos_django": produtos_django,
        "produtos_firebird": produtos_firebird,
        "alterados": alterados,
        "iguais": iguais,
        "revisar": revisar,
        "revisao_manual": revisao_manual,
        "bloqueados": bloqueados,
        "fora": fora,
    }


def fmt(valor):
    if valor is None:
        return "-"
    return f"{valor.quantize(Decimal('0.001')):.3f}"


def mostrar(comparacao, dry_run=True):
    alterados = comparacao["alterados"]
    total_alterados = sum((item["diferenca"] for item in alterados), Decimal("0"))
    fora = comparacao["fora"]
    fora_total = len(fora["firebird_sem_django"]) + len(fora["django_sem_firebird"]) + len(fora["django_sem_codigo"])
    if dry_run:
        print("Modo: DRY-RUN (nenhuma escrita sera executada)")
    else:
        print("Modo: APLICAR")
    print(f"Produtos ativos Firebird: {len(comparacao['produtos_firebird'])}")
    print(f"Produtos operacionais Django: {len(comparacao['produtos_django'])}")
    print(f"Produtos Django com codigo_legado: {sum(bool((p.codigo_legado or '').strip()) for p in comparacao['produtos_django'])}")
    print(f"Seguramente comparados: {len(alterados) + len(comparacao['iguais'])}")
    print(f"Alterados: {len(alterados)}")
    print(f"Sem alteracao: {len(comparacao['iguais'])}")
    print(f"Revisar unidade/conversao: {len(comparacao['revisar'])}")
    print(f"Revisao manual: {len(comparacao['revisao_manual'])}")
    print(f"Bloqueados por validacao: {len(comparacao['bloqueados'])}")
    print(f"Fora do universo/nao mapeados: {fora_total}")
    print(f"Diferenca liquida dos alterados: {fmt(total_alterados)}")

    print("\nALTERADOS:")
    for item in alterados:
        produto = item["produto"]
        firebird = item["firebird"]
        print(
            f"- {produto.codigo_legado} | Django {produto.nome} | "
            f"Firebird {firebird['nome']} | Django {fmt(produto.quantidade or Decimal('0'))} | "
            f"Firebird {fmt(item['quantidade_firebird'])} | Diferenca {fmt(item['diferenca'])} | "
            f"Unidade Django {produto.unidade_venda_1 or '-'} | UNV {firebird['unv'] or '-'} | "
            f"UNF {firebird['unf'] or '-'} | CONVF {fmt(firebird['convf'])} | Regra {item['regra']}"
        )
    print("\nREVISAR UNIDADE/CONVERSAO:")
    for item in comparacao["revisar"]:
        print(f"- {item['produto'].codigo_legado} | {item['produto'].nome} | Firebird {item['firebird']['nome']} | {item['regra']}")
    print("\nREVISAO MANUAL / EXCLUIDOS DA SINCRONIZACAO:")
    for item in comparacao["revisao_manual"]:
        produto = item["produto"]
        firebird = item["firebird"]
        print(
            f"- {produto.codigo_legado} | {produto.nome} | Django {fmt(produto.quantidade or Decimal('0'))} | "
            f"Firebird {fmt(firebird['saldov'])} | {item['motivo']}"
        )
    print("\nBLOQUEADOS POR VALIDACAO:")
    for item in comparacao["bloqueados"]:
        print(f"- {item['produto'].codigo_legado} | {item['produto'].nome} | {item['motivo']}")
    print("\nFORA DO UNIVERSO/NAO MAPEADOS:")
    for item in fora["firebird_sem_django"]:
        print(f"- FIREBIRD SEM DJANGO | {item['codigo']} | {item['nome']}")
    for produto in fora["django_sem_firebird"]:
        print(f"- DJANGO SEM FIREBIRD | {produto.codigo_legado} | {produto.nome}")
    for produto in fora["django_sem_codigo"]:
        print(f"- DJANGO SEM CODIGO | {produto.id} | {produto.nome}")


def aplicar(comparacao):
    if comparacao["bloqueados"]:
        raise ValueError("existem produtos bloqueados por validacao; aplicacao abortada")
    ids = {item["produto"].id for item in comparacao["alterados"]}
    esperados = {
        item["produto"].id: item["quantidade_firebird"]
        for item in comparacao["alterados"]
    }
    with transaction.atomic():
        produtos = {
            produto.id: produto
            for produto in Produto.objects.select_for_update().filter(id__in=ids)
        }
        if len(produtos) != len(ids):
            raise ValueError("produto alterado desapareceu antes da aplicacao")
        for item in comparacao["alterados"]:
            produto = produtos[item["produto"].id]
            if produto.excluido or not produto.ativo:
                raise ValueError(f"produto {produto.id} deixou de ser operacional")
            if (produto.codigo_legado or "").strip() != item["produto"].codigo_legado.strip():
                raise ValueError(f"codigo divergente no produto {produto.id}")
            if produto.quantidade != esperados[produto.id]:
                produto.quantidade = esperados[produto.id]
                produto.save(update_fields=["quantidade"])

        for produto in Produto.objects.filter(id__in=ids):
            if produto.quantidade != esperados[produto.id]:
                raise ValueError(f"reconciliacao falhou para produto {produto.id}")


def main():
    parser = argparse.ArgumentParser(description="Sincroniza estoque ativo do Firebird com Produto.")
    parser.add_argument("--aplicar", action="store_true", help="atualiza somente produtos seguramente mapeados")
    parser.add_argument("--isql", default=os.getenv("FIREBIRD_ISQL", ISQL_PADRAO))
    parser.add_argument("--banco", default=os.getenv("FIREBIRD_DB", BANCO_PADRAO))
    parser.add_argument("--usuario", default=os.getenv("FIREBIRD_USER", "SYSDBA"))
    parser.add_argument("--senha", default=os.getenv("FIREBIRD_PASSWORD", "masterkey"))
    args = parser.parse_args()

    produtos_firebird = extrair_firebird(args.isql, args.banco, args.usuario, args.senha)
    comparacao = carregar_comparacao(produtos_firebird)
    mostrar(comparacao, dry_run=not args.aplicar)
    if args.aplicar:
        # Rele a fonte e repita o preflight imediatamente antes da escrita.
        produtos_firebird = extrair_firebird(args.isql, args.banco, args.usuario, args.senha)
        comparacao = carregar_comparacao(produtos_firebird)
        aplicar(comparacao)
        print("SINCRONIZACAO CONCLUIDA")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"SINCRONIZACAO INVALIDA: {exc}")
        raise SystemExit(1)
