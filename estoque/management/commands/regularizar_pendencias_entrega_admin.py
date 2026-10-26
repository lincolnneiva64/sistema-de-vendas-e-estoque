import json
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError

from estoque.services.regularizacao_pendencias_entrega import regularizar


class Command(BaseCommand):
    help = "Simula regularização de um manifesto explícito. Só grava com --executar e --confirmar-hash."
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("manifesto")
        parser.add_argument("--usuario", type=int, required=True)
        parser.add_argument("--justificativa", required=True)
        parser.add_argument("--executar", action="store_true")
        parser.add_argument("--confirmar-hash")

    def handle(self, *args, **options):
        try:
            manifesto = json.loads(Path(options["manifesto"]).read_text(encoding="utf-8-sig"))
            usuario = get_user_model().objects.get(pk=options["usuario"])
            resultado = regularizar(manifesto, usuario=usuario, justificativa=options["justificativa"],
                executar=options["executar"], confirmacao=options["confirmar_hash"])
            self.stdout.write(json.dumps(resultado, ensure_ascii=False, indent=2))
        except (OSError, ValueError, get_user_model().DoesNotExist, ValidationError, PermissionDenied) as exc:
            raise CommandError(str(exc)) from exc
