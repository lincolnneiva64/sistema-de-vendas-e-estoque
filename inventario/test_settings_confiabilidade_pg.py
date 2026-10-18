"""Only the freshly bootstrapped, guarded loopback cluster is accepted."""
import os
from .test_settings_confiabilidade import *  # noqa: F403

DATABASES = {"default": {
    "ENGINE": "django.db.backends.postgresql",
    "NAME": "inventario_confiabilidade",
    "USER": "inventario_fixture",
    "HOST": "127.0.0.1",
    "PORT": int(os.environ["INVENTARIO_CONFIABILIDADE_PG_PORT"]),
}}
TEST_RUNNER = "inventario.test_runner_confiabilidade.PostgreSQLSemMigrationsRunner"
