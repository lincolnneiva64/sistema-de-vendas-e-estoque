from unittest.mock import patch

from django.test import SimpleTestCase

from sistema import settings as project_settings


class CloudflareR2EnvConfigTests(SimpleTestCase):
    def test_clean_env_config_with_changed_strips_edge_whitespace(self):
        with patch.object(project_settings, "config", return_value=" \r\n chave-r2 \n\r "):
            value, changed = project_settings._clean_env_config_with_changed(
                "CLOUDFLARE_R2_ACCESS_KEY_ID",
                default="",
            )

        self.assertEqual(value, "chave-r2")
        self.assertTrue(changed)

    def test_clean_env_value_preserves_internal_whitespace(self):
        value = project_settings._clean_env_value(" \r\n chave r2 interna \n ")

        self.assertEqual(value, "chave r2 interna")
