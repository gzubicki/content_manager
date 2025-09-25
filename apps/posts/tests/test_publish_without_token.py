from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.posts import tasks
from apps.posts.models import Channel, Post


class PublishWithoutTokenTest(TestCase):
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
