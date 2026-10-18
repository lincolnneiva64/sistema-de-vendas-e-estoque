"""SQLite-only test settings. No operational credentials and no migrations."""
import os
import tempfile

# Set before importing production settings; this worktree must have no .env.
os.environ["SECRET_KEY"] = "inventario-confiabilidade-ficticio-sem-acesso-producao"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
from sistema.settings import *  # noqa: E402,F403

DEBUG = True
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "testserver"]
SECURE_SSL_REDIRECT = SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = False
SECURE_HSTS_SECONDS = 0
SECURE_PROXY_SSL_HEADER = None
LOCAL_SALES_LOOPBACK = False
_directory = tempfile.TemporaryDirectory(prefix="inventario-confiabilidade-")
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3",
                         "NAME": os.path.join(_directory.name, "ficticio.sqlite3")}}
STORAGES = {"default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
            "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}}
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
TEST_RUNNER = "inventario.test_runner_confiabilidade.SemMigrationsRunner"
