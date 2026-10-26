import json

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from estoque.services.regularizacao_pendencias_entrega import consultar


class Command(BaseCommand):
    help = "Exporta manifesto somente de leitura das ocorrências abertas, sem regularizar."
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("--identificador", action="append", help="Chave explícita obtida na consulta; pode repetir.")

    def handle(self, *args, **options):
        sqlite = connection.vendor == "sqlite"
        anterior = None
        try:
            if sqlite:
                with connection.cursor() as cursor:
                    cursor.execute("PRAGMA query_only")
                    anterior = cursor.fetchone()[0]
                    cursor.execute("PRAGMA query_only = ON")
            with transaction.atomic():
                if connection.vendor == "postgresql":
                    with connection.cursor() as cursor:
                        cursor.execute("SET TRANSACTION READ ONLY")
                elif not sqlite:
                    raise CommandError("Consulta protegida disponível apenas em PostgreSQL ou SQLite.")
                manifesto = consultar(options["identificador"])
            self.stdout.write(json.dumps(manifesto, ensure_ascii=False, indent=2))
        except ValidationError as exc:
            raise CommandError("; ".join(exc.messages)) from exc
        finally:
            if sqlite and anterior is not None:
                with connection.cursor() as cursor:
                    cursor.execute(f"PRAGMA query_only = {int(anterior)}")
