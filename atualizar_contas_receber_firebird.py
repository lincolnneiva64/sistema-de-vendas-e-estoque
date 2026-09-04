#!/usr/bin/env python
"""Sincronizacao incremental de Contas a Receber Firebird -> Django.

O modo padrao e dry-run. --aplicar cria/atualiza somente contas legadas
pertencentes ao universo de clientes ja importado; ausentes nao sao quitadas.
"""

import argparse
import os
import subprocess
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "sistema.settings")

import django

django.setup()

from django.db import transaction

from estoque.models import Cliente, ContaReceber

BASE_DIR = Path(__file__).resolve().parent
ISQL_PADRAO = r"C:\Program Files (x86)\Firebird\Firebird_2_5\bin\isql.exe"
BANCO_PADRAO = r"C:\Ariramba\Dados\BDados.fdb"
QUERY = """
SET LIST OFF;
SET HEADING OFF;
SET ECHO OFF;
SET COUNT OFF;
SELECT
    TRIM(R.NUME) || '|' ||
    COALESCE(TRIM(R.CODI), '') || '|' ||
    COALESCE(TRIM(R.NOME), '') || '|' ||
    CAST(R.DATA AS VARCHAR(10)) || '|' ||
    CAST(R.VENC AS VARCHAR(10)) || '|' ||
    CAST(R.VALOR AS VARCHAR(30)) || '|' ||
    CAST(R.VPAGO AS VARCHAR(30)) || '|' ||
    CAST(R.VREST AS VARCHAR(30))
FROM CRCLIENTES R
JOIN CLIENTES C ON C.CODI = R.CODI
WHERE R.VREST > 0
  AND R.SN = 'S'
  AND C.ST = 'A'
ORDER BY R.NOME, R.VENC, R.NUME;
QUIT;
"""


def _sql_literal(valor):
    return "'" + str(valor).replace("'", "''") + "'"


def _query_quitadas(candidatas):
    pares = [
        f"(NUME = {_sql_literal(conta.numero_legado)} AND CODI = {_sql_literal(conta.cliente.codigo_legado.strip())})"
        for conta in candidatas
        if conta.cliente and conta.cliente.codigo_legado
    ]
    if not pares:
        return ""
    return """
SET LIST OFF;
SET HEADING OFF;
SET ECHO OFF;
SET COUNT OFF;
SELECT TRIM(NUME) || '|' || TRIM(CODI) || '|' || CAST(VALOR AS VARCHAR(30)) || '|' ||
       CAST(VPAGO AS VARCHAR(30)) || '|' || CAST(VREST AS VARCHAR(30))
FROM CRCLIENTES
WHERE VREST = 0
  AND ({})
;
QUIT;
""".format(" OR ".join(pares))


def decimal_obrigatorio(texto, contexto, campo):
    try:
        valor = Decimal(texto.strip().replace(",", "."))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{contexto}: {campo} invalido: {texto!r}")
    if not valor.is_finite() or valor < 0:
        raise ValueError(f"{contexto}: {campo} deve ser finito e nao negativo")
    return valor.quantize(Decimal("0.01"))


def parse_linha(linha, numero, cliente_ids):
    partes = [parte.strip() for parte in linha.split("|")]
    if len(partes) != 8:
        raise ValueError(f"linha {numero}: esperado formato com 8 campos")
    numero_legado, codigo, nome, emissao_texto, vencimento_texto, valor_texto, pago_texto, saldo_texto = partes
    if not numero_legado:
        raise ValueError(f"linha {numero}: NUME vazio")
    try:
        emissao = date.fromisoformat(emissao_texto)
        vencimento = date.fromisoformat(vencimento_texto)
    except ValueError as exc:
        raise ValueError(f"linha {numero}: data invalida: {exc}")
    valor = decimal_obrigatorio(valor_texto, f"linha {numero}", "VALOR")
    pago = decimal_obrigatorio(pago_texto, f"linha {numero}", "VPAGO")
    saldo = decimal_obrigatorio(saldo_texto, f"linha {numero}", "VREST")
    if valor < saldo:
        raise ValueError(f"linha {numero}: VALOR menor que VREST")
    cliente_id = cliente_ids.get(codigo)
    conta = {
        "linha": numero,
        "numero_legado": numero_legado,
        "codigo": codigo,
        "nome": nome,
        "cliente_id": cliente_id,
        "emissao": emissao,
        "vencimento": vencimento,
        "valor": valor,
        "pago": pago,
        "saldo": saldo,
        "status": "parcial" if pago > 0 and saldo > 0 else "aberta",
    }
    return conta


def extrair_firebird(isql_path, banco, usuario, senha, cliente_ids):
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
    fora = []
    erros = []
    for numero, linha in enumerate(resultado.stdout.splitlines(), start=1):
        linha = linha.strip()
        if not linha or linha.count("|") != 7:
            continue
        try:
            conta = parse_linha(linha, numero, cliente_ids)
            (contas if conta["cliente_id"] is not None else fora).append(conta)
        except ValueError as exc:
            erros.append(str(exc))
    if erros:
        raise ValueError("\n".join(erros))
    if not contas:
        raise ValueError("a extracao Firebird nao retornou contas do universo autorizado")
    return contas, fora


def confirmar_quitadas(isql_path, banco, usuario, senha, candidatas):
    query = _query_quitadas(candidatas)
    if not query:
        return []
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
        raise RuntimeError(f"isql falhou ({resultado.returncode}): {resultado.stderr.strip() or resultado.stdout.strip()}")
    confirmadas = []
    por_chave = {
        (conta.numero_legado, conta.cliente.codigo_legado.strip()): conta
        for conta in candidatas
        if conta.cliente and conta.cliente.codigo_legado
    }
    for numero, linha in enumerate(resultado.stdout.splitlines(), start=1):
        linha = linha.strip()
        if not linha or "|" not in linha:
            continue
        partes = [parte.strip() for parte in linha.split("|")]
        if len(partes) != 5:
            raise ValueError(f"linha {numero}: esperado formato com 5 campos na confirmacao de quitadas")
        numero_legado, codigo, valor, pago, saldo = partes
        conta = por_chave.get((numero_legado, codigo))
        if conta is None:
            raise ValueError(f"linha {numero}: conta quitada nao corresponde ao cliente esperado")
        if Decimal(saldo.replace(",", ".")).quantize(Decimal("0.01")) != Decimal("0.00"):
            raise ValueError(f"linha {numero}: conta confirmada com VREST diferente de zero")
        confirmadas.append((conta, {"numero_legado": numero_legado, "codigo": codigo, "valor": decimal_obrigatorio(valor, f"linha {numero}", "VALOR"), "pago": decimal_obrigatorio(pago, f"linha {numero}", "VPAGO"), "saldo": Decimal("0.00")}))
    return confirmadas


def carregar_universo():
    clientes = list(
        Cliente.objects
        .exclude(codigo_legado__isnull=True)
        .exclude(codigo_legado="")
    )
    cliente_ids = {}
    for cliente in clientes:
        codigo = cliente.codigo_legado.strip()
        if not codigo or codigo in cliente_ids:
            raise ValueError("universo de clientes legados possui codigo_legado ausente ou duplicado")
        cliente_ids[codigo] = cliente.id
    return {cliente.id: cliente for cliente in clientes}, cliente_ids


def diferencas_conta(django_conta, firebird):
    status = ContaReceber.STATUS_PARCIAL if firebird["status"] == "parcial" else ContaReceber.STATUS_ABERTA
    return {
        campo: mudou for campo, mudou in {
            "cliente": django_conta.cliente_id != firebird["cliente_id"],
            "data_emissao": django_conta.data_emissao != firebird["emissao"],
            "data_vencimento": django_conta.data_vencimento != firebird["vencimento"],
            "valor_original": django_conta.valor_original != firebird["valor"],
            "valor_em_aberto": django_conta.valor_em_aberto != firebird["saldo"],
            "status": django_conta.status != status,
        }.items() if mudou
    }


def dinheiro(valor):
    return f"R$ {valor.quantize(Decimal('0.01')):.2f}".replace(".", ",")


def comparar(contas, isql_path=ISQL_PADRAO, banco=BANCO_PADRAO, usuario="SYSDBA", senha="masterkey"):
    documentos = [conta["numero_legado"] for conta in contas]
    if len(documentos) != len(set(documentos)):
        raise ValueError("numero_legado duplicado na extracao Firebird")
    existentes = {
        conta.numero_legado: conta
        for conta in ContaReceber.objects.filter(numero_legado__in=documentos).select_related("cliente")
    }
    por_documento = {conta["numero_legado"]: conta for conta in contas}
    novas, alteradas, sem_alteracao = [], [], []
    for documento, firebird in por_documento.items():
        django_conta = existentes.get(documento)
        if django_conta is None:
            novas.append(firebird)
        else:
            if django_conta.venda_id is not None:
                raise ValueError(f"{documento}: pertence a ContaReceber normal com Venda")
            diferencas = diferencas_conta(django_conta, firebird)
            (alteradas if diferencas else sem_alteracao).append((django_conta, firebird, diferencas))

    cliente_ids = {conta["cliente_id"] for conta in contas}
    candidatas = list(ContaReceber.objects.filter(
        numero_legado__isnull=False,
        cliente_id__in=cliente_ids,
        venda__isnull=True,
        status__in=[ContaReceber.STATUS_ABERTA, ContaReceber.STATUS_PARCIAL],
    ).exclude(numero_legado__in=documentos).select_related("cliente"))
    quitadas = confirmar_quitadas(isql_path, banco, usuario, senha, candidatas) if candidatas else []
    quitadas_ids = {conta.pk for conta, _ in quitadas}
    alertas = [conta for conta in candidatas if conta.pk not in quitadas_ids]
    return novas, alteradas, sem_alteracao, quitadas, alertas


def mostrar(contas, fora, clientes, novas, alteradas, sem_alteracao, quitadas, alertas):
    total_novas = sum((conta["saldo"] for conta in novas), Decimal("0.00"))
    print("Modo: DRY-RUN (nenhuma escrita sera executada)")
    print(f"Contas abertas Firebird encontradas: {len(contas) + len(fora)}")
    print(f"Contas abertas Firebird no universo autorizado: {len(contas)}")
    print(f"Contas novas: {len(novas)} — {dinheiro(total_novas)}")
    print(f"Contas alteradas: {len(alteradas)}")
    print(f"Quitadas confirmadas no Firebird: {len(quitadas)}")
    print(f"Alertas nao confirmados: {len(alertas)}")
    print(f"Sem alteracao: {len(sem_alteracao)}")
    print(f"Registros/clientes fora do universo autorizado: {len(fora)}")
    print("\nNOVAS:")
    for conta in novas:
        print(f"- {conta['numero_legado']} | {clientes[conta['cliente_id']].nome} | Vencimento {conta['vencimento']} | Saldo {dinheiro(conta['saldo'])}")
    print("\nALTERADAS:")
    for django_conta, firebird, diferencas in alteradas:
        diferenca = firebird["saldo"] - django_conta.valor_em_aberto
        print(f"- {firebird['numero_legado']} | {clientes[firebird['cliente_id']].nome} | Django {dinheiro(django_conta.valor_em_aberto)} | Firebird {dinheiro(firebird['saldo'])} | Diferenca {dinheiro(diferenca)} | Campos: {', '.join(diferencas)}")
    print("\nQUITADAS CONFIRMADAS NO FIREBIRD:")
    for conta, firebird in quitadas:
        print(f"- {conta.numero_legado} | {conta.cliente.nome} | Valor original preservado {dinheiro(conta.valor_original)}")
    print("\nALERTAS: NAO APARECEM ENTRE CONTAS ABERTAS DO FIREBIRD:")
    for conta in alertas:
        nome = conta.cliente.nome if conta.cliente else "Cliente nao informado"
        print(
            f"- {conta.numero_legado} | {nome} | Saldo atual Django {dinheiro(conta.valor_em_aberto)} "
            "| Quitacao nao confirmada; nao sera fechada"
        )
    if fora:
        print("\nFORA DO UNIVERSO AUTORIZADO (nao sincronizados):")
        for conta in fora:
            print(f"- {conta['numero_legado']} | CODI {conta['codigo'] or '(vazio)'} | {conta['nome']}")


def aplicar(contas, novas, alteradas, documentos, quitadas=None):
    quitadas = quitadas or []
    with transaction.atomic():
        for conta in novas:
            ContaReceber.objects.create(
                venda=None,
                numero_legado=conta["numero_legado"],
                cliente_id=conta["cliente_id"],
                data_emissao=conta["emissao"],
                data_vencimento=conta["vencimento"],
                valor_original=conta["valor"],
                valor_em_aberto=conta["saldo"],
                status=ContaReceber.STATUS_PARCIAL if conta["status"] == "parcial" else ContaReceber.STATUS_ABERTA,
                observacao=(
                    "Sincronizado do Firebird. "
                    f"Cliente antigo: {conta['nome']}. "
                    f"Valor pago anterior: {conta['pago']:.2f}."
                ),
            )
        for django_conta, firebird, _ in alteradas:
            django_conta.cliente_id = firebird["cliente_id"]
            django_conta.data_emissao = firebird["emissao"]
            django_conta.data_vencimento = firebird["vencimento"]
            django_conta.valor_original = firebird["valor"]
            django_conta.valor_em_aberto = firebird["saldo"]
            django_conta.status = ContaReceber.STATUS_PARCIAL if firebird["status"] == "parcial" else ContaReceber.STATUS_ABERTA
            django_conta.save(update_fields=["cliente", "data_emissao", "data_vencimento", "valor_original", "valor_em_aberto", "status", "atualizado_em"])

        for django_conta, _ in quitadas:
            django_conta.valor_em_aberto = Decimal("0.00")
            django_conta.status = ContaReceber.STATUS_PAGA
            django_conta.save(update_fields=["valor_em_aberto", "status", "atualizado_em"])

        reconciliadas = list(ContaReceber.objects.filter(numero_legado__in=documentos, venda__isnull=True))
        esperadas = {conta["numero_legado"]: conta for conta in contas}
        if len(reconciliadas) != len(documentos) or len({conta.numero_legado for conta in reconciliadas}) != len(documentos):
            raise ValueError("reconciliacao falhou: quantidade ou documentos divergentes")
        for conta in reconciliadas:
            if diferencas_conta(conta, esperadas[conta.numero_legado]):
                raise ValueError(f"reconciliacao falhou para {conta.numero_legado}")


def main():
    parser = argparse.ArgumentParser(description="Sincroniza contas abertas do Firebird com ContaReceber legada.")
    parser.add_argument("--aplicar", action="store_true", help="cria/atualiza contas; ausentes nao sao fechadas")
    parser.add_argument("--isql", default=os.getenv("FIREBIRD_ISQL", ISQL_PADRAO))
    parser.add_argument("--banco", default=os.getenv("FIREBIRD_DB", BANCO_PADRAO))
    parser.add_argument("--usuario", default=os.getenv("FIREBIRD_USER", "SYSDBA"))
    parser.add_argument("--senha", default=os.getenv("FIREBIRD_PASSWORD", "masterkey"))
    args = parser.parse_args()
    clientes, cliente_ids = carregar_universo()
    contas, fora = extrair_firebird(args.isql, args.banco, args.usuario, args.senha, cliente_ids)
    novas, alteradas, sem_alteracao, quitadas, alertas = comparar(contas)
    mostrar(contas, fora, clientes, novas, alteradas, sem_alteracao, quitadas, alertas)
    if args.aplicar:
        aplicar(contas, novas, alteradas, [conta["numero_legado"] for conta in contas], quitadas)
        print("SINCRONIZACAO CONCLUIDA")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"SINCRONIZACAO INVALIDA: {exc}")
        raise SystemExit(1)
