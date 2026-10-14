"""Django settings para validacao manual com SQLite temporario obrigatorio."""
import os
import tempfile
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured


BASE_DIR = Path(__file__).resolve().parent.parent
LOCAL_APP_DATA = os.environ.get("LOCALAPPDATA", "").strip()
if not LOCAL_APP_DATA:
    raise ImproperlyConfigured("LOCALAPPDATA nao esta definido; ambiente temporario recusado.")
TEMP_ROOT = (Path(LOCAL_APP_DATA) / "Temp").resolve()
TEMP_DIR_VALUE = os.environ.get("LOCACOES_MANUAL_TEMP_DIR", "").strip()
DB_VALUE = os.environ.get("LOCACOES_MANUAL_DB_PATH", "").strip()
TOKEN = os.environ.get("LOCACOES_MANUAL_TOKEN", "").strip()

if not TEMP_DIR_VALUE or not DB_VALUE or not TOKEN:
    raise ImproperlyConfigured(
        "Defina LOCACOES_MANUAL_TEMP_DIR, LOCACOES_MANUAL_DB_PATH e "
        "LOCACOES_MANUAL_TOKEN pelo launcher isolado."
    )

MANUAL_TEMP_DIR = Path(TEMP_DIR_VALUE).resolve()
MANUAL_DB_PATH = Path(DB_VALUE).resolve()
MARKER = MANUAL_TEMP_DIR / ".locacoes-manual-owner"
if (
    MANUAL_TEMP_DIR.parent != TEMP_ROOT
    or not MANUAL_TEMP_DIR.name.startswith("locacoes-manual-")
    or not MANUAL_TEMP_DIR.is_dir()
    or MANUAL_DB_PATH != MANUAL_TEMP_DIR / "locacoes-manual.sqlite3"
    or not MARKER.is_file()
    or MARKER.read_text(encoding="ascii").strip() != TOKEN
):
    raise ImproperlyConfigured(
        "O SQLite precisa estar no diretorio temporario exclusivo criado pelo launcher."
    )
if MANUAL_DB_PATH.exists() and not MANUAL_DB_PATH.is_file():
    raise ImproperlyConfigured("O caminho do banco temporario nao e um arquivo regular.")

SECRET_KEY = "validacao-local-locacoes-sem-segredos-reais"
DEBUG = True
ALLOWED_HOSTS = ["127.0.0.1", "localhost"]
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "estoque",
    "locacoes",
    "offline",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "sistema.urls"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
        "estoque.context_processors.ambiente_sistema",
    ]},
}]
WSGI_APPLICATION = "sistema.wsgi.application"
_sqlite_test_directory = tempfile.TemporaryDirectory(prefix="locacoes-manual-tests-")
DATABASES = {"default": {
    "ENGINE": "django.db.backends.sqlite3",
    "NAME": str(MANUAL_DB_PATH),
    "TEST": {"NAME": str(Path(_sqlite_test_directory.name) / "test.sqlite3")},
}}
TEST_RUNNER = "offline.test_runner.OfflineTestRunner"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
STATIC_URL = "/static/"
STATIC_ROOT = str(MANUAL_TEMP_DIR / "static")
STATICFILES_DIRS = [BASE_DIR / "static"]
MEDIA_ROOT = str(MANUAL_TEMP_DIR / "media")
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
LANGUAGE_CODE = "pt-br"
TIME_ZONE = "America/Sao_Paulo"
USE_I18N = True
USE_TZ = True
OFFLINE_ENVIRONMENT_ID = "locacoes-manual-isolated"
