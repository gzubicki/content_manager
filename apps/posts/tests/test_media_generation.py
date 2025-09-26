import base64
import os
import tempfile
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

import httpx
from django.test import TestCase, override_settings

from apps.posts import services
from apps.posts.models import Channel, Post, PostMedia


@dataclass
class _FakeImageData:
    url: str | None = None
    b64_json: str | None = None

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)


class _FakeImageResponse:
    def __init__(self, data: list[_FakeImageData]):
        self.data = data


class _FakeImagesClient:
    def __init__(self, response: _FakeImageResponse):
        self._response = response
        self.calls: list[dict[str, Any]] = []

    def generate(self, **kwargs: Any) -> _FakeImageResponse:
        self.calls.append(kwargs)
        return self._response


class _FakeOpenAIClient:
    def __init__(self, response: _FakeImageResponse):
        self.images = _FakeImagesClient(response)


class _FakeHttpxResponse:
    def __init__(self, *, status_code: int, content: bytes, url: str = "", history: list["_FakeHttpxResponse"] | None = None):
        self.status_code = status_code
        self.content = content
        self.url = url
        self.history = history or []
        self.headers: dict[str, str] = {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "HTTP error",
                request=httpx.Request("GET", self.url or "https://example.invalid"),
                response=httpx.Response(self.status_code),
            )


class _RedirectingGet:
    def __init__(self, final_bytes: bytes):
        self.final_bytes = final_bytes
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.last_response: _FakeHttpxResponse | None = None
        self._redirect_response = _FakeHttpxResponse(status_code=302, content=b"", url="https://cdn.example/intermediate.png")

    def __call__(self, url: str, *args: Any, **kwargs: Any) -> _FakeHttpxResponse:
        self.calls.append((url, kwargs))
        follow_redirects = kwargs.get("follow_redirects", False)
        if follow_redirects:
            self.last_response = _FakeHttpxResponse(
                status_code=200,
                content=self.final_bytes,
                url="https://cdn.example/final.png",
                history=[self._redirect_response],
            )
            return self.last_response
        self.last_response = self._redirect_response
        return self.last_response


class GeneratePhotoFallbackTest(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.temp_media = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_media.cleanup)
        override = override_settings(MEDIA_ROOT=self.temp_media.name)
        override.enable()
        self.addCleanup(override.disable)
        self.channel = Channel.objects.create(name="Kanał", slug="kanal", tg_channel_id="@kanal")
        self.post = Post.objects.create(channel=self.channel, text="Treść")

    def _create_media(self) -> PostMedia:
        return PostMedia.objects.create(post=self.post, type="photo", source_url="")

    def _cleanup_file(self, path: str | None) -> None:
        if path and os.path.exists(path):
            os.remove(path)

    def test_uses_base64_payload_when_available(self) -> None:
        pm = self._create_media()
        image_bytes = b"binary-image"
        payload = _FakeImageResponse([_FakeImageData(b64_json=base64.b64encode(image_bytes).decode("ascii"))])
        client = _FakeOpenAIClient(payload)

        with patch("apps.posts.services._client", return_value=client), patch("apps.posts.services.httpx.get") as mock_get:
            path = services._generate_photo_for_media(pm, "prompt")

        self.addCleanup(self._cleanup_file, path)
        pm.refresh_from_db()
        self.assertTrue(path)
        self.assertEqual(pm.cache_path, path)
        self.assertTrue(os.path.exists(path))
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), image_bytes)
        mock_get.assert_not_called()

    def test_downloads_image_following_redirects(self) -> None:
        pm = self._create_media()
        payload = _FakeImageResponse([_FakeImageData(url="https://example.com/image.png")])
        client = _FakeOpenAIClient(payload)
        redirecting_get = _RedirectingGet(b"redirected-bytes")

        with patch("apps.posts.services._client", return_value=client), patch(
            "apps.posts.services.httpx.get", side_effect=redirecting_get
        ):
            path = services._generate_photo_for_media(pm, "prompt")

        self.addCleanup(self._cleanup_file, path)
        pm.refresh_from_db()
        self.assertTrue(path)
        self.assertTrue(os.path.exists(path))
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), b"redirected-bytes")

        self.assertEqual(len(redirecting_get.calls), 1)
        called_url, kwargs = redirecting_get.calls[0]
        self.assertEqual(called_url, "https://example.com/image.png")
        self.assertTrue(kwargs.get("follow_redirects"))
        self.assertIsNotNone(redirecting_get.last_response)
        self.assertTrue(redirecting_get.last_response.history)
        self.assertEqual(redirecting_get.last_response.history[0].status_code, 302)
