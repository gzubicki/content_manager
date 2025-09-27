from django.test import TestCase
from django.utils import timezone

from apps.posts.models import Channel, Post


class PostStatusTransitionsTest(TestCase):
    def setUp(self):
        self.channel = Channel.objects.create(
            name="Kanał",
            slug="kanal",
            tg_channel_id="123",
        )

    def test_approved_post_with_schedule_becomes_scheduled(self):
        post = Post.objects.create(
            channel=self.channel,
            text="Treść",
            status=Post.Status.APPROVED,
            scheduled_at=timezone.now(),
        )

        post.refresh_from_db()
        self.assertEqual(post.status, Post.Status.SCHEDULED)

    def test_approved_post_without_schedule_stays_approved(self):
        post = Post.objects.create(
            channel=self.channel,
            text="Treść",
            status=Post.Status.APPROVED,
        )

        post.refresh_from_db()
        self.assertEqual(post.status, Post.Status.APPROVED)
