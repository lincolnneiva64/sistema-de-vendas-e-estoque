import json
import subprocess
from decimal import Decimal
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from estoque.services.precos_antigo_snapshot import SNAPSHOT_SOURCE, SNAPSHOT_VERSION, caminho_snapshot_padrao


FIREBIRD_DATABASE_PADRAO = r"C:\Ariramba\Dados\BDados.fdb"
ISQL_PADRAO = r"C:\Program Files (x86)\Firebird\Firebird_2_5\bin\isql.exe"
SQL_PRODUTOS_PRECOS = """
SELECT
    CODI,
    NOME,
    ST,
    UNV,
    UNF,
    CONVF,
    CUSTOV,
    PRCOM,
    PRPRA,
    PRATA,
    CUSTOF,
    PRCOMF,
    PRPRAF,
    PRATAF
FROM PRODUTOS
WHERE ST = 'A'
ORDER BY CODI;
"""
FIREBIRD_FIELDS = [
    "CODI",
    "NOME",
    "ST",
    "UNV",
    "UNF",
    "CONVF",
    "CUSTOV",
    "PRCOM",
    "PRPRA",
    "PRATA",
    "CUSTOF",
    "PRCOMF",
    "PRPRAF",
    "PRATAF",
]


def _validar_sql_somente_leitura(sql):
    texto = " ".join(sql.upper().split())
    if not texto.startswith("SELECT "):
        raise CommandError("A extracao de precos aceita apenas SELECT.")
    comandos_proibidos = (
        " INSERT ",
        " UPDATE ",
        " DELETE ",
        " DROP ",
        " ALTER ",
        " CREATE ",
        " EXECUTE ",
        " MERGE ",
    )
    if any(comando in f" {texto} " for comando in comandos_proibidos):
        raise CommandError("SQL contem comando de escrita ou DDL.")


def _decimal_texto(valor):
    if valor in (None, ""):
        return None
    return str(Decimal(str(valor).strip()))


def _linha_snapshot(linha):
    return {
        "codigo": (linha.get("CODI") or "").strip(),
        "nome": (linha.get("NOME") or "").strip(),
        "st": (linha.get("ST") or "").strip(),
        "unv": (linha.get("UNV") or "").strip(),
        "unf": (linha.get("UNF") or "").strip(),
        "convf": _decimal_texto(linha.get("CONVF")),
        "principal": {
            "compra": _decimal_texto(linha.get("CUSTOV")),
            "vista": _decimal_texto(linha.get("PRCOM")),
            "prazo": _decimal_texto(linha.get("PRPRA")),
            "terceiro": _decimal_texto(linha.get("PRATA")),
        },
        "fracionado": {
            "compra": _decimal_texto(linha.get("CUSTOF")),
            "vista": _decimal_texto(linha.get("PRCOMF")),
            "prazo": _decimal_texto(linha.get("PRPRAF")),
            "terceiro": _decimal_texto(linha.get("PRATAF")),
        },
    }


def _extrair_linhas_list_mode(texto):
    registros = []
    atual = {}
    campos = set(FIREBIRD_FIELDS)

    for linha in texto.splitlines():
        linha = linha.rstrip()
        if not linha or linha.startswith("SQL>") or linha.startswith("CON>"):
            continue

        partes = linha.split(None, 1)
        if not partes:
            continue
        campo = partes[0].strip().upper()
        if campo not in campos:
            continue

        if campo == "CODI" and atual:
            registros.append(atual)
            atual = {}

        atual[campo] = partes[1].strip() if len(partes) > 1 else ""

    if atual:
        registros.append(atual)
    return registros


class Command(BaseCommand):
    help = "Extrai, somente leitura, um snapshot JSON de precos de produtos do Firebird antigo."

    def add_arguments(self, parser):
        parser.add_argument("--database", default=FIREBIRD_DATABASE_PADRAO)
        parser.add_argument("--isql", default=ISQL_PADRAO)
        parser.add_argument("--output", default=str(caminho_snapshot_padrao()))
        parser.add_argument("--user", default="SYSDBA")
        parser.add_argument("--password", default="masterkey")

    def handle(self, *args, **options):
        _validar_sql_somente_leitura(SQL_PRODUTOS_PRECOS)

        database = Path(options["database"])
        isql = Path(options["isql"])
        output = Path(options["output"])

        if not database.exists():
            raise CommandError(f"Banco Firebird nao encontrado: {database}")
        if not isql.exists():
            raise CommandError(f"isql nao encontrado: {isql}")

        sql = "SET HEADING OFF;\nSET LIST ON;\nSET COUNT OFF;\n" + SQL_PRODUTOS_PRECOS + "\nQUIT;\n"
        processo = subprocess.run(
            [
                str(isql),
                "-user",
                options["user"],
                "-password",
                options["password"],
                str(database),
            ],
            input=sql,
            text=True,
            capture_output=True,
            encoding="cp1252",
            errors="replace",
            check=False,
        )
        if processo.returncode != 0:
            raise CommandError(processo.stderr.strip() or "Falha ao consultar Firebird.")

        linhas = [_linha_snapshot(linha) for linha in _extrair_linhas_list_mode(processo.stdout)]

        linhas.sort(key=lambda item: item["codigo"])
        dados = {
            "versao": SNAPSHOT_VERSION,
            "fonte_logica": SNAPSHOT_SOURCE,
            "tabela": "PRODUTOS",
            "filtro": "ST = 'A'",
            "campos": FIREBIRD_FIELDS,
            "gerado_em": timezone.now().replace(microsecond=0).isoformat(),
            "quantidade_registros": len(linhas),
            "produtos": linhas,
        }

        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(f"Snapshot gerado: {output} ({len(linhas)} produtos)"))
