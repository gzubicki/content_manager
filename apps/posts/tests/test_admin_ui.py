from __future__ import annotations

import os
import tempfile
import time
import unittest

import os

os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")

from django.contrib.auth import get_user_model
from urllib.parse import urlparse

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import Client, override_settings

from apps.posts.models import Channel

try:
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover
    sync_playwright = None


class PostAdminUITest(StaticLiveServerTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if sync_playwright is None:
            raise unittest.SkipTest("Playwright is not available")
        cls._static_override = override_settings(
            STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage"
        )
        cls._static_override.enable()
        super().setUpClass()
        try:
            cls._playwright = sync_playwright().start()
            cls.browser = cls._playwright.firefox.launch(headless=True)
        except Exception as exc:  # pragma: no cover
            super().tearDownClass()
            raise unittest.SkipTest(f"Cannot launch Playwright Firefox: {exc}")

    @classmethod
    def tearDownClass(cls) -> None:
        browser = getattr(cls, "browser", None)
        if browser is not None:
            browser.close()
        if hasattr(cls, "_playwright"):
            cls._playwright.stop()
        if hasattr(cls, "_static_override"):
            cls._static_override.disable()
        super().tearDownClass()

    def setUp(self) -> None:
        User = get_user_model()
        self.user, _ = User.objects.get_or_create(
            username="admin",
            defaults={
                "email": "admin@example.com",
                "is_staff": True,
                "is_superuser": True,
            },
        )
        self.user.set_password("pass1234")
        self.user.save()
        self.channel, _ = Channel.objects.get_or_create(
            slug="kanal-testowy",
            defaults={
                "name": "Kanał testowy",
                "tg_channel_id": "@kanał",
            },
        )
        self.page = self.browser.new_page()
        self.page.on("console", lambda msg: print("BROWSER", msg.type, msg.text))
        self.page.on("pageerror", lambda exc: print("PAGEERROR", exc))
        self.page.context.add_init_script(path="/app/static/admin/post_edit.js")

    def tearDown(self) -> None:
        self.page.close()

    def wait_for_js(self, script: str, timeout_ms: int = 5000) -> None:
        deadline = time.time() + (timeout_ms / 1000.0)
        while time.time() < deadline:
            if self.page.evaluate(script):
                return
            self.page.wait_for_timeout(100)
        self.fail(f"Condition not met: {script}")

    def login_admin(self) -> None:
        client = Client()
        client.force_login(self.user)
        session_cookie = client.cookies.get("sessionid")
        assert session_cookie is not None

        self.page.context.add_cookies([
            {
                "name": session_cookie.key,
                "value": session_cookie.value,
                "url": self.live_server_url,
                "httpOnly": True,
                "sameSite": "Lax",
                "secure": False,
            }
        ])
        self.page.goto(f"{self.live_server_url}/admin/")
        self.page.wait_for_load_state("networkidle")
        if "/login" in self.page.url:
            self.page.fill("input[name='username']", self.user.username)
            self.page.fill("input[name='password']", "pass1234")
            self.page.click("button[type='submit']")
            self.page.wait_for_load_state("networkidle")
        self.page.wait_for_selector("section#content")

    def test_media_preview_updates_without_save(self) -> None:
        self.login_admin()
        page = self.page
        page.goto(f"{self.live_server_url}/admin/posts/draftpost/add/")
        page.wait_for_load_state("networkidle")
        if "/login" in page.url:
            page.fill("input[name='username']", self.user.username)
            page.fill("input[name='password']", "pass1234")
            page.click("button[type='submit']")
            page.wait_for_load_state("networkidle")
            page.goto(f"{self.live_server_url}/admin/posts/draftpost/add/")
            page.wait_for_load_state("networkidle")
        assert "/admin/posts/draftpost/add/" in page.url, page.url
        page.wait_for_selector("select#id_channel")
        bridge_type = page.evaluate("() => typeof window.postEditBridge")
        self.assertEqual(bridge_type, "object")
        page.select_option("select#id_channel", value=str(self.channel.pk))
        page.fill("#id_text", "Przykładowa treść")

        preview_locator = "#media-0 [data-post-media-preview]"
        url_field = "#id_media-0-source_url"

        image_url = f"{self.live_server_url}/static/vendor/adminlte/img/AdminLTELogo.png"
        page.fill(url_field, image_url)
        page.wait_for_timeout(500)
        page.evaluate(
            "() => (document.querySelector('#media-0 [data-post-media-preview]') || {}).outerHTML || ''"
        )
        self.wait_for_js(
            "() => {\n                const el = document.querySelector('#media-0 [data-post-media-preview]');\n                return !!(el && el.getAttribute('data-has-preview') === '1' && el.querySelector('img'));\n            }"
        )

        import base64

        temp = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
        png_bytes = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIW2P4DwQACfsD/URCZhQAAAAASUVORK5CYII="
        )
        temp.write(png_bytes)
        temp.close()
        self.addCleanup(lambda: os.remove(temp.name))

        page.set_input_files("#id_media-0-upload", temp.name)
        page.wait_for_timeout(500)
        self.assertTrue(
            page.evaluate(
                "() => {\n                    const el = document.querySelector('#media-0 [data-post-media-preview]');\n                    return !!(el && el.getAttribute('data-has-preview') === '1' && el.querySelector('img'));\n                }"
            )
        )

        # Ensure preview remains visible (upload preview)
        self.assertIn("img", page.evaluate(
            "() => (document.querySelector('#media-0 [data-post-media-preview]') || {}).innerHTML || ''"
        ))
