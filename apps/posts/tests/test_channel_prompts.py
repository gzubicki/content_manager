import json
from unittest.mock import patch

from django.test import TestCase

from apps.posts import services
from apps.posts.models import Channel


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
        self.assertIn("Wytyczne kanału", system_prompt)
        self.assertIn("maksymalnie 321 znaków", system_prompt)
        self.assertIn("Liczba emoji", system_prompt)
        self.assertIn("linia1", system_prompt)
        self.assertIn("linia2", system_prompt)
        self.assertIn("Nie dodawaj linków w treści.", system_prompt)

        self.assertIn("limicie znaków", user_prompt)
        self.assertIn("resolver (np. twitter/telegram/instagram/rss)", user_prompt)
        self.assertIn("reference – obiekt z prawdziwymi identyfikatorami", user_prompt)
        self.assertIn("reference.source_locator", user_prompt)
        self.assertIn("angielskich nazw pól", user_prompt)
        self.assertIn("Nie podawaj bezpośrednich linków", user_prompt)
        self.assertNotIn("linia1", user_prompt)
        self.assertNotIn("linia2", user_prompt)
        self.assertNotIn("Nie dodawaj linków", user_prompt)

    def test_rewrite_text_uses_same_channel_rules(self):
        with patch("apps.posts.services.gpt_generate_text") as mock_gpt:
            services.gpt_rewrite_text(self.channel, "oryginalny tekst", "edytor")

        system_prompt, user_prompt = mock_gpt.call_args[0][:2]
        self.assertIn("Wytyczne kanału", system_prompt)
        self.assertIn("maksymalnie 321 znaków", system_prompt)
        self.assertIn("linia1", system_prompt)
        self.assertIn("linia2", system_prompt)
        self.assertIn("Nie dodawaj linków", system_prompt)

        self.assertIn("Zachowaj charakter kanału", user_prompt)
        self.assertIn("długości, emoji oraz stopki", user_prompt)
        self.assertNotIn("linia1", user_prompt)
        self.assertNotIn("linia2", user_prompt)
        self.assertNotIn("Nie dodawaj linków", user_prompt)
