import asyncio
import json
import logging
import os
import shutil
import subprocess
from typing import Any

from celery import shared_task
from django.db import transaction
from django.utils import timezone
from telegram import InputMediaDocument, InputMediaPhoto, InputMediaVideo
from telegram.error import Forbidden

from .models import Post, Channel
from . import services
from .drafts import iter_missing_draft_requirements
from openai import RateLimitError, APIError, APIConnectionError, APITimeoutError


logger = logging.getLogger(__name__)


def _coerce_positive_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            coerced = int(round(float(value)))
        else:
            coerced = int(round(float(str(value).strip())))
    except (TypeError, ValueError):
        return None
    if coerced <= 0:
        return None
    return coerced


def _video_metadata_from_reference(reference: Any) -> tuple[dict[str, int], dict[str, bool]]:
    metadata: dict[str, int] = {}
    stored_flags: dict[str, bool] = {}
    if not isinstance(reference, dict):
        return metadata, stored_flags

    alias_map = {
        "width": ("width", "w", "video_width"),
        "height": ("height", "h", "video_height"),
        "duration": ("duration", "length", "video_duration"),
    }

    candidates: list[tuple[str, dict[str, Any]]] = [("root", reference)]
    for key in ("video_metadata", "video", "meta", "metadata", "properties"):
        value = reference.get(key)
        if isinstance(value, dict):
            candidates.append((key, value))

    for source_name, candidate in candidates:
        for target_key, candidate_keys in alias_map.items():
            if target_key in metadata:
                continue
            for candidate_key in candidate_keys:
                coerced = _coerce_positive_int(candidate.get(candidate_key))
                if coerced:
                    metadata[target_key] = coerced
                    stored_flags[target_key] = source_name == "video_metadata"
                    break

    return metadata, stored_flags


def _probe_video_metadata(path: str) -> dict[str, int]:
    if not path:
        return {}
    ffprobe_bin = shutil.which("ffprobe")
    if not ffprobe_bin:
        return {}
    try:
        completed = subprocess.run(
            [
                ffprobe_bin,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height,duration",
                "-of",
                "json",
                path,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return {}

    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        return {}

    streams = payload.get("streams") or []
    if not streams:
        return {}
    stream_info = streams[0] or {}

    metadata: dict[str, int] = {}
    for key in ("width", "height", "duration"):
        coerced = _coerce_positive_int(stream_info.get(key))
        if coerced:
            metadata[key] = coerced
    return metadata


def _persist_video_metadata(pm, metadata: dict[str, int], stored_flags: dict[str, bool]) -> bool:
    if not metadata:
        return False
    reference = pm.reference_data if isinstance(pm.reference_data, dict) else {}
    existing_video_meta = {}
    if isinstance(reference.get("video_metadata"), dict):
        existing_video_meta = dict(reference["video_metadata"])

    changed = False
    new_video_meta = dict(existing_video_meta)
    for key in ("width", "height", "duration"):
        value = metadata.get(key)
        if not value:
            continue
        already_stored = stored_flags.get(key, False) and existing_video_meta.get(key) == value
        if already_stored:
            continue
        if new_video_meta.get(key) != value:
            new_video_meta[key] = value
            changed = True

    if not changed:
        return False

    updated_reference = dict(reference)
    updated_reference["video_metadata"] = new_video_meta
    pm.reference_data = updated_reference
    return True


def _video_metadata_for_media(pm) -> tuple[dict[str, int], bool]:
    metadata, stored_flags = _video_metadata_from_reference(pm.reference_data)

    needs_probe = False
    for key in ("width", "height"):
        if not metadata.get(key):
            needs_probe = True
            break
    if not metadata.get("duration"):
        needs_probe = True

    if needs_probe and pm.cache_path and os.path.exists(pm.cache_path):
        probed = _probe_video_metadata(pm.cache_path)
        for key in ("width", "height", "duration"):
            value = probed.get(key)
            if value and metadata.get(key) != value:
                metadata[key] = value
                stored_flags[key] = False

    changed = _persist_video_metadata(pm, metadata, stored_flags)
    return metadata, changed


@shared_task
def task_ensure_min_drafts():
    queued = 0
    affected = 0
    for channel_id, need in iter_missing_draft_requirements():
        task_gpt_generate_for_channel.delay(channel_id, need)
        queued += need
        affected += 1
    if affected:
        logger.info(
            "Queued %s GPT draft(s) for %s channel(s) via ensure_min_drafts.",
            queued,
            affected,
        )
    return {"queued": queued, "channels": affected}

@shared_task
def task_housekeeping():
    Post.objects.filter(status="DRAFT", expires_at__lt=timezone.now()).delete()
    services.purge_cache()

@shared_task
def task_publish_due():
    now = timezone.now()
    with transaction.atomic():
        connection = transaction.get_connection()
        queryset = Post.objects.filter(
            status__in=(Post.Status.APPROVED, Post.Status.SCHEDULED),
            scheduled_at__lte=now,
        )
        if connection.features.has_select_for_update:
            select_kwargs = {}
            if getattr(connection.features, "has_select_for_update_skip_locked", False):
                select_kwargs["skip_locked"] = True
            queryset = queryset.select_for_update(**select_kwargs)
        due_ids = list(queryset.values_list("id", flat=True))
        if due_ids:
            Post.objects.filter(id__in=due_ids).update(status=Post.Status.PUBLISHING)
    for post_id in due_ids:
        publish_post.delay(post_id)

async def _publish_async(post: Post, medias):
    bot = services._bot_for(post.channel)
    if bot is None:
        logger.warning(
            "Cannot publish for channel %s (%s): missing bot token. Configure Channel.bot_token to enable publishing.",
            post.channel_id,
            getattr(post.channel, "slug", None),
        )
        return None
    chat = post.channel.tg_channel_id
    sent_group_ids = []
    text_message_id = None
    post_text = post.text or ""
    text_has_content = bool(post_text.strip())
    caption_text = ""
    if text_has_content and len(post_text) <= 1024:
        caption_text = post_text
    send_text_separately = text_has_content and not caption_text
    if medias:
        im = []
        opened_files = []
        media_records = []
        try:
            for index, m in enumerate(medias):
                media = m.tg_file_id
                if not media:
                    cache_path = m.cache_path
                    if not cache_path:
                        cache_path = await asyncio.to_thread(services.cache_media, m)
                    if cache_path:
                        media = open(cache_path, "rb")
                        opened_files.append(media)
                if not media:
                    continue
                caption_kwargs = {}
                if index == 0 and caption_text:
                    caption_kwargs["caption"] = caption_text
                if m.type == "photo":
                    im.append(
                        InputMediaPhoto(
                            media=media,
                            has_spoiler=m.has_spoiler,
                            **caption_kwargs,
                        )
                    )
                elif m.type == "video":
                    video_kwargs: dict[str, Any] = {}
                    metadata, metadata_changed = _video_metadata_for_media(m)
                    for key in ("width", "height", "duration"):
                        value = metadata.get(key)
                        if value:
                            video_kwargs[key] = value
                    video_kwargs["supports_streaming"] = True
                    im.append(
                        InputMediaVideo(
                            media=media,
                            has_spoiler=m.has_spoiler,
                            **video_kwargs,
                            **caption_kwargs,
                        )
                    )
                    if metadata_changed:
                        setattr(m, "_reference_data_dirty", True)
                elif m.type == "doc":
                    im.append(InputMediaDocument(media=media, **caption_kwargs))
                else:
                    continue
                media_records.append(m)
            if im:
                res = await bot.send_media_group(chat_id=chat, media=im)
                sent_group_ids = [r.message_id for r in res]
                if caption_text and sent_group_ids:
                    text_message_id = sent_group_ids[0]
                for record, message in zip(media_records, res):
                    file_id = None
                    if record.type == "photo" and getattr(message, "photo", None):
                        file_id = message.photo[-1].file_id
                    elif record.type == "video" and getattr(message, "video", None):
                        file_id = message.video.file_id
                    elif record.type == "doc" and getattr(message, "document", None):
                        file_id = message.document.file_id
                    if file_id and record.tg_file_id != file_id:
                        record.tg_file_id = file_id
                        await asyncio.to_thread(record.save, update_fields=["tg_file_id"])
        finally:
            for fh in opened_files:
                try:
                    fh.close()
                except Exception:
                    pass
    if not medias or send_text_separately:
        msg = await bot.send_message(chat_id=chat, text=post_text)
        text_message_id = msg.message_id
    return sent_group_ids, text_message_id

def _restore_status_after_failure(post: Post) -> None:
    if post.scheduled_at:
        target = Post.Status.SCHEDULED
    else:
        target = Post.Status.APPROVED
    if post.status != target:
        post.status = target
        post.save(update_fields=["status"])


@shared_task
def publish_post(post_id: int):
    post = Post.objects.select_related("channel").get(id=post_id)
    if post.status == Post.Status.PUBLISHED:
        logger.info("Post %s already published, skipping.", post_id)
        return None
    if post.status not in {
        Post.Status.APPROVED,
        Post.Status.SCHEDULED,
        Post.Status.PUBLISHING,
    }:
        logger.info("Post %s has status %s, skipping publish.", post_id, post.status)
        return None
    if post.status != Post.Status.PUBLISHING:
        post.status = Post.Status.PUBLISHING
        post.save(update_fields=["status"])
    medias = list(post.media.all().order_by("order", "id"))
    services.mark_publication_requested(post, auto_save=True)
    coroutine = _publish_async(post, medias)
    try:
        result = asyncio.run(coroutine)
    except Forbidden as exc:
        coroutine.close()
        _restore_status_after_failure(post)
        services.mark_publication_failed(post, reason="forbidden")
        logger.error(
            (
                "Cannot publish post %s to channel %s (%s): %s. "
                "Add the bot to the channel and grant permission to post."
            ),
            post.id,
            post.channel_id,
            getattr(post.channel, "slug", None) or post.channel.tg_channel_id,
            exc,
            exc_info=exc,
        )
        return None
    dirty_medias = [m for m in medias if getattr(m, "_reference_data_dirty", False)]
    for media in dirty_medias:
        media.save(update_fields=["reference_data"])
        setattr(media, "_reference_data_dirty", False)
    if result is None:
        _restore_status_after_failure(post)
        services.mark_publication_failed(post, reason="missing_bot")
        return None
    sent_group_ids, msg_id = result
    if msg_id is None and sent_group_ids:
        msg_id = sent_group_ids[0]
    now = timezone.now()
    if post.scheduled_at is None:
        post.scheduled_at = now
    post.message_id = msg_id
    post.dupe_score = services.compute_dupe(post)
    post.status = "PUBLISHED"
    post.save()
    services.mark_publication_completed(
        post,
        message_id=msg_id,
        group_message_ids=sent_group_ids,
        auto_save=True,
    )
    return {"group": sent_group_ids, "text": msg_id}

@shared_task(bind=True,
             autoretry_for=(APIError, APIConnectionError, APITimeoutError, RateLimitError),
             retry_backoff=True, retry_jitter=True,
             retry_kwargs={"max_retries": 6})
def task_gpt_generate_for_channel(self, channel_id: int, count: int = 1):
    ch = Channel.objects.get(id=channel_id)
    added = 0
    for _ in range(count):
        payload = services.gpt_new_draft(ch)
        if payload is None:
            # np. insufficient_quota – przerwij grzecznie, bez wyjątku
            break
        services.create_post_from_payload(ch, payload)
        added += 1
    return added

@shared_task(bind=True,
             autoretry_for=(APIError, APIConnectionError, APITimeoutError, RateLimitError),
             retry_backoff=True, retry_jitter=True,
             retry_kwargs={"max_retries": 6})
def task_gpt_generate_from_article(self, channel_id: int, article: dict[str, Any] | None = None):
    ch = Channel.objects.get(id=channel_id)
    payload = services.gpt_generate_post_payload(ch, article=article)
    if payload is None:
        return 0
    try:
        services.create_post_from_payload(ch, payload)
    except ValueError:
        logger.exception(
            "GPT article payload dla kanału %s nie zawiera wymaganych danych",
            channel_id,
        )
        return 0
    return 1

@shared_task
def task_gpt_rewrite_post(post_id: int, editor_prompt: str):
    p = Post.objects.select_related("channel").get(id=post_id)
    new_text = services.gpt_rewrite_text(p.channel, p.text, editor_prompt)
    p.text = new_text
    services.mark_rewrite_completed(p, auto_save=False)
    p.save(update_fields=["text", "source_metadata"])
    return p.id

@shared_task(bind=True, rate_limit="1/s",
             autoretry_for=(APIError, APIConnectionError, APITimeoutError, RateLimitError),
             retry_backoff=True, retry_jitter=True, retry_kwargs={"max_retries": 5})
def task_gpt_generate_one(self, channel_id: int):
    ch = Channel.objects.get(id=channel_id)
    payload = services.gpt_new_draft(ch)
    if payload is None:  # np. brak środków
        return 0
    services.create_post_from_payload(ch, payload)
    return 1
