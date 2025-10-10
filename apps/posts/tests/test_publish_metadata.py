from unittest.mock import AsyncMock, patch

from django.test import TestCase

from apps.posts import tasks
from apps.posts.models import Channel, Post


class PublishMetadataTest(TestCase):
    def setUp(self):
        self.channel = Channel.objects.create(
            name="Test channel",
            slug="test-metadata",
            tg_channel_id="@test_metadata",
            bot_token="token",
        )

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
        self.assertIsNotNone(post.published_at)
        self.assertEqual(post.dupe_score, 0.42)

        publication = post.source_metadata.get("publication", {})
        self.assertEqual(publication.get("status"), "completed")
        self.assertEqual(publication.get("message_id"), "303")
        self.assertEqual(publication.get("group_message_ids"), [101, 202])
        self.assertTrue(publication.get("requested_at"))
        self.assertTrue(publication.get("completed_at"))
