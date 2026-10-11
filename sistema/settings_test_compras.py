"""Testes de compras exclusivamente em SQLite descartavel, sem settings de producao."""
from pathlib import Path
from tempfile import TemporaryDirectory

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = "somente-testes-isolados-de-compras"
DEBUG = True
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "testserver"]
INSTALLED_APPS = [
    "django.contrib.admin", "django.contrib.auth", "django.contrib.contenttypes",
    "django.contrib.sessions", "django.contrib.messages", "django.contrib.staticfiles",
    "estoque", "locacoes", "offline",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware", "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware", "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware", "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "sistema.urls"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates", "DIRS": [], "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request", "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]
COMPRAS_TEST_DIRECTORY = TemporaryDirectory(prefix="compras-isolated-", ignore_cleanup_errors=True)
DATABASES = {"default": {
    "ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:",
    "TEST": {"NAME": str(Path(COMPRAS_TEST_DIRECTORY.name) / "test.sqlite3")},
}}
TEST_RUNNER = "estoque.test_runner_compras.CompraTestRunner"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
STATIC_URL = "/static/"
STATIC_ROOT = str(Path(COMPRAS_TEST_DIRECTORY.name) / "static")
STATICFILES_DIRS = [BASE_DIR / "static"]
MEDIA_ROOT = str(Path(COMPRAS_TEST_DIRECTORY.name) / "media")
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
LANGUAGE_CODE = "pt-br"
TIME_ZONE = "America/Sao_Paulo"
USE_I18N = True
USE_TZ = True
OFFLINE_ENVIRONMENT_ID = "compras-isolated-tests"
