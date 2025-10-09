from __future__ import annotations

import hashlib

from django.contrib import admin
from django.test import TestCase

from apps.posts import services
from apps.posts.admin import DraftPostAdmin
from apps.posts.models import Channel, DraftPost, Post


class RewriteMetadataTests(TestCase):
    def setUp(self) -> None:
        self.channel = Channel.objects.create(
            name="Kanał",
            slug="kanal",
            tg_channel_id="@kanal",
        )
        self.post = Post.objects.create(
            channel=self.channel,
            text="Oryginalna treść",
            status=Post.Status.DRAFT,
        )

    def test_mark_rewrite_requested_sets_pending_metadata(self) -> None:
        services.mark_rewrite_requested(self.post, prompt="Popraw proszę")
        self.post.refresh_from_db()
        rewrite = self.post.source_metadata.get("rewrite")
        assert isinstance(rewrite, dict)
        self.assertEqual(rewrite.get("status"), "pending")
        self.assertEqual(rewrite.get("prompt"), "Popraw proszę")
        self.assertTrue(rewrite.get("requested_at"))
        self.assertTrue(rewrite.get("requested_display"))
        self.assertEqual(rewrite.get("completed_at"), "")
        self.assertEqual(rewrite.get("completed_display"), "")
        expected_checksum = hashlib.sha256(self.post.text.encode("utf-8")).hexdigest()
        self.assertEqual(rewrite.get("text_checksum"), expected_checksum)

    def test_mark_rewrite_completed_updates_status_and_checksum(self) -> None:
        services.mark_rewrite_requested(self.post, prompt="Popraw proszę")
        self.post.refresh_from_db()
        self.post.text = "Tekst po korekcie"
        services.mark_rewrite_completed(self.post, auto_save=False)
        self.post.save(update_fields=["text", "source_metadata"])
        self.post.refresh_from_db()
        rewrite = self.post.source_metadata.get("rewrite")
        assert isinstance(rewrite, dict)
        self.assertEqual(rewrite.get("status"), "completed")
        self.assertTrue(rewrite.get("completed_at"))
        self.assertTrue(rewrite.get("completed_display"))
        checksum = hashlib.sha256(self.post.text.encode("utf-8")).hexdigest()
        self.assertEqual(rewrite.get("text_checksum"), checksum)

    def test_admin_serializer_returns_rewrite_state(self) -> None:
        services.mark_rewrite_requested(self.post, prompt="Popraw proszę")
        self.post.refresh_from_db()
        self.post.text = "Tekst po korekcie"
        services.mark_rewrite_completed(self.post, auto_save=False)
        self.post.save(update_fields=["text", "source_metadata"])
        self.post.refresh_from_db()
        admin_instance = DraftPostAdmin(DraftPost, admin.site)
        state = admin_instance._serialize_rewrite_state(self.post)
        self.assertEqual(state.get("status"), "completed")
        checksum = hashlib.sha256(self.post.text.encode("utf-8")).hexdigest()
        self.assertEqual(state.get("text_checksum"), checksum)
        self.assertTrue(state.get("completed_display"))
