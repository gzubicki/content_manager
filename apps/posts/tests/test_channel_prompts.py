import json
from unittest.mock import patch

from django.test import TestCase

from apps.posts import services
from apps.posts.models import Channel, Post


class ChannelPromptPropagationTest(TestCase):
    def setUp(self):
        self.channel = Channel.objects.create(
            name="Kanał testowy",
            slug="kanał-testowy",
            tg_channel_id="@kanal",
            language="pl",
            max_chars=321,
            emoji_min=2,
            emoji_max=4,
            footer_text="linia1\nlinia2",
            no_links_in_text=True,
            style_prompt="Dostosuj ton do kanału.",
        )

    def test_generate_payload_adds_channel_rules_to_prompts(self):
        with patch("apps.posts.services.gpt_generate_text") as mock_gpt:
            mock_gpt.return_value = json.dumps({"post": {"text": "tekst"}, "media": []})
            services.gpt_generate_post_payload(self.channel)

        system_prompt, user_prompt = mock_gpt.call_args[0][:2]
        self.assertIn("Zwróć dokładnie jeden obiekt JSON", system_prompt)
        self.assertIn("resolver (np. twitter/telegram/instagram/rss)", system_prompt)
        self.assertIn("reference – obiekt z prawdziwymi identyfikatorami", system_prompt)
        self.assertIn("reference.source_locator", system_prompt)
        self.assertIn("angielskich nazw pól", system_prompt)
        self.assertIn("Treść posta oraz wszystkie media muszą opisywać to samo wydarzenie", system_prompt)
        self.assertIn("Jeśli korzystasz z wpisów Telegram", system_prompt)
        self.assertIn("Nie podawaj bezpośrednich linków", system_prompt)
        self.assertIn("poleceniach kanału", system_prompt)
        self.assertNotIn("linia1", system_prompt)
        self.assertNotIn("linia2", system_prompt)

        self.assertIn("Wytyczne kanału", user_prompt)
        self.assertIn("maksymalnie 321 znaków", user_prompt)
        self.assertIn("Liczba emoji", user_prompt)
        self.assertIn("linia1", user_prompt)
        self.assertIn("linia2", user_prompt)
        self.assertIn("Nie dodawaj linków w treści.", user_prompt)

    def test_rewrite_text_uses_same_channel_rules(self):
        with patch("apps.posts.services.gpt_generate_text") as mock_gpt:
            services.gpt_rewrite_text(self.channel, "oryginalny tekst", "edytor")

        system_prompt, user_prompt = mock_gpt.call_args[0][:2]
        self.assertIn("Przepisz poniższy tekst", system_prompt)
        self.assertIn("poleceniach kanału", system_prompt)
        self.assertNotIn("linia1", system_prompt)
        self.assertNotIn("linia2", system_prompt)

        self.assertIn("Wytyczne kanału", user_prompt)
        self.assertIn("maksymalnie 321 znaków", user_prompt)
        self.assertIn("linia1", user_prompt)
        self.assertIn("linia2", user_prompt)
        self.assertIn("Nie dodawaj linków", user_prompt)

    def test_duplicate_detection_triggers_retry_with_additional_context(self):
        Post.objects.create(
            channel=self.channel,
            text="Powtarzalny wpis o dronach",
            status=Post.Status.PUBLISHED,
        )

        responses = [
            json.dumps({"post": {"text": "Powtarzalny wpis o dronach"}, "media": []}),
            json.dumps({"post": {"text": "Nowy raport o sytuacji"}, "media": []}),
        ]

        def _side_effect(*args, **kwargs):
            return responses.pop(0)

        with patch("apps.posts.services.gpt_generate_text", side_effect=_side_effect) as mock_gpt:
            payload = services.gpt_generate_post_payload(self.channel)

        self.assertEqual(payload["post"]["text"], "Nowy raport o sytuacji")
        self.assertEqual(mock_gpt.call_count, 2)

        second_system_prompt = mock_gpt.call_args_list[1][0][0]
        self.assertIn("Unikaj powtarzania poniższych tekstów", second_system_prompt)
        self.assertIn("Powtarzalny wpis o dronach", second_system_prompt)
