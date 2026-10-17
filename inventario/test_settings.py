"""Disposable local databases only. No production URL or local operational .env."""
import os
import tempfile
from urllib.parse import urlparse

os.environ["SECRET_KEY"] = "inventario-disposable-test-key"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
from sistema.settings import *  # noqa: F403,E402

DEBUG = True
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "testserver"]
SECURE_SSL_REDIRECT = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
TEST_RUNNER = "offline.test_runner.OfflineTestRunner"
_temporary = tempfile.TemporaryDirectory(prefix="inventario-tests-")
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:",
                         "TEST": {"NAME": os.path.join(_temporary.name, "test.sqlite3")}}}
if os.environ.get("INVENTARIO_TEST_POSTGRES_URL"):
    import dj_database_url
    _url = os.environ["INVENTARIO_TEST_POSTGRES_URL"]
    _parsed = urlparse(_url)
    if _parsed.hostname not in {"127.0.0.1", "localhost"} or not _parsed.path.startswith("/inventario_disposable"):
        raise RuntimeError("Only disposable loopback PostgreSQL is allowed.")
    DATABASES = {"default": dj_database_url.parse(_url)}
    DATABASES["default"]["TEST"] = {"NAME": "test_inventario_disposable"}
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
STORAGES = {"default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
            "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}}
INVENTARIO_AJUSTES_HABILITADOS = False

# Shared synthetic identity for the commercial integration suites.
OFFLINE_ENVIRONMENT_ID = "offline-isolated-tests"
