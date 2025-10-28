from __future__ import annotations

import io
import json

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from apps.posts.admin import DraftImportForm
from apps.posts.models import Channel, Post


class DraftImportFormTest(TestCase):
    def setUp(self) -> None:
        self.channel = Channel.objects.create(
            name="Kanał testowy",
            slug="kanal-testowy",
            tg_channel_id="@kanal",
        )

    def _upload(self, payload: object) -> SimpleUploadedFile:
        data = json.dumps(payload).encode("utf-8")
        return SimpleUploadedFile("drafts.json", data, content_type="application/json")

    def test_requires_channel_or_default(self) -> None:
        form = DraftImportForm(data={}, files={"drafts_file": self._upload({"post": {"text": "Tekst"}})})
        self.assertFalse(form.is_valid())
        self.assertIn("kanału", " ".join(form.errors.get("__all__", [])))

    def test_accepts_default_channel(self) -> None:
        form = DraftImportForm(
            data={"default_channel": str(self.channel.pk)},
            files={"drafts_file": self._upload({"post": {"text": "Treść"}})},
        )
        self.assertTrue(form.is_valid())
        entries = form.cleaned_data["drafts_file"]
        self.assertEqual(len(entries), 1)


class DraftImportViewTest(TestCase):
    def setUp(self) -> None:
        User = get_user_model()
        self.user = User.objects.create_superuser("admin", "admin@example.com", "pass1234")
        self.channel = Channel.objects.create(name="Kanał", slug="kanal", tg_channel_id="@kanal")
        self.client.force_login(self.user)

    def _upload(self, payload: object) -> SimpleUploadedFile:
        return SimpleUploadedFile(
            "drafts.json",
            json.dumps(payload).encode("utf-8"),
            content_type="application/json",
        )

    def test_import_creates_draft(self) -> None:
        payload = {
            "drafts": [
                {
                    "channel_id": self.channel.id,
                    "post": {"text": "Nowy wpis"},
                }
            ]
        }
        response = self.client.post(
            reverse("admin:posts_draftpost_import_json"),
            {"drafts_file": self._upload(payload)},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Post.objects.filter(channel=self.channel).count(), 1)


class GenerateDraftPromptCommandTest(TestCase):
    def setUp(self) -> None:
        self.channel = Channel.objects.create(
            name="Sztuka Wojny",
            slug="sztuka-wojny",
            tg_channel_id="@sztuka",
        )

    def test_command_outputs_prompts(self) -> None:
        output = io.StringIO()
        call_command("generate_draft_prompt", str(self.channel.id), stdout=output)
        result = output.getvalue()
        self.assertIn("System prompt", result)
        self.assertIn("User prompt", result)
        self.assertIn("Wytyczne kanału", result)
