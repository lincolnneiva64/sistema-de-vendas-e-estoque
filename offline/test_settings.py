"""Isolated validation settings; never use the operational database."""
import os
import tempfile
import dj_database_url
from sistema.settings import *  # noqa: F403

DEBUG = True
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "testserver"]
SECURE_SSL_REDIRECT = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
OFFLINE_ENVIRONMENT_ID = "offline-isolated-tests"
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
TEST_RUNNER = "offline.test_runner.OfflineTestRunner"
if os.environ.get("OFFLINE_TEST_DATABASE_URL"):
    DATABASES = {"default": dj_database_url.parse(os.environ["OFFLINE_TEST_DATABASE_URL"])}
else:
    # LiveServer shares a single in-memory SQLite connection across request
    # threads. Concurrent auth/snapshot probes can fail with InterfaceError.
    # A private test file gives each thread its own connection; production
    # settings and PostgreSQL test configuration are unaffected.
    _offline_sqlite_test_directory = tempfile.TemporaryDirectory(prefix="offline-isolated-tests-")
    DATABASES["default"]["TEST"] = {
        "NAME": os.path.join(_offline_sqlite_test_directory.name, "test.sqlite3"),
    }
STORAGES = {"default": {"BACKEND": "django.core.files.storage.FileSystemStorage"}, "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}}
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
