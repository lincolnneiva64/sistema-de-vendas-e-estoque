"""Explicit, interactive account setup. Never run from build/deploy/startup."""
import getpass
import sys
import warnings

from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction


class Command(BaseCommand):
    help = 'Verifica uma conta; --ativar prepara administrador individual com senha oculta.'

    def add_arguments(self, parser):
        parser.add_argument('--username', required=True)
        parser.add_argument('--ativar', action='store_true')

    def handle(self, *args, **options):
        if connection.vendor != 'postgresql':
            raise CommandError('Use o PostgreSQL explicitamente configurado; não prepare contas em um SQLite de fallback.')
        User = get_user_model()
        username = options['username'].strip()
        if not username:
            raise CommandError('Informe o nome individual da conta.')
        matches = User.objects.filter(username__iexact=username)
        if matches.count() > 1 or (matches.exists() and not matches.filter(username=username).exists()):
            raise CommandError('Nome ambíguo ou capitalização diferente. Confira a conta exata antes de continuar.')
        user = matches.first()
        if not options['ativar']:
            if not user or not (user.is_active and user.is_staff and user.is_superuser and user.has_usable_password()):
                raise CommandError('Conta ainda não preparada: confira existência, ativação, privilégios e senha utilizável.')
            self.stdout.write('Conta preparada: ativa, administrativa e com senha utilizável. Falta o titular validar o login no navegador.')
            return
        if not sys.stdin.isatty():
            raise CommandError('Use um terminal interativo com entrada oculta. Nenhuma alteração realizada.')
        confirmation = input('Confirme digitando o nome exato da conta que receberá acesso total: ')
        if confirmation != username:
            raise CommandError('Confirmação divergente. Nenhuma alteração realizada.')
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            try:
                password = getpass.getpass('Nova senha individual (entrada oculta): ')
                repeat = getpass.getpass('Repita a senha: ')
            except (getpass.GetPassWarning, EOFError):
                raise CommandError('Não foi possível garantir entrada oculta. Nenhuma alteração realizada.')
        if not password or password != repeat:
            raise CommandError('As senhas não conferem. Nenhuma alteração realizada.')
        candidate = user or User(username=username)
        try:
            candidate.full_clean(exclude=['password'])
            validate_password(password, candidate)
        except ValidationError:
            raise CommandError('Nome ou senha rejeitados pelos validadores. Use nome válido e senha longa, exclusiva e não previsível.')
        with transaction.atomic():
            current = User.objects.select_for_update().filter(username=username).first()
            if (current.pk if current else None) != (user.pk if user else None):
                raise CommandError('A conta mudou durante a preparação. Operação cancelada.')
            current = current or User(username=username)
            current.is_active = current.is_staff = current.is_superuser = True
            current.set_password(password)
            current.save()
            verified = authenticate(username=current.get_username(), password=password)
            if not verified or verified.pk != current.pk:
                raise CommandError('Autenticação não confirmada. A preparação foi revertida.')
        self.stdout.write('Conta individual preparada e autenticação validada. Nenhuma credencial foi exibida.')
