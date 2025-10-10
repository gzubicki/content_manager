import asyncio
import os
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from django.test import TestCase

from apps.posts import tasks
from apps.posts.models import Channel, Post, PostMedia


class PublishMetadataTest(TestCase):
    def setUp(self):
        self.channel = Channel.objects.create(
            name="Test channel",
            slug="test-metadata",
            tg_channel_id="@test_metadata",
            bot_token="token",
        )

    def test_video_metadata_from_reference_prefers_video_section(self):
        metadata, flags = tasks._video_metadata_from_reference(
            {"video_metadata": {"width": 1280, "height": 720, "duration": 12}}
        )

        self.assertEqual(metadata, {"width": 1280, "height": 720, "duration": 12})
        self.assertTrue(all(flags.values()))

    @patch("apps.posts.tasks._probe_video_metadata", return_value={})
    def test_video_metadata_for_media_normalises_reference(self, mock_probe):
        post = Post.objects.create(channel=self.channel, text="Video post")
        media = PostMedia.objects.create(
            post=post,
            type="video",
            reference_data={"width": "1920", "height": "1080", "duration": "15"},
        )

        metadata, changed = tasks._video_metadata_for_media(media)

        self.assertFalse(mock_probe.called)
        self.assertTrue(changed)
        self.assertEqual(metadata, {"width": 1920, "height": 1080, "duration": 15})
        self.assertIn("video_metadata", media.reference_data)
        self.assertEqual(media.reference_data["video_metadata"], metadata)

    @patch("apps.posts.tasks._probe_video_metadata", return_value={"width": 640, "height": 360, "duration": 9})
    def test_video_metadata_for_media_uses_probe_when_missing(self, mock_probe):
        post = Post.objects.create(channel=self.channel, text="Video post")
        with tempfile.NamedTemporaryFile(delete=False) as temp_file:
            temp_file.write(b"fake")
        try:
            media = PostMedia.objects.create(
                post=post,
                type="video",
                cache_path=temp_file.name,
                reference_data={},
            )

            metadata, changed = tasks._video_metadata_for_media(media)
        finally:
            os.unlink(temp_file.name)

        self.assertTrue(mock_probe.called)
        self.assertTrue(changed)
        self.assertEqual(metadata, {"width": 640, "height": 360, "duration": 9})
        self.assertEqual(media.reference_data["video_metadata"], metadata)

    @patch("apps.posts.tasks._probe_video_metadata", return_value={})
    @patch("apps.posts.tasks.services._bot_for")
    def test_publish_async_includes_video_metadata(self, mock_bot_for, mock_probe):
        post = Post.objects.create(channel=self.channel, text="Caption text")
        with tempfile.NamedTemporaryFile(delete=False) as temp_file:
            temp_file.write(b"fake video")

        media = PostMedia.objects.create(
            post=post,
            type="video",
            cache_path=temp_file.name,
            reference_data={"width": 800, "height": 600, "duration": 5},
        )
        media.tg_file_id = "cached"
        media.save(update_fields=["tg_file_id"])

        class _FakeMessage:
            def __init__(self, message_id: int):
                self.message_id = message_id
                self.video = SimpleNamespace(file_id="cached")

        class _FakeBot:
            def __init__(self):
                self.media_calls = []
                self.message_calls = []

            async def send_media_group(self, chat_id, media):
                self.media_calls.append((chat_id, media))
                return [_FakeMessage(111)]

            async def send_message(self, chat_id, text):
                self.message_calls.append((chat_id, text))
                return SimpleNamespace(message_id=222)

        fake_bot = _FakeBot()
        mock_bot_for.return_value = fake_bot

        try:
            result = asyncio.run(tasks._publish_async(post, [media]))
        finally:
            os.unlink(temp_file.name)

        self.assertEqual(result, ([111], 111))
        self.assertEqual(len(fake_bot.media_calls), 1)
        _, media_payload = fake_bot.media_calls[0]
        self.assertEqual(len(media_payload), 1)
        video = media_payload[0]
        self.assertEqual(video.width, 800)
        self.assertEqual(video.height, 600)
        self.assertEqual(video.duration, 5)
        self.assertTrue(video.supports_streaming)
        self.assertTrue(getattr(media, "_reference_data_dirty", False))
        self.assertEqual(media.reference_data.get("video_metadata", {}), {"width": 800, "height": 600, "duration": 5})

    @patch("apps.posts.tasks.services.compute_dupe", return_value=0.42)
    @patch("apps.posts.tasks._publish_async", new_callable=AsyncMock)
    def test_publish_updates_metadata_and_status(self, mock_publish_async, mock_compute):
        mock_publish_async.return_value = ([101, 202], 303)
        post = Post.objects.create(
            channel=self.channel,
            text="Hello world",
            status=Post.Status.SCHEDULED,
        )

        result = tasks.publish_post(post.id)

        mock_publish_async.assert_awaited()
        mock_compute.assert_called_once()
        self.assertEqual(result, {"group": [101, 202], "text": 303})

        post.refresh_from_db()
        self.assertEqual(post.status, Post.Status.PUBLISHED)
        self.assertIsNotNone(post.scheduled_at)
        self.assertEqual(post.dupe_score, 0.42)

        publication = post.source_metadata.get("publication", {})
        self.assertEqual(publication.get("status"), "completed")
        self.assertEqual(publication.get("message_id"), "303")
        self.assertEqual(publication.get("group_message_ids"), [101, 202])
        self.assertTrue(publication.get("requested_at"))
        self.assertTrue(publication.get("completed_at"))
