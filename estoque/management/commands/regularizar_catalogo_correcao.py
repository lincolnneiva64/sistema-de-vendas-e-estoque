import json
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from estoque.services.catalogo_correcao_compra import regularizar


class Command(BaseCommand):
    help = 'Prévia ou regularização auditada de catálogo de uma compra corrigida. Não refaz estoque ou financeiro.'

    def add_arguments(self, parser):
        parser.add_argument('--compra', type=int, required=True)
        parser.add_argument('--usuario', type=int, required=True)
        parser.add_argument('--aplicar', action='store_true')

    def handle(self, *args, **options):
        try:
            usuario = get_user_model().objects.get(pk=options['usuario'], is_active=True)
            resultado = regularizar(options['compra'],usuario,executar=options['aplicar'])
        except (ValueError, get_user_model().DoesNotExist) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(resultado,ensure_ascii=False,indent=2))
