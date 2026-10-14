"""Manual settings must never reuse the operational SQLite as a test database."""
import os
import runpy
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase


class ManualSQLiteIsolationTests(SimpleTestCase):
    def test_manual_database_survives_real_django_test_runner(self):
        with TemporaryDirectory(prefix='manual-settings-sentinel-') as workspace:
            local_app_data = Path(workspace)
            directory = local_app_data / 'Temp' / 'locacoes-manual-settings-test'
            directory.mkdir(parents=True)
            token = 'test-only-owner-token'
            (directory / '.locacoes-manual-owner').write_text(token, encoding='ascii')
            database = directory / 'locacoes-manual.sqlite3'
            sentinel = b'MANUAL DATABASE SENTINEL - MUST NOT BE OPENED OR RECREATED'
            database.write_bytes(sentinel)
            environment = {
                'LOCALAPPDATA': str(local_app_data),
                'LOCACOES_MANUAL_TEMP_DIR': str(directory),
                'LOCACOES_MANUAL_DB_PATH': str(database),
                'LOCACOES_MANUAL_TOKEN': token,
                'DJANGO_SETTINGS_MODULE': 'sistema.settings_locacoes_manual',
                'DATABASE_URL': 'sqlite:///:memory:',
                'OFFLINE_TEST_DATABASE_URL': '',
            }
            with patch.dict(os.environ, environment):
                config = runpy.run_path(str(settings.BASE_DIR / 'sistema/settings_locacoes_manual.py'))
            db = config['DATABASES']['default']
            self.assertEqual(db['ENGINE'], 'django.db.backends.sqlite3')
            self.assertEqual(Path(db['NAME']).resolve(), database.resolve())
            self.assertNotEqual(Path(db['TEST']['NAME']).resolve(), database.resolve())
            self.assertTrue(Path(db['TEST']['NAME']).parent.name.startswith('locacoes-manual-tests-'))
            result = subprocess.run([
                sys.executable, str(settings.BASE_DIR / 'manage.py'), 'test',
                'offline.tests.OfflineAPITests.test_login_and_session_preparation',
                '--settings=sistema.settings_locacoes_manual', '--noinput',
            ], cwd=settings.BASE_DIR, env={**os.environ, **environment},
                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=90)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(database.read_bytes(), sentinel)
