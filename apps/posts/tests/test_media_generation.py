import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings

from apps.posts import services
from apps.posts.models import Channel, Post, PostMedia


class GeneratePhotoFallbackTest(TestCase):
    def setUp(self):
        self.channel = Channel.objects.create(
            name="Kanał",
            slug="kanal",
            tg_channel_id="@kanal",
        )
        self.post = Post.objects.create(channel=self.channel, text="treść")

    def test_downloads_image_when_only_url_is_returned(self):
        media = PostMedia.objects.create(post=self.post, type="photo")
        fake_payload = SimpleNamespace(
            data=[SimpleNamespace(b64_json=None, url="http://example.com/image.png")]
        )
        mock_client = Mock()
        mock_client.images.generate.return_value = fake_payload

        class DummyHTTPResponse:
            def __init__(self, body: bytes):
                self.content = body

            def raise_for_status(self):
                return None

        downloaded = DummyHTTPResponse(b"obrazek")

        with tempfile.TemporaryDirectory() as tmpdir:
            with override_settings(MEDIA_ROOT=tmpdir):
                with patch("apps.posts.services._client", return_value=mock_client):
                    with patch("apps.posts.services.httpx.get", return_value=downloaded) as mock_get:
                        cache_path = services._generate_photo_for_media(media, "prompt")

                        self.assertTrue(cache_path)
                        stored = Path(cache_path)
                        self.assertTrue(stored.exists())
                        self.assertEqual(stored.read_bytes(), b"obrazek")
                        self.assertEqual(media.cache_path, cache_path)
                        self.assertIsNotNone(media.expires_at)

                        mock_client.images.generate.assert_called_once()
                        call_kwargs = mock_client.images.generate.call_args.kwargs
                        self.assertEqual(call_kwargs["response_format"], "b64_json")
                        mock_get.assert_called_once_with("http://example.com/image.png", timeout=30)
