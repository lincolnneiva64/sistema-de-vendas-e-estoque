from django.apps import AppConfig


class LocacoesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "locacoes"
    verbose_name = "Locacoes"

    def ready(self):
        from .pickup_logging import install
        install()
