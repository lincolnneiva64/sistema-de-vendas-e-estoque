from django.apps import AppConfig


class EstoqueConfig(AppConfig):
    name = 'estoque'

    def ready(self):
        from . import grupos_produtos  # noqa: F401
