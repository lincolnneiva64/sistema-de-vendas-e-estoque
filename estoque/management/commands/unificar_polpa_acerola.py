from django.core.management.base import BaseCommand, CommandError

from estoque.services.unificar_polpa_acerola import (
    UnificacaoPolpaAcerolaErro,
    unificar_polpa_acerola,
)


class Command(BaseCommand):
    help = "Unifica com seguranca o cadastro duplicado da Polpa Acerola 1Kg."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Aplica a unificacao. Sem esta flag, executa somente dry-run.",
        )

    def handle(self, *args, **options):
        aplicar = options["apply"]

        try:
            resultado = unificar_polpa_acerola(aplicar=aplicar)
        except UnificacaoPolpaAcerolaErro as exc:
            raise CommandError(str(exc)) from exc

        modo = "APLICADO" if resultado.aplicado else "DRY-RUN"
        self.stdout.write(self.style.SUCCESS(f"{modo}: unificacao Polpa Acerola validada."))
        self.stdout.write(f"Produto operacional: {resultado.produto_operacional_id}")
        self.stdout.write(f"Produto duplicado/importado: {resultado.produto_duplicado_id}")
        self.stdout.write(f"Fornecedores atuais 277: {resultado.fornecedor_ids_operacional}")
        self.stdout.write(f"Fornecedores atuais 1028: {resultado.fornecedor_ids_duplicado}")
        self.stdout.write(f"Fornecedores finais 277: {resultado.fornecedor_ids_finais}")
        self.stdout.write(f"Historico transacional 1028: {resultado.relacoes_duplicado}")

        if not aplicar:
            self.stdout.write("Nenhuma alteracao foi gravada. Use --apply somente apos revisao.")
