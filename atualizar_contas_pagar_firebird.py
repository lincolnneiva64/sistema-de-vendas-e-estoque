#!/usr/bin/env python
"""Sincronizacao incremental de contas a pagar Firebird -> Django.

O modo padrao e dry-run. A opcao --aplicar cria/atualiza somente contas
legadas autorizadas; contas ausentes no Firebird nunca sao fechadas automaticamente.
"""

import argparse
import os
import subprocess
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path


os.environ.setdefault("DJANGO_SETTINGS_MODULE", "sistema.settings")

import django


django.setup()

from django.db import transaction
from django.db.models import Count

from estoque.models import ContaPagar, Fornecedor


BASE_DIR = Path(__file__).resolve().parent
ISQL_PADRAO = r"C:\Program Files (x86)\Firebird\Firebird_2_5\bin\isql.exe"
BANCO_PADRAO = r"C:\Ariramba\Dados\BDados.fdb"
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
CONTAS_EXCLUIDAS_EXPLICITAMENTE = {
    ("00098", "27928-02/3-BO"),
    ("00098", "27928-03/3-BO"),
    ("00098", "28038-01/3-BO"),
    ("00098", "28038-02/3-BO"),
    ("00098", "28038-03/3-BO"),
}
CONTAS_EXCLUIDAS_EXPLICITAMENTE_QTD_ESPERADA = 5
CONTAS_EXCLUIDAS_EXPLICITAMENTE_TOTAL_ESPERADO = Decimal("1711.66")
QUERY = """
SET LIST OFF;
SET HEADING OFF;
SET ECHO OFF;
SET COUNT OFF;
SELECT
    TRIM(DOCTO) || '|' ||
    COALESCE(TRIM(CODI), '') || '|' ||
    COALESCE(TRIM(NOME), '') || '|' ||
    CAST(EMIS AS VARCHAR(10)) || '|' ||
    CAST(VENC AS VARCHAR(10)) || '|' ||
    CAST(VALOR AS VARCHAR(30)) || '|' ||
    CAST(VALPAG AS VARCHAR(30)) || '|' ||
    CAST(VALRES AS VARCHAR(30))
FROM CPAGAR
WHERE VALRES > 0
  AND ST = 'A'
ORDER BY NOME, VENC, DOCTO;
QUIT;
"""


def decimal_obrigatorio(texto, contexto, campo):
    texto = texto.strip().replace(",", ".")
    try:
        valor = Decimal(texto)
    except (InvalidOperation, ValueError):
        raise ValueError(f"{contexto}: {campo} invalido: {texto!r}")
    if not valor.is_finite() or valor < 0:
        raise ValueError(f"{contexto}: {campo} deve ser finito e nao negativo")
    return valor.quantize(Decimal("0.01"))


def fornecedor_id_para(conta, contexto, permitir_ausente=False):
    if conta["codi"]:
        fornecedor_id = CODI_PARA_FORNECEDOR.get(conta["codi"])
        if fornecedor_id is None:
            if permitir_ausente:
                return None
            raise ValueError(f"{contexto}: CODI sem mapeamento: {conta['codi']}")
        return fornecedor_id
    fornecedor_id = NOME_SEM_CODI_PARA_FORNECEDOR.get(conta["nome"])
    if fornecedor_id is None:
        if permitir_ausente:
            return None
        raise ValueError(f"{contexto}: CODI vazio e NOME sem mapeamento: {conta['nome']!r}")
    return fornecedor_id


def parse_linha(linha, numero, permitir_fornecedor_nao_mapeado=False):
    partes = [parte.strip() for parte in linha.split("|")]
    if len(partes) != 8:
        raise ValueError(f"linha {numero}: esperado formato com 8 campos")
    docto, codi, nome, emissao_texto, vencimento_texto, valor_texto, valpag_texto, valres_texto = partes
    if not docto:
        raise ValueError(f"linha {numero}: DOCTO vazio")
    try:
        emissao = date.fromisoformat(emissao_texto)
        vencimento = date.fromisoformat(vencimento_texto)
    except ValueError as exc:
        raise ValueError(f"linha {numero}: data invalida: {exc}")
    valor = decimal_obrigatorio(valor_texto, f"linha {numero}", "VALOR")
    valpag = decimal_obrigatorio(valpag_texto, f"linha {numero}", "VALPAG")
    valres = decimal_obrigatorio(valres_texto, f"linha {numero}", "VALRES")
    if valor < valres:
        raise ValueError(f"linha {numero}: VALOR menor que VALRES")
    conta = {
        "linha": numero,
        "documento": docto,
        "codi": codi,
        "nome": nome,
        "emissao": emissao,
        "vencimento": vencimento,
        "valor": valor,
        "valpag": valpag,
        "valres": valres,
        "status": "parcial" if valpag > 0 and valres > 0 else "aberta",
    }
    conta["fornecedor_id"] = fornecedor_id_para(
        conta,
        f"linha {numero}",
        permitir_ausente=permitir_fornecedor_nao_mapeado,
    )
    return conta


def extrair_firebird(isql_path, banco, usuario, senha):
    resultado = subprocess.run(
        [isql_path, "-q", "-user", usuario, "-password", senha, banco],
        input=QUERY,
        text=True,
        capture_output=True,
        encoding="cp1252",
        errors="replace",
        check=False,
    )
    if resultado.returncode != 0:
        raise RuntimeError(f"isql falhou ({resultado.returncode}): {resultado.stderr.strip() or resultado.stdout.strip()}")

    contas = []
    ignoradas = []
    erros = []
    for numero, linha in enumerate(resultado.stdout.splitlines(), start=1):
        linha = linha.strip()
        if not linha:
            continue
        if linha.count("|") != 7:
            continue
        try:
            conta = parse_linha(linha, numero, permitir_fornecedor_nao_mapeado=True)
            if conta_excluida_explicitamente(conta):
                ignoradas.append({
                    **conta,
                    "motivo_ignorada": "Exclusao explicita: Cafe Santa Clara removido da migracao",
                })
                continue
            if conta["fornecedor_id"] is None:
                ignoradas.append(conta)
            else:
                contas.append(conta)
        except ValueError as exc:
            erros.append(str(exc))
    if erros:
        raise ValueError("\n".join(erros))
    return contas, ignoradas


def conta_excluida_explicitamente(conta):
    """Bloqueia documentos legados removidos manualmente da migracao."""
    chave = ((conta.get("codi") or "").strip(), (conta.get("documento") or "").strip())
    return chave in CONTAS_EXCLUIDAS_EXPLICITAMENTE


def validar_exclusoes_explicitamente_configuradas(contas_ignoradas):
    contas_santa_clara = [
        conta for conta in contas_ignoradas
        if conta.get("motivo_ignorada") == "Exclusao explicita: Cafe Santa Clara removido da migracao"
    ]
    total = sum((conta["valres"] for conta in contas_santa_clara), Decimal("0.00"))
    if (
        len(CONTAS_EXCLUIDAS_EXPLICITAMENTE) != CONTAS_EXCLUIDAS_EXPLICITAMENTE_QTD_ESPERADA
        or len(contas_santa_clara) != CONTAS_EXCLUIDAS_EXPLICITAMENTE_QTD_ESPERADA
        or total != CONTAS_EXCLUIDAS_EXPLICITAMENTE_TOTAL_ESPERADO
    ):
        raise ValueError(
            "aplicacao de Contas a Pagar bloqueada: exclusao explicita do Cafe Santa Clara "
            "ainda nao confere com 5 contas / R$ 1.711,66"
        )


def carregar_django(contas):
    if not contas:
        raise ValueError("a extracao Firebird nao retornou contas abertas")
    documentos = [conta["documento"] for conta in contas]
    if len(contas) != len(set(documentos)):
        repetidos = sorted({doc for doc in documentos if documentos.count(doc) > 1})
        raise ValueError(f"DOCTO duplicado na extracao Firebird: {repetidos}")
    fornecedor_ids = {conta["fornecedor_id"] for conta in contas}
    fornecedores = Fornecedor.objects.in_bulk(fornecedor_ids)
    faltantes = sorted(fornecedor_ids - set(fornecedores))
    if faltantes:
        raise ValueError(f"fornecedores mapeados inexistentes: {faltantes}")

    existentes = {
        conta.documento_legado: conta
        for conta in ContaPagar.objects.filter(documento_legado__in=documentos).select_related("fornecedor")
    }
    duplicados_django = set(
        ContaPagar.objects.filter(documento_legado__in=documentos)
        .values("documento_legado")
        .annotate(total_id=Count("id"))
        .filter(total_id__gt=1)
        .values_list("documento_legado", flat=True)
    )
    if duplicados_django:
        raise ValueError(f"DOCTO duplicado no Django: {sorted(duplicados_django)}")
    for documento, conta in existentes.items():
        if conta.compra_id is not None:
            raise ValueError(f"{documento}: pertence a ContaPagar normal com Compra")

    por_documento = {conta["documento"]: conta for conta in contas}
    novas = []
    alteradas = []
    sem_alteracao = []
    for documento, firebird in por_documento.items():
        django_conta = existentes.get(documento)
        if django_conta is None:
            novas.append(firebird)
            continue
        diferencas = diferencas_conta(django_conta, firebird)
        if diferencas:
            alteradas.append((django_conta, firebird, diferencas))
        else:
            sem_alteracao.append((django_conta, firebird))

    sincronizados = ContaPagar.objects.filter(
        documento_legado__isnull=False,
        fornecedor_id__in=fornecedor_ids,
        compra__isnull=True,
        status__in=[ContaPagar.STATUS_ABERTA, ContaPagar.STATUS_PARCIAL],
    )
    fechadas = [conta for conta in sincronizados if conta.documento_legado not in por_documento]
    return fornecedores, novas, alteradas, sem_alteracao, fechadas


def diferencas_conta(django_conta, firebird):
    esperado_status = ContaPagar.STATUS_PARCIAL if firebird["status"] == "parcial" else ContaPagar.STATUS_ABERTA
    diferencas = {}
    campos = {
        "fornecedor": django_conta.fornecedor_id != firebird["fornecedor_id"],
        "data_emissao": django_conta.data_emissao != firebird["emissao"],
        "data_vencimento": django_conta.data_vencimento != firebird["vencimento"],
        "valor_original": django_conta.valor_original != firebird["valor"],
        "valor_em_aberto": django_conta.valor_em_aberto != firebird["valres"],
        "status": django_conta.status != esperado_status,
    }
    for campo, mudou in campos.items():
        if mudou:
            diferencas[campo] = True
    return diferencas


def dinheiro(valor):
    return f"R$ {valor.quantize(Decimal('0.01')):.2f}".replace(".", ",")


def mostrar(contas, ignoradas, fornecedores, novas, alteradas, sem_alteracao, fechadas):
    novas_total = sum((conta["valres"] for conta in novas), Decimal("0.00"))
    print("Modo: DRY-RUN (nenhuma escrita sera executada)")
    print(f"Contas novas: {len(novas)} — {dinheiro(novas_total)}")
    print(f"Contas alteradas: {len(alteradas)}")
    print(f"Possivelmente quitadas/fechadas: {len(fechadas)}")
    print(f"Sem alteracao: {len(sem_alteracao)}")
    print(f"Fora do mapeamento autorizado: {len(ignoradas)}")
    print("\nNOVAS:")
    for conta in novas:
        print(f"- {conta['documento']} | {fornecedores[conta['fornecedor_id']].nome} | Vencimento {conta['vencimento']} | VALRES {dinheiro(conta['valres'])}")
        print()
    print("\nALTERADAS:")
    for django_conta, firebird, diferencas in alteradas:
        diferenca = firebird["valres"] - django_conta.valor_em_aberto
        print(f"- {firebird['documento']} | {fornecedores[firebird['fornecedor_id']].nome} | Django {dinheiro(django_conta.valor_em_aberto)} | Firebird {dinheiro(firebird['valres'])} | Diferenca {dinheiro(diferenca)} | Campos: {', '.join(diferencas)}")
    print("\nPOSSIVELMENTE FECHADAS:")
    for conta in fechadas:
        nome = conta.fornecedor.nome if conta.fornecedor else "Fornecedor nao informado"
        print(f"- {conta.documento_legado} | {nome} | Saldo atual Django {dinheiro(conta.valor_em_aberto)}")
    if ignoradas:
        print("\nFORA DO MAPEAMENTO AUTORIZADO (nao sincronizadas):")
        for conta in ignoradas:
            print(f"- {conta['documento']} | CODI {conta['codi'] or '(vazio)'} | {conta['nome']}")


def aplicar(contas, novas, alteradas, documentos):
    with transaction.atomic():
        for conta in novas:
            ContaPagar.objects.create(
                compra=None,
                documento_legado=conta["documento"],
                fornecedor_id=conta["fornecedor_id"],
                data_emissao=conta["emissao"],
                data_vencimento=conta["vencimento"],
                valor_original=conta["valor"],
                valor_em_aberto=conta["valres"],
                status=ContaPagar.STATUS_PARCIAL if conta["status"] == "parcial" else ContaPagar.STATUS_ABERTA,
                observacao=(
                    "Importado do Firebird na virada. "
                    f"Codigo antigo: {conta['codi'] or '(vazio)'}. "
                    f"Fornecedor antigo: {conta['nome']}. "
                    f"Valor pago anterior: {conta['valpag']:.2f}."
                ),
            )
        for django_conta, firebird, _ in alteradas:
            django_conta.fornecedor_id = firebird["fornecedor_id"]
            django_conta.data_emissao = firebird["emissao"]
            django_conta.data_vencimento = firebird["vencimento"]
            django_conta.valor_original = firebird["valor"]
            django_conta.valor_em_aberto = firebird["valres"]
            django_conta.status = ContaPagar.STATUS_PARCIAL if firebird["status"] == "parcial" else ContaPagar.STATUS_ABERTA
            django_conta.save(update_fields=["fornecedor", "data_emissao", "data_vencimento", "valor_original", "valor_em_aberto", "status", "atualizado_em"])

        reconciliadas = list(ContaPagar.objects.filter(documento_legado__in=documentos, compra__isnull=True))
        if len(reconciliadas) != len(documentos) or len({conta.documento_legado for conta in reconciliadas}) != len(documentos):
            raise ValueError("reconciliacao falhou: quantidade ou documentos divergentes")
        contas_por_documento = {item["documento"]: item for item in contas}
        for conta in reconciliadas:
            firebird = contas_por_documento[conta.documento_legado]
            if diferencas_conta(conta, firebird):
                raise ValueError(f"reconciliacao falhou para {conta.documento_legado}")


def main():
    parser = argparse.ArgumentParser(description="Sincroniza contas abertas do Firebird com ContaPagar legada.")
    parser.add_argument("--aplicar", action="store_true", help="cria/atualiza contas; ausentes nao sao fechadas")
    parser.add_argument("--isql", default=os.getenv("FIREBIRD_ISQL", ISQL_PADRAO))
    parser.add_argument("--banco", default=os.getenv("FIREBIRD_DB", BANCO_PADRAO))
    parser.add_argument("--usuario", default=os.getenv("FIREBIRD_USER", "SYSDBA"))
    parser.add_argument("--senha", default=os.getenv("FIREBIRD_PASSWORD", "masterkey"))
    args = parser.parse_args()

    contas, ignoradas = extrair_firebird(args.isql, args.banco, args.usuario, args.senha)
    fornecedores, novas, alteradas, sem_alteracao, fechadas = carregar_django(contas)
    mostrar(contas, ignoradas, fornecedores, novas, alteradas, sem_alteracao, fechadas)
    if args.aplicar:
        aplicar(contas, novas, alteradas, [conta["documento"] for conta in contas])
        print("SINCRONIZACAO CONCLUIDA")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"SINCRONIZACAO INVALIDA: {exc}")
        raise SystemExit(1)
