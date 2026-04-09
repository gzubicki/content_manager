from __future__ import annotations

from unittest import mock

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from content_manager import settings


class SecurityCookieSettingsTest(SimpleTestCase):
    def test_default_is_false_for_dev_environment(self) -> None:
        with mock.patch.dict("os.environ", {"ENV": "dev"}, clear=False):
            self.assertFalse(settings._is_production_environment())
            self.assertFalse(
                settings._env_bool(
                    "SESSION_COOKIE_SECURE",
                    settings._is_production_environment(),
                )
            )

    def test_default_is_true_for_production_environment(self) -> None:
        with mock.patch.dict("os.environ", {"ENV": "production"}, clear=False):
            self.assertTrue(settings._is_production_environment())
            self.assertTrue(
                settings._env_bool(
                    "SESSION_COOKIE_SECURE",
                    settings._is_production_environment(),
                )
            )

    def test_explicit_env_flag_overrides_default(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {"ENV": "production", "SESSION_COOKIE_SECURE": "0"},
            clear=False,
        ):
            self.assertFalse(
                settings._env_bool(
                    "SESSION_COOKIE_SECURE",
                    settings._is_production_environment(),
                )
            )

    def test_invalid_boolean_value_raises_error(self) -> None:
        with mock.patch.dict("os.environ", {"CSRF_COOKIE_SECURE": "maybe"}, clear=False):
            with self.assertRaises(ImproperlyConfigured):
                settings._env_bool("CSRF_COOKIE_SECURE", False)
