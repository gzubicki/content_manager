from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.posts.models import Channel, Post
from apps.posts.tasks import task_housekeeping


class HousekeepingTaskTest(TestCase):
    def setUp(self):
        self.channel = Channel.objects.create(
            name="Kanał testowy",
            slug="kana-test",
            tg_channel_id="@kana_test",
        )

    @override_settings(PUBLISHED_POST_TTL_DAYS=7)
    @patch("apps.posts.tasks.services.purge_cache")
    def test_removes_published_posts_after_ttl(self, purge_cache_mock):
        now = timezone.now()
        stale_post = Post.objects.create(
            channel=self.channel,
            text="stary",
            status=Post.Status.PUBLISHED,
            published_at=now - timezone.timedelta(days=8),
        )
        recent_post = Post.objects.create(
            channel=self.channel,
            text="świeży",
            status=Post.Status.PUBLISHED,
            published_at=now - timezone.timedelta(days=2),
        )
        # ensure drafts still cleaned via expires_at
        expired_draft = Post.objects.create(
            channel=self.channel,
            text="draft",
            status=Post.Status.DRAFT,
            expires_at=now - timezone.timedelta(days=1),
        )

        task_housekeeping()

        purge_cache_mock.assert_called_once()
        self.assertFalse(Post.objects.filter(id=stale_post.id).exists())
        self.assertTrue(Post.objects.filter(id=recent_post.id).exists())
        self.assertFalse(Post.objects.filter(id=expired_draft.id).exists())
