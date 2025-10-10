import asyncio
import logging
from typing import Any

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from telegram import InputMediaPhoto, InputMediaVideo, InputMediaDocument
from telegram.error import Forbidden

from .models import Post, Channel
from . import services
from .drafts import iter_missing_draft_requirements
from openai import RateLimitError, APIError, APIConnectionError, APITimeoutError


logger = logging.getLogger(__name__)


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
    ttl_days = int(getattr(settings, "PUBLISHED_POST_TTL_DAYS", 0) or 0)
    if ttl_days > 0:
        cutoff = timezone.now() - timezone.timedelta(days=ttl_days)
        Post.objects.filter(
            status=Post.Status.PUBLISHED,
            published_at__lt=cutoff,
        ).delete()
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
                    im.append(
                        InputMediaVideo(
                            media=media,
                            has_spoiler=m.has_spoiler,
                            **caption_kwargs,
                        )
                    )
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
    post.published_at = now
    post.save(update_fields=[
        "status",
        "scheduled_at",
        "dupe_score",
        "message_id",
        "published_at",
    ])
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
