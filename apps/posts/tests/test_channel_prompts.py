import json
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.posts import services
from apps.posts.models import Channel, ChannelSource, Post


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
        self.assertIn("Jeśli media pochodzą z artykułu lub innego źródła", system_prompt)
        self.assertNotIn("Nie podawaj bezpośrednich linków", system_prompt)
        self.assertNotIn("poleceniach kanału", system_prompt)
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
        self.assertNotIn("Unikaj powtarzania tematów", second_system_prompt)
        self.assertIn("Powtarzalny wpis o dronach", second_system_prompt)

    def test_channel_sources_are_listed_in_prompt(self):
        primary = ChannelSource.objects.create(
            channel=self.channel,
            name="ISW",
            url="https://www.understandingwar.org/",
            priority=5,
        )
        secondary = ChannelSource.objects.create(
            channel=self.channel,
            name="DeepState",
            url="https://deepstate.com/",
            priority=1,
        )

        with patch("apps.posts.services._select_channel_sources") as mock_select, patch(
            "apps.posts.services.gpt_generate_text"
        ) as mock_gpt:
            mock_select.return_value = [primary]
            mock_gpt.return_value = json.dumps({"post": {"text": "tekst"}, "media": []})
            services.gpt_generate_post_payload(self.channel)

        mock_select.assert_called_with(self.channel, limit=1)

        system_prompt, user_prompt = mock_gpt.call_args[0][:2]
        self.assertNotIn(primary.url, system_prompt)
        self.assertNotIn(secondary.url, system_prompt)
        self.assertIn(primary.url, user_prompt)
        self.assertNotIn(secondary.url, user_prompt)
        self.assertNotIn("Preferuj", user_prompt)
        self.assertNotIn("źródło:", user_prompt)
        self.assertNotIn("priorytet", user_prompt)

    def test_recent_headlines_are_included_for_last_24_hours(self):
        Post.objects.create(
            channel=self.channel,
            text="Nagłówek A\nDalsza część wpisu",
            status=Post.Status.PUBLISHED,
            scheduled_at=timezone.now() - timedelta(hours=1),
        )
        Post.objects.create(
            channel=self.channel,
            text="Drugi post bez entera",
            status=Post.Status.DRAFT,
        )
        old_post = Post.objects.create(
            channel=self.channel,
            text="Stary nagłówek\nNie powinien się pojawić",
            status=Post.Status.PUBLISHED,
            scheduled_at=timezone.now() - timedelta(days=3),
        )
        Post.objects.filter(pk=old_post.pk).update(
            created_at=timezone.now() - timedelta(days=3)
        )

        with patch("apps.posts.services.gpt_generate_text") as mock_gpt:
            mock_gpt.return_value = json.dumps({"post": {"text": "tekst"}, "media": []})
            services.gpt_generate_post_payload(self.channel)

        system_prompt, _ = mock_gpt.call_args[0][:2]
        self.assertIn("nie powielaj tematów:", system_prompt)
        self.assertIn("Nagłówek A", system_prompt)
        self.assertIn("Drugi post bez entera", system_prompt)
        self.assertNotIn("Stary nagłówek", system_prompt)

    def test_recent_headlines_list_includes_up_to_40_entries(self):
        for idx in range(41):
            Post.objects.create(
                channel=self.channel,
                text=f"Nagłówek {idx}\nDalsza część wpisu",
                status=Post.Status.PUBLISHED,
                scheduled_at=timezone.now() - timedelta(minutes=idx),
            )

        with patch("apps.posts.services.gpt_generate_text") as mock_gpt:
            mock_gpt.return_value = json.dumps({"post": {"text": "tekst"}, "media": []})
            services.gpt_generate_post_payload(self.channel)

        system_prompt, _ = mock_gpt.call_args[0][:2]
        prefixes = tuple(f"{n}. " for n in range(1, 45))
        enumerated_lines = [
            line for line in system_prompt.splitlines() if line.strip().startswith(prefixes)
        ]
        self.assertEqual(40, len(enumerated_lines))
        self.assertIn("Nagłówek 40", system_prompt)
        self.assertNotIn("Nagłówek 0", system_prompt)

    def test_article_headlines_are_sanitized_in_prompt(self):
        article = {
            "headlines": ["🔥 Pilne! Alarm!!!", "Drugi @nagłówek"],
        }

        with patch("apps.posts.services.gpt_generate_text") as mock_gpt:
            mock_gpt.return_value = json.dumps({"post": {"text": "tekst"}, "media": []})
            services.gpt_generate_post_payload(self.channel, article=article)

        system_prompt, _ = mock_gpt.call_args[0][:2]
        self.assertIn("nie powielaj tematów:", system_prompt)
        self.assertIn("1. Pilne Alarm", system_prompt)
        self.assertIn("2. Drugi nagłówek", system_prompt)
        self.assertNotIn("🔥", system_prompt)
        self.assertNotIn("@", system_prompt)

    def test_topics_to_avoid_do_not_duplicate_recent_headlines(self):
        Post.objects.create(
            channel=self.channel,
            text="Powielony nagłówek\nDalszy opis wpisu",
            status=Post.Status.PUBLISHED,
            scheduled_at=timezone.now() - timedelta(minutes=5),
        )

        article = {
            "headlines": ["Powielony nagłówek", "Dodatkowy temat"],
        }

        with patch("apps.posts.services.gpt_generate_text") as mock_gpt:
            mock_gpt.return_value = json.dumps({"post": {"text": "tekst"}, "media": []})
            services.gpt_generate_post_payload(self.channel, article=article)

        system_prompt, _ = mock_gpt.call_args[0][:2]
        duplicate_lines = [
            line for line in system_prompt.splitlines() if "Powielony nagłówek" in line
        ]
        self.assertEqual(1, len(duplicate_lines))
        self.assertIn("Dodatkowy temat", system_prompt)
