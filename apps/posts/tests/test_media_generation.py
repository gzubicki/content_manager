from unittest.mock import patch

import tempfile
import os
from unittest import mock

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

    def test_normalise_media_payload_extracts_identifiers(self) -> None:
        items = [
            {
                "type": "video",
                "resolver": "telegram",
                "identyfikator": "tg_post_url",
            },
            {
                "type": "photo",
                "resolver": "twitter",
                "tweet_id": "12345",
                "posted_at": "2025-01-01T12:00:00Z",
            },
            {
                "type": "video",
                "resolver": "telegram",
                "identyfikator": "https://t.me/channel/99",
                "caption": "Nagranie z pola walki",
                "posted_at": "2024-06-07T10:15:00Z",
                "has_spoiler": True,
            },
            {
                "type": "video",
                "resolver": "telegram",
                "identyfikator źródła": "https://t.me/source/123",
                "caption": "Nowe nagranie",
                "reference": {
                    "tg_post_url": "https://t.me/source/old",
                },
            },
            {
                "type": "doc",
                "url": "https://example.com/report.pdf",
            },
        ]
        result = services._normalise_media_payload(items, "unused")
        self.assertEqual(len(result), 4)
        twitter_item = result[0]
        self.assertEqual(twitter_item["resolver"], "twitter")
        self.assertIn("tweet_id", twitter_item["reference"])
        self.assertEqual(twitter_item["reference"]["tweet_id"], "12345")
        self.assertEqual(twitter_item["posted_at"], "2025-01-01T12:00:00Z")
        telegram_item = result[1]
        self.assertEqual(telegram_item["resolver"], "telegram")
        self.assertTrue(telegram_item["has_spoiler"])
        self.assertEqual(
            telegram_item["reference"]["tg_post_url"], "https://t.me/channel/99"
        )
        self.assertEqual(telegram_item["caption"], "Nagranie z pola walki")
        self.assertEqual(telegram_item["posted_at"], "2024-06-07T10:15:00Z")
        source_identifier_item = result[2]
        self.assertEqual(source_identifier_item["resolver"], "telegram")
        self.assertEqual(
            source_identifier_item["reference"]["tg_post_url"], "https://t.me/source/123"
        )
        self.assertEqual(result[2]["caption"], "Nowe nagranie")
        doc_item = result[3]
        self.assertEqual(doc_item["source_url"], "https://example.com/report.pdf")
        self.assertEqual(doc_item["type"], "doc")
        for media_entry in result:
            ref = media_entry.get("reference", {})
            self.assertNotIn("message_id", ref)
            self.assertNotEqual(ref.get("message_id"), "tg_post_url")

    def test_attach_media_from_payload_skips_items_without_url(self) -> None:
        PostMedia.objects.create(post=self.post, type="photo", source_url="https://old.example/a.jpg")
        payload = [
            {"type": "photo"},
            {"type": "photo", "source_url": "https://example.com/new.jpg"},
        ]
        with patch("apps.posts.services.cache_media") as mock_cache:
            mock_cache.return_value = "/cache/item.jpg"
            services.attach_media_from_payload(self.post, payload)
        media = list(self.post.media.all())
        self.assertEqual(len(media), 1)
        self.assertEqual(media[0].source_url, "https://example.com/new.jpg")
        mock_cache.assert_called_once_with(media[0])

    def test_attach_media_from_payload_resolves_identifier(self) -> None:
        payload = [
            {
                "type": "photo",
                "resolver": "twitter",
                "reference": {"tweet_id": "123"},
            },
        ]
        with patch("apps.posts.services._resolve_media_reference", return_value="https://example.com/new.jpg") as mock_resolve, patch(
            "apps.posts.services.cache_media"
        ) as mock_cache:
            mock_cache.return_value = "/cache/item.jpg"
            services.attach_media_from_payload(self.post, payload)

        media = list(self.post.media.all())
        self.assertEqual(len(media), 1)
        self.assertEqual(media[0].source_url, "https://example.com/new.jpg")
        mock_resolve.assert_called_once()
        mock_cache.assert_called_once_with(media[0])

    def test_attach_media_from_payload_resolves_polish_identifier(self) -> None:
        payload = [
            {
                "type": "video",
                "resolver": "telegram",
                "identyfikator": "https://t.me/uniannet/129462",
                "caption": "Atak dronów FPV na rosyjskie BMP-2 pod Nowoprokopiwką",
                "posted_at": "2024-06-09T10:32:00+03:00",
            },
        ]
        normalised = services._normalise_media_payload(payload, "unused")
        with patch(
            "apps.posts.services._resolve_media_reference",
            return_value="https://cdn.example/video.mp4",
        ) as mock_resolve, patch("apps.posts.services.cache_media") as mock_cache:
            mock_cache.return_value = "/cache/item.mp4"
            services.attach_media_from_payload(self.post, normalised)

        media = list(self.post.media.all())
        self.assertEqual(len(media), 1)
        self.assertEqual(media[0].type, "video")
        self.assertEqual(media[0].source_url, "https://cdn.example/video.mp4")
        mock_resolve.assert_called_once_with(
            resolver="telegram",
            reference={
                "tg_post_url": "https://t.me/uniannet/129462",
                "posted_at": "2024-06-09T10:32:00+03:00",
            },
            media_type="video",
            caption="Atak dronów FPV na rosyjskie BMP-2 pod Nowoprokopiwką",
        )
        mock_cache.assert_called_once_with(media[0])

    def test_attach_media_auto_expands_telegram_album(self) -> None:
        payload = [
            {
                "type": "photo",
                "resolver": "telegram",
                "reference": {"tg_post_url": "https://t.me/uniannet/109639"},
            }
        ]

        def _fake_cache(pm: PostMedia) -> str:
            path = f"/cache/{pm.id}.bin"
            pm.cache_path = path
            pm.save(update_fields=["cache_path"])
            return path

        with patch(
            "apps.posts.services._resolve_media_reference",
            return_value="file:///tmp/photo1.jpg",
        ) as mock_resolve, patch(
            "apps.posts.services.cache_media",
            side_effect=_fake_cache,
        ) as mock_cache, patch(
            "apps.posts.resolvers.telegram.consume_cached_album",
            return_value=[{"uri": "file:///tmp/photo2.jpg", "type": "photo"}],
        ) as mock_consume:
            services.attach_media_from_payload(self.post, payload)

        media = list(self.post.media.order_by("order"))
        self.assertEqual(len(media), 2)
        self.assertEqual(media[0].source_url, "file:///tmp/photo1.jpg")
        self.assertEqual(media[1].source_url, "file:///tmp/photo2.jpg")
        self.assertEqual(media[1].type, "photo")
        mock_resolve.assert_called_once()
        self.assertEqual(mock_cache.call_count, 2)
        mock_consume.assert_called_once_with("https://t.me/uniannet/109639")
        metadata = self.post.source_metadata.get("media", [])
        self.assertEqual(len(metadata), 2)
        self.assertTrue(metadata[1].get("auto_album"))

    def test_attach_media_skips_auto_expand_when_multiple_entries_present(self) -> None:
        payload = [
            {
                "type": "photo",
                "resolver": "telegram",
                "reference": {"tg_post_url": "https://t.me/uniannet/109639"},
            },
            {
                "type": "photo",
                "resolver": "telegram",
                "reference": {"tg_post_url": "https://t.me/uniannet/109639"},
            },
        ]

        def _fake_cache(pm: PostMedia) -> str:
            path = f"/cache/{pm.id}.bin"
            pm.cache_path = path
            pm.save(update_fields=["cache_path"])
            return path

        with patch(
            "apps.posts.services._resolve_media_reference",
            side_effect=["file:///tmp/photo1.jpg", "file:///tmp/photo2.jpg"],
        ) as mock_resolve, patch(
            "apps.posts.services.cache_media",
            side_effect=_fake_cache,
        ) as mock_cache, patch(
            "apps.posts.resolvers.telegram.consume_cached_album",
            return_value=[{"uri": "file:///tmp/photo3.jpg", "type": "photo"}],
        ) as mock_consume:
            services.attach_media_from_payload(self.post, payload)

        media = list(self.post.media.order_by("order"))
        self.assertEqual(len(media), 2)
        self.assertEqual(media[0].source_url, "file:///tmp/photo1.jpg")
        self.assertEqual(media[1].source_url, "file:///tmp/photo2.jpg")
        self.assertEqual(mock_cache.call_count, 2)
        mock_resolve.assert_called()
        mock_consume.assert_not_called()
        metadata = self.post.source_metadata.get("media", [])
        self.assertEqual(len(metadata), 2)
        self.assertFalse(any(entry.get("auto_album") for entry in metadata))

    def test_resolve_media_reference_without_resolver_service(self) -> None:
        with mock.patch.dict(os.environ, {"MEDIA_RESOLVER_URL": ""}):
            url = services._resolve_media_reference(
                resolver="telegram",
                reference={"tg_post_url": "https://t.me/source/456"},
                media_type="video",
                caption="desc",
            )

        self.assertEqual(url, "https://t.me/source/456")

    def test_attach_media_removes_when_download_fails(self) -> None:
        payload = [
            {"type": "photo", "source_url": "https://example.com/new.jpg"},
        ]
        with patch("apps.posts.services.cache_media", side_effect=Exception("404")):
            services.attach_media_from_payload(self.post, payload)

        self.assertEqual(self.post.media.count(), 0)

    def test_attach_media_removes_when_cache_empty(self) -> None:
        payload = [
            {"type": "photo", "source_url": "https://example.com/new.jpg"},
        ]

        with patch("apps.posts.services.cache_media", return_value=""):
            services.attach_media_from_payload(self.post, payload)

        self.assertEqual(self.post.media.count(), 0)

    def test_cache_media_downloads_file(self) -> None:
        pm = PostMedia.objects.create(post=self.post, type="photo", source_url="https://example.com/img.png")
        fake_bytes = b"binary\x00data"

        class _Resp:
            def __init__(self, content: bytes):
                self.content = content
                self.headers = {"content-type": "image/png"}

            def raise_for_status(self) -> None:
                return None

        with patch("apps.posts.services.httpx.get", return_value=_Resp(fake_bytes)) as mock_get:
            path = services.cache_media(pm)

        mock_get.assert_called_once_with("https://example.com/img.png", timeout=30.0, follow_redirects=True)

        self.assertTrue(path)
        self.assertTrue(os.path.exists(path))
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), fake_bytes)

    def test_cache_media_corrects_media_type(self) -> None:
        pm = PostMedia.objects.create(
            post=self.post,
            type="photo",
            source_url="https://example.com/video.mp4",
            reference_data={}
        )
        fake_bytes = b"video-bytes"

        class _Resp:
            def __init__(self, content: bytes):
                self.content = content
                self.headers = {"content-type": "video/mp4"}

            def raise_for_status(self) -> None:
                return None

        with patch("apps.posts.services.httpx.get", return_value=_Resp(fake_bytes)):
            path = services.cache_media(pm)

        self.assertTrue(path.endswith(".mp4"))
        pm.refresh_from_db()
        self.assertEqual(pm.type, "video")
        self.assertEqual(pm.reference_data.get("detected_type"), "video")


class ArticleSourceMetadataTest(TestCase):
    def setUp(self) -> None:
        self.channel = Channel.objects.create(name="Kanał", slug="kanal", tg_channel_id="@kanal")

    def test_create_post_from_payload_saves_article_sources(self) -> None:
        payload = {
            "post": {"text": "Nowy wpis"},
            "media": [],
            "source": [
                "https://example.com/artykul-1",
                {"url": "https://example.com/artykul-2", "title": "Raport"},
            ],
        }

        post = services.create_post_from_payload(self.channel, payload)

        metadata = post.source_metadata
        self.assertIn("article", metadata)
        sources = metadata["article"].get("sources", [])
        self.assertEqual(len(sources), 2)
        self.assertEqual(sources[0]["url"], "https://example.com/artykul-1")
        self.assertEqual(sources[1]["label"], "Raport")

    def test_attach_media_preserves_article_metadata(self) -> None:
        post = services.create_post_from_payload(
            self.channel,
            {"post": {"text": "Nowy wpis"}, "media": [], "source": "https://example.com/a"},
        )

        payload = [{"type": "photo", "source_url": "https://example.com/img.jpg"}]

        with patch("apps.posts.services.cache_media", return_value="/cache/img.jpg"):
            services.attach_media_from_payload(post, payload)

        metadata = post.source_metadata
        self.assertIn("article", metadata)
        self.assertIn("media", metadata)
        self.assertEqual(metadata["article"]["sources"][0]["url"], "https://example.com/a")
