from unittest.mock import patch

import tempfile
import os

from django.test import TestCase, override_settings

from apps.posts import services
from apps.posts.models import Channel, Post, PostMedia


class MediaHandlingTest(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self._tmp_media = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp_media.cleanup)
        override = override_settings(MEDIA_ROOT=self._tmp_media.name)
        override.enable()
        self.addCleanup(override.disable)
        self.channel = Channel.objects.create(name="Kanał", slug="kanal", tg_channel_id="@kanal")
        self.post = Post.objects.create(channel=self.channel, text="Treść")

    def test_normalise_media_payload_requires_url(self) -> None:
        items = [
            {"type": "photo", "title": "Brak", "source": "article"},
            {"type": "photo", "title": "Jest", "source": "article", "url": "https://example.com/img.jpg"},
            {"type": "video", "title": "Video", "url": "https://cdn.example/video.mp4"},
        ]
        result = services._normalise_media_payload(items, "unused")
        self.assertEqual(len(result), 2)
        self.assertTrue(all(item["url"] for item in result))
        self.assertEqual(result[0]["title"], "Jest")
        self.assertEqual(result[1]["type"], "video")

    def test_attach_media_from_payload_skips_items_without_url(self) -> None:
        PostMedia.objects.create(post=self.post, type="photo", source_url="https://old.example/a.jpg")
        payload = [
            {"type": "photo", "title": "Brak", "source": "article"},
            {"type": "photo", "title": "Nowe", "source": "article", "url": "https://example.com/new.jpg"},
        ]
        with patch("apps.posts.services.cache_media") as mock_cache:
            services.attach_media_from_payload(self.post, payload)
        media = list(self.post.media.all())
        self.assertEqual(len(media), 1)
        self.assertEqual(media[0].source_url, "https://example.com/new.jpg")
        mock_cache.assert_called_once_with(media[0])

    def test_attach_media_removes_when_download_fails(self) -> None:
        payload = [
            {"type": "photo", "title": "Nowe", "source": "article", "url": "https://example.com/new.jpg"},
        ]
        with patch("apps.posts.services.cache_media", side_effect=Exception("404")):
            services.attach_media_from_payload(self.post, payload)

        self.assertEqual(self.post.media.count(), 0)

    def test_attach_media_removes_when_cache_empty(self) -> None:
        payload = [
            {"type": "photo", "title": "Nowe", "source": "article", "url": "https://example.com/new.jpg"},
        ]

        with patch("apps.posts.services.cache_media", return_value=""):
            services.attach_media_from_payload(self.post, payload)

        self.assertEqual(self.post.media.count(), 0)

    def test_cache_media_downloads_file(self) -> None:
        pm = PostMedia.objects.create(post=self.post, type="photo", source_url="https://example.com/img.png")
        fake_bytes = b"binary\x00data"

        def _fake_stream(method, url, timeout=30):
            class _Resp:
                def __init__(self, content: bytes):
                    self._content = content
                    self.headers = {}

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc_val, exc_tb):
                    return False

                def iter_bytes(self):
                    yield self._content

                def raise_for_status(self):
                    return None

            return _Resp(fake_bytes)

        with patch("apps.posts.services.httpx.stream", side_effect=_fake_stream):
            path = services.cache_media(pm)

        self.assertTrue(path)
        self.assertTrue(os.path.exists(path))
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), fake_bytes)
