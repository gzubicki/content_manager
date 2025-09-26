from __future__ import annotations

from collections.abc import Iterable, Iterator

from django.db.models import Count, Q

from .models import Channel, Post


def iter_missing_draft_requirements(
    channels: Iterable[Channel] | None = None,
) -> Iterator[tuple[int, int]]:
    """Yield ``(channel_id, missing_count)`` for channels below draft targets."""

    channel_ids: set[int] | None = None
    if channels is not None:
        channel_ids = {ch.id for ch in channels if getattr(ch, "id", None)}
        if not channel_ids:
            return

    queryset = Channel.objects.all()
    if channel_ids is not None:
        queryset = queryset.filter(id__in=channel_ids)

    annotated = (
        queryset.annotate(
            draft_count=Count(
                "posts",
                filter=Q(posts__status=Post.Status.DRAFT),
            )
        )
        .values("id", "draft_target_count", "draft_count")
    )

    for entry in annotated:
        current = entry.get("draft_count") or 0
        target = entry.get("draft_target_count") or 0
        missing = max(target - current, 0)
        if missing:
            yield entry["id"], missing
