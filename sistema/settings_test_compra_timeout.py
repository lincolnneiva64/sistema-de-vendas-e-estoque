"""Standalone settings: never import operational settings or .env."""
import os
from .settings_test_compras import *  # noqa: F403

INSTALLED_APPS = [*INSTALLED_APPS, "inventario"]
DATABASES = {"default": {
    "ENGINE": "django.db.backends.postgresql", "NAME": "inventario_disposable",
    "USER": "inventario_fixture", "HOST": "127.0.0.1",
    "PORT": int(os.environ["COMPRA_TIMEOUT_PG_PORT"]),
    "TEST": {"NAME": "test_inventario_disposable"},
}}
TEST_RUNNER = "django.test.runner.DiscoverRunner"
