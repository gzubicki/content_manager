from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.posts import tasks
from apps.posts.models import Channel, Post
from telegram.error import Forbidden


class PublishWithoutTokenTest(TestCase):
    @patch("apps.posts.tasks.publish_post.delay")
    def test_task_publish_due_marks_post_as_publishing(self, mock_delay):
        channel = Channel.objects.create(
            name="Test channel",
            slug="test",
            tg_channel_id="@test",
            bot_token="123",
        )
        post = Post.objects.create(
            channel=channel,
            text="Hello world",
            status=Post.Status.SCHEDULED,
            scheduled_at=timezone.now() - timezone.timedelta(minutes=5),
        )

        tasks.task_publish_due()

        post.refresh_from_db()
        self.assertEqual(post.status, Post.Status.PUBLISHING)
        mock_delay.assert_called_once_with(post.id)

        tasks.task_publish_due()
        mock_delay.assert_called_once_with(post.id)

    def test_publish_skips_when_bot_missing(self):
        channel = Channel.objects.create(
            name="Test channel",
            slug="test",
            tg_channel_id="@test",
            bot_token=" ",
        )
        post = Post.objects.create(
            channel=channel,
            text="Hello world",
            status="SCHEDULED",
            scheduled_at=timezone.now(),
        )

        with patch("apps.posts.services.Bot") as bot_cls, \
             patch("apps.posts.tasks.logger.warning") as mock_warning, \
             patch("apps.posts.tasks.services.compute_dupe") as mock_compute:
            result = tasks.publish_post(post.id)

        bot_cls.assert_not_called()
        mock_compute.assert_not_called()
        mock_warning.assert_called_once()

        post.refresh_from_db()
        self.assertEqual(post.status, "SCHEDULED")
        self.assertIsNone(post.message_id)
        self.assertIsNone(result)
        self.assertEqual(post.source_metadata.get("publication", {}).get("status"), "failed")

    def test_publish_logs_forbidden(self):
        channel = Channel.objects.create(
            name="Test channel",
            slug="test",
            tg_channel_id="@test",
            bot_token="123",
        )
        post = Post.objects.create(
            channel=channel,
            text="Hello world",
            status="SCHEDULED",
            scheduled_at=timezone.now(),
        )

        with patch("apps.posts.tasks.asyncio.run", side_effect=Forbidden("nope")) as mock_run, \
             patch("apps.posts.tasks.logger.error") as mock_error, \
             patch("apps.posts.tasks.services.compute_dupe") as mock_compute:
            result = tasks.publish_post(post.id)

        mock_run.assert_called_once()
        mock_compute.assert_not_called()
        mock_error.assert_called_once()

        post.refresh_from_db()
        self.assertEqual(post.status, "SCHEDULED")
        self.assertIsNone(post.message_id)
        self.assertIsNone(result)
        self.assertEqual(post.source_metadata.get("publication", {}).get("status"), "failed")
