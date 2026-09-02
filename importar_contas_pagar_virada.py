#!/usr/bin/env python
"""Dry-run da importacao de contas a pagar legadas do Firebird.

Por padrao, este script somente le o arquivo de origem e consulta o banco via ORM.
A opcao --aplicar habilita a importacao transacional de ContaPagar.
"""

import argparse
import os
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path


os.environ.setdefault("DJANGO_SETTINGS_MODULE", "sistema.settings")

import django


django.setup()

from django.db import transaction
from django.db.models import Sum

from estoque.models import ContaPagar, Fornecedor


BASE_DIR = Path(__file__).resolve().parent
ARQUIVO_ORIGEM = BASE_DIR / "contas_pagar_migrar_2026-09-02.txt"
TOTAL_ESPERADO = Decimal("41051.74")

CODI_PARA_FORNECEDOR = {
    "00006": 226,
    "00011": 227,
    "00013": 20,
    "00035": 62,
    "00037": 64,
    "00044": 230,
    "00071": 10,
    "00089": 38,
    "00098": 104,
    "00106": 232,
    "00114": 117,
    "00126": 126,
    "00198": 37,
    "00215": 202,
    "00216": 203,
    "00219": 206,
    "00220": 237,
    "00223": 233,
    "00235": 39,
}

NOME_SEM_CODI_PARA_FORNECEDOR = {
    "COCA COLA": 64,
    "COMERCIAL AMAZONIA": 126,
}


def decimal_obrigatorio(texto, linha, campo):
    try:
        valor = Decimal(texto)
    except (InvalidOperation, ValueError):
        raise ValueError(f"linha {linha}: {campo} invalido: {texto!r}")
    if not valor.is_finite():
        raise ValueError(f"linha {linha}: {campo} nao e finito")
    if valor < 0:
        raise ValueError(f"linha {linha}: {campo} nao pode ser negativo")
    return valor.quantize(Decimal("0.01"))


def ler_contas():
    if not ARQUIVO_ORIGEM.exists():
        raise ValueError(f"arquivo de origem nao encontrado: {ARQUIVO_ORIGEM}")

    linhas = ARQUIVO_ORIGEM.read_text(encoding="utf-8-sig").splitlines()
    contas = []
    erros = []

    for numero, linha_original in enumerate(linhas, start=1):
        linha = linha_original.strip()
        partes = [parte.strip() for parte in linha.split("|")]
        if len(partes) != 8 or not linha:
            erros.append(f"linha {numero}: esperado formato com 8 campos")
            continue

        docto, codi, nome, emissao_texto, vencimento_texto, valor_texto, valpag_texto, valres_texto = partes
        try:
            if not docto:
                raise ValueError("DOCTO vazio")
            emissao = date.fromisoformat(emissao_texto)
            vencimento = date.fromisoformat(vencimento_texto)
            valor = decimal_obrigatorio(valor_texto, numero, "VALOR")
            valpag = decimal_obrigatorio(valpag_texto, numero, "VALPAG")
            valres = decimal_obrigatorio(valres_texto, numero, "VALRES")
            if valor < valres:
                raise ValueError("VALOR menor que VALRES")

            if codi:
                fornecedor_id = CODI_PARA_FORNECEDOR.get(codi)
                if fornecedor_id is None:
                    raise ValueError(f"CODI sem mapeamento: {codi}")
            else:
                fornecedor_id = NOME_SEM_CODI_PARA_FORNECEDOR.get(nome)
                if fornecedor_id is None:
                    raise ValueError(f"CODI vazio e NOME sem mapeamento: {nome!r}")

            contas.append({
                "linha": numero,
                "documento": docto,
                "codi": codi,
                "nome": nome,
                "fornecedor_id": fornecedor_id,
                "emissao": emissao,
                "vencimento": vencimento,
                "valor": valor,
                "valpag": valpag,
                "valres": valres,
                "status": "parcial" if valpag > 0 and valres > 0 else "aberta",
            })
        except ValueError as exc:
            erros.append(str(exc))

    if erros:
        raise ValueError("\n".join(erros))
    return contas


def validar(contas):
    if len(contas) != 55:
        raise ValueError(f"quantidade de linhas validas: {len(contas)}; esperado: 55")

    documentos = [conta["documento"] for conta in contas]
    documentos_unicos = set(documentos)
    if len(documentos_unicos) != 55:
        repetidos = sorted({documento for documento in documentos if documentos.count(documento) > 1})
        raise ValueError(f"DOCTO unicos: {len(documentos_unicos)}; repetidos: {repetidos}")

    total = sum((conta["valres"] for conta in contas), Decimal("0.00")).quantize(Decimal("0.01"))
    if total != TOTAL_ESPERADO:
        raise ValueError(f"total VALRES: {total}; esperado: {TOTAL_ESPERADO}")

    fornecedor_ids = {conta["fornecedor_id"] for conta in contas}
    fornecedores = Fornecedor.objects.in_bulk(fornecedor_ids)
    faltantes = sorted(fornecedor_ids - set(fornecedores))
    if faltantes:
        raise ValueError(f"fornecedores inexistentes: {faltantes}")

    documentos_existentes = set(
        ContaPagar.objects.filter(documento_legado__in=documentos).values_list("documento_legado", flat=True)
    )
    if documentos_existentes:
        raise ValueError(f"documentos ja existentes em ContaPagar: {sorted(documentos_existentes)}")

    return fornecedores, documentos, total


def mostrar_previa(contas, fornecedores, documentos, total):
    agrupado = defaultdict(lambda: {"quantidade": 0, "total": Decimal("0.00")})
    for conta in contas:
        grupo = agrupado[conta["fornecedor_id"]]
        grupo["quantidade"] += 1
        grupo["total"] += conta["valres"]

    print(f"Arquivo: {ARQUIVO_ORIGEM.name}")
    print("Modo: DRY-RUN (nenhuma escrita sera executada)")
    print(f"Linhas validas: {len(contas)}")
    print(f"DOCTO unicos: {len(set(documentos))}")
    print(f"Total VALRES: R$ {total:.2f}")
    print("\nPrevia por fornecedor:")
    for fornecedor_id in sorted(agrupado, key=lambda item: (fornecedores[item].nome.lower(), item)):
        grupo = agrupado[fornecedor_id]
        fornecedor = fornecedores[fornecedor_id]
        print(f"- ID {fornecedor_id} | {fornecedor.nome} | {grupo['quantidade']} conta(s) | R$ {grupo['total']:.2f}")

    print("\nTotal geral:")
    print(f"- {len(contas)} conta(s) | R$ {total:.2f} em aberto")


def aplicar(contas, documentos, total):
    with transaction.atomic():
        for conta in contas:
            ContaPagar.objects.create(
                compra=None,
                documento_legado=conta["documento"],
                fornecedor_id=conta["fornecedor_id"],
                data_emissao=conta["emissao"],
                data_vencimento=conta["vencimento"],
                valor_original=conta["valor"],
                valor_em_aberto=conta["valres"],
                status=(
                    ContaPagar.STATUS_PARCIAL
                    if conta["status"] == "parcial"
                    else ContaPagar.STATUS_ABERTA
                ),
                observacao=(
                    "Importado do Firebird na virada. "
                    f"Código antigo: {conta['codi'] or '(vazio)'}. "
                    f"Fornecedor antigo: {conta['nome']}. "
                    f"Valor pago anterior: {conta['valpag']:.2f}."
                ),
            )

        documentos_criados = list(
            ContaPagar.objects
            .filter(documento_legado__in=documentos)
            .values_list("documento_legado", flat=True)
        )
        quantidade_criada = len(documentos_criados)
        total_criado = (
            ContaPagar.objects
            .filter(documento_legado__in=documentos)
            .aggregate(total=Sum("valor_em_aberto"))
            .get("total")
            or Decimal("0.00")
        ).quantize(Decimal("0.01"))
        if quantidade_criada != 55:
            raise ValueError(f"quantidade criada: {quantidade_criada}; esperado: 55")
        if len(set(documentos_criados)) != 55:
            raise ValueError(f"documentos encontrados: {len(set(documentos_criados))}; esperado: 55")
        if total_criado != total:
            raise ValueError(f"soma criada: {total_criado}; esperado: {total}")


def main():
    parser = argparse.ArgumentParser(description="Dry-run ou importacao de contas a pagar legadas.")
    parser.add_argument(
        "--aplicar",
        action="store_true",
        help="cria as ContaPagar validadas dentro de uma transacao",
    )
    args = parser.parse_args()

    contas = ler_contas()
    fornecedores, documentos, total = validar(contas)
    if not args.aplicar:
        mostrar_previa(contas, fornecedores, documentos, total)
        return

    aplicar(contas, documentos, total)
    print("IMPORTAÇÃO CONCLUÍDA")

if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as exc:
        print(f"DRY-RUN INVALIDO: {exc}")
        raise SystemExit(1)
