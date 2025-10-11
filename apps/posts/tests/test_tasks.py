from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase

from apps.posts.models import Channel, Post
from apps.posts.tasks import task_gpt_generate_for_channel


class GenerateDraftsTaskTest(TestCase):
    def setUp(self) -> None:
        self.channel = Channel.objects.create(
            name="Kanał testowy",
            slug="kanal-test",
            tg_channel_id="@kanal_test",
            draft_target_count=20,
        )

        # wypełnijmy połowę celu, aby sprawdzić ograniczenie do brakujących slotów
        for idx in range(10):
            Post.objects.create(
                channel=self.channel,
                text=f"Istniejący draft {idx}",
                status=Post.Status.DRAFT,
            )

    @patch("apps.posts.tasks.services.gpt_new_draft")
    def test_limits_generation_to_remaining_target(self, gpt_new_draft_mock):
        def _fake_payload(*_, **__):
            counter = gpt_new_draft_mock.call_count
            return {"post": {"text": f"Nowy draft {counter}"}}

        gpt_new_draft_mock.side_effect = _fake_payload

        created = task_gpt_generate_for_channel.run(self.channel.id, 50)

        self.assertEqual(created, 10)
        self.assertEqual(
            Post.objects.filter(channel=self.channel, status=Post.Status.DRAFT).count(),
            self.channel.draft_target_count,
        )

