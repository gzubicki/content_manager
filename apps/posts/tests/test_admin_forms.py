from __future__ import annotations

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from apps.posts.admin import PostForm, RescheduleForm, PostMediaInlineForm
from apps.posts.models import Channel, Post


class PostFormTests(TestCase):
    def setUp(self) -> None:
        self.channel = Channel.objects.create(
            name="Kanał",
            slug="kanal",
            tg_channel_id="@kanal",
            max_chars=10,
        )
        self.post = Post.objects.create(
            channel=self.channel,
            text="ok",
            status=Post.Status.DRAFT,
            schedule_mode="AUTO",
        )

    def test_text_longer_than_limit_is_invalid(self) -> None:
        long_text = "x" * 50
        self.post.text = long_text
        form = PostForm(
            data={
                "channel": self.channel.pk,
                "text": long_text,
                "source_url": "",
                "status": Post.Status.DRAFT,
                "schedule_mode": "AUTO",
            },
            instance=self.post,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("Za długie", " ".join(form.non_field_errors()))

    def test_valid_text_passes_validation(self) -> None:
        form = PostForm(
            data={
                "channel": self.channel.pk,
                "text": "krótki",
                "source_url": "",
                "status": Post.Status.DRAFT,
                "schedule_mode": "AUTO",
            },
            instance=self.post,
        )
        self.assertTrue(form.is_valid())

    def test_source_url_can_be_set_and_is_stripped(self) -> None:
        form = PostForm(
            data={
                "channel": self.channel.pk,
                "text": "krótki",
                "source_url": "  https://example.com/artykul  ",
                "status": Post.Status.DRAFT,
                "schedule_mode": "AUTO",
            },
            instance=self.post,
        )
        self.assertTrue(form.is_valid())
        saved = form.save()
        self.assertEqual(saved.source_url, "https://example.com/artykul")


class RescheduleFormTests(TestCase):
    def test_manual_mode_requires_datetime(self) -> None:
        form = RescheduleForm(
            data={
                "schedule_mode": "MANUAL",
                "scheduled_at_0": "",
                "scheduled_at_1": "",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("scheduled_at", form.errors)

    def test_manual_mode_with_datetime_is_valid(self) -> None:
        form = RescheduleForm(
            data={
                "schedule_mode": "MANUAL",
                "scheduled_at_0": "2025-01-01",
                "scheduled_at_1": "12:30:00",
            }
        )
        self.assertTrue(form.is_valid())

    def test_auto_mode_allows_empty_datetime(self) -> None:
        form = RescheduleForm(data={"schedule_mode": "AUTO"})
        self.assertTrue(form.is_valid())


class PostMediaInlineFormTests(TestCase):
    def setUp(self) -> None:
        self.channel = Channel.objects.create(
            name="Kanał",
            slug="kanal",
            tg_channel_id="@kanal",
        )
        self.post = Post.objects.create(
            channel=self.channel,
            text="Lorem",
            status=Post.Status.DRAFT,
        )

    def test_requires_upload_or_url(self) -> None:
        form = PostMediaInlineForm(
            data={
                "order": "5",
                "has_spoiler": "",
                "source_url": "   ",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("__all__", form.errors)

    def test_sets_type_to_photo_for_upload(self) -> None:
        upload = SimpleUploadedFile("image.jpg", b"content", content_type="image/jpeg")
        form = PostMediaInlineForm(
            data={
                "order": "0",
                "has_spoiler": "",
                "source_url": "",
            },
            files={"upload": upload},
        )
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data["type"], "photo")

    def test_sets_type_to_video_for_url(self) -> None:
        form = PostMediaInlineForm(
            data={
                "order": "0",
                "has_spoiler": "",
                "source_url": "https://cdn.example/video.mp4",
            }
        )
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data["type"], "video")
