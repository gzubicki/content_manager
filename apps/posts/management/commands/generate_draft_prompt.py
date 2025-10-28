from __future__ import annotations

import json
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from apps.posts import services
from apps.posts.models import Channel


class Command(BaseCommand):
    help = "Wyświetla prompt do generowania draftów GPT dla wskazanego kanału."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "channel",
            help="ID lub slug kanału.",
        )
        parser.add_argument(
            "--no-headlines",
            action="store_true",
            help="Nie dołączaj ostatnich nagłówków do promptu.",
        )
        parser.add_argument(
            "--article",
            dest="article",
            help="Opcjonalny obiekt JSON z danymi artykułu przekazywanymi do GPT.",
        )
        parser.add_argument(
            "--avoid",
            dest="avoid",
            action="append",
            default=[],
            help="Teksty do listy avoid_texts (można podać wielokrotnie).",
        )

    def handle(self, *args, **options) -> None:
        channel_ref = options["channel"]
        channel = self._resolve_channel(channel_ref)

        article_payload = self._parse_article(options.get("article"))
        avoid_texts = options.get("avoid") or []
        include_headlines = not options.get("no_headlines")

        prompts = services.build_draft_generation_prompt(
            channel,
            article=article_payload,
            avoid_texts=avoid_texts,
            include_recent_headlines=include_headlines,
        )

        self.stdout.write(self.style.SUCCESS("== System prompt =="))
        self.stdout.write(prompts["system"])
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("== User prompt =="))
        self.stdout.write(prompts["user"])

    def _resolve_channel(self, reference: str) -> Channel:
        try:
            channel_id = int(reference)
        except (TypeError, ValueError):
            channel = Channel.objects.filter(slug=reference).first()
            if channel is None:
                raise CommandError(f"Nie znaleziono kanału o slugu '{reference}'.")
            return channel

        try:
            return Channel.objects.get(id=channel_id)
        except Channel.DoesNotExist as exc:
            raise CommandError(f"Nie znaleziono kanału o ID {reference}.") from exc

    def _parse_article(self, raw: str | None) -> dict[str, Any] | None:
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CommandError(f"Niepoprawny JSON w parametrze --article: {exc}") from exc
        if not isinstance(parsed, dict):
            raise CommandError("Parametr --article musi być obiektem JSON (mapą).")
        return parsed
