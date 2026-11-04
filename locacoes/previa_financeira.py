"""Interactive loopback preview with a temporary SQLite database, no .env/migrations.

Run from the repository: venv/Scripts/python.exe -m locacoes.previa_financeira
"""
import argparse
from datetime import time, timedelta
from decimal import Decimal
import os
from pathlib import Path
import secrets
from tempfile import TemporaryDirectory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--duration', type=int, default=0, help='Encerrar automaticamente após N segundos (validação local).')
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('Escolha uma porta local entre 1024 e 65535.')
    with TemporaryDirectory(prefix='locacoes-previa-financeira-') as directory:
        # Standalone test settings do not import operational settings or .env.
        from sistema import settings_test_compras as preview
        preview.DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3',
            'NAME': str(Path(directory) / 'previa.sqlite3')}}
        preview.INSTALLED_APPS = [*preview.INSTALLED_APPS, 'inventario']
        preview.MIDDLEWARE = [*preview.MIDDLEWARE, 'sistema.acesso.AcessoComercialMiddleware']
        preview.ROOT_URLCONF = __name__
        preview.TIME_ZONE = 'America/Belem'
        preview.SECRET_KEY = secrets.token_urlsafe(48)
        preview.SESSION_COOKIE_NAME = 'locacoes_previa_session'
        preview.CSRF_COOKIE_NAME = 'locacoes_previa_csrf'
        preview.LOGIN_URL = '/entrar/'
        preview.LOGIN_REDIRECT_URL = '/locacoes/'
        os.environ['DJANGO_SETTINGS_MODULE'] = 'sistema.settings_test_compras'
        import django
        django.setup()
        from django.apps import apps
        from django.contrib.auth import get_user_model
        from django.contrib.auth.views import LoginView
        from django.contrib.auth.management import create_permissions
        from django.contrib.staticfiles.handlers import StaticFilesHandler
        from django.core.handlers.wsgi import WSGIHandler
        from wsgiref.simple_server import make_server, WSGIServer
        import socket
        from django.db import connection, connections
        from django.urls import include, path
        from django.utils import timezone
        from .models import ConfiguracaoLocacao, FaixaPrecoLocacao, Locacao
        from estoque.models import Funcionario

        if connection.vendor != 'sqlite' or Path(connection.settings_dict['NAME']).parent != Path(directory):
            raise RuntimeError('Prévia recusada: banco não pertence ao diretório temporário.')
        try:
            with connection.schema_editor() as editor:
                for model in apps.get_models():
                    if model._meta.managed and not model._meta.proxy:
                        editor.create_model(model)
            for app in apps.get_app_configs():
                create_permissions(app, verbosity=0)
            password = secrets.token_urlsafe(9)
            get_user_model().objects.create_superuser('previa', password=password)
            ConfiguracaoLocacao.obter()
            faixa = FaixaPrecoLocacao.objects.create(codigo='centro_perto', nome='Faixa fictícia', preco_jogo_diaria=10)
            Funcionario.objects.create(nome='Operador fictício', ativo=True, pode_operar_sistema=True, pode_operar_caixa=True)
            today = timezone.localdate()
            for name, status, total, paid in [
                ('Reserva com sinal — fictícia', 'reservada', 350, 100),
                ('Entrega em andamento — fictícia', 'entregue', 280, 80),
                ('Recolhimento parcial — fictícia', 'pendente_devolucao', 400, 100),
                ('Recolhida com dívida — fictícia', 'devolvida', 500, 200),
                ('Recolhida com avaria — fictícia', 'devolvida_com_avaria', 150, 0),
                ('Quitada — fictícia', 'entregue', 120, 120),
                ('Cancelada — fictícia', 'cancelada', 90, 0),
            ]:
                rental = Locacao.objects.create(tipo_pessoa='avulsa', pessoa_avulsa_nome=name, status=status,
                    faixa_preco=faixa, data_entrega=today, data_evento=today,
                    horario_entrega=time(9), horario_evento=time(18),
                    data_prevista_devolucao=today + timedelta(days=1), total=total, saldo_devedor=total)
                if paid:
                    rental.registrar_pagamento(Decimal(paid), 'pix', responsavel='Operador fictício')
                if status == 'devolvida':
                    Locacao.objects.filter(pk=rental.pk).update(criado_em=timezone.now() - timedelta(days=90))
            globals()['urlpatterns'] = [
                path('entrar/', LoginView.as_view(template_name='locacoes/previa_login.html'), name='sistema_login'),
                path('', include('sistema.urls')),
            ]
            class PreviewServer(WSGIServer):
                allow_reuse_address = False
                def server_bind(self):
                    if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                    super().server_bind()
            with make_server('127.0.0.1', args.port, StaticFilesHandler(WSGIHandler()), server_class=PreviewServer) as server:
                print(f'\nPRÉVIA FICTÍCIA — banco temporário: {directory}\n'
                      f'Abrir: http://127.0.0.1:{args.port}/entrar/\nUsuário: previa\nSenha: {password}\n'
                      'Nenhuma conexão ao Neon. Recebimentos nesta janela são fictícios.\n'
                      'Ctrl+C encerra e descarta o banco.\n', flush=True)
                if args.duration > 0:
                    from threading import Thread
                    import time as clock
                    def stop_later():
                        clock.sleep(args.duration)
                        server.shutdown()
                    Thread(target=stop_later, daemon=True).start()
                server.serve_forever()
        except KeyboardInterrupt:
            print('\nPrévia encerrada; banco fictício descartado.', flush=True)
        finally:
            connections.close_all()


if __name__ == '__main__':
    main()
