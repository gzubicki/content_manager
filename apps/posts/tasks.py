import asyncio
import logging

from celery import shared_task
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
    services.purge_cache()

@shared_task
def task_publish_due():
    now = timezone.now()
    due = Post.objects.select_related("channel").filter(status__in=("APPROVED","SCHEDULED"), scheduled_at__lte=now)
    for p in due:
        publish_post.delay(p.id)

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
    if medias:
        im = []
        opened_files = []
        media_records = []
        try:
            for m in medias:
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
                if m.type == "photo":
                    im.append(InputMediaPhoto(media=media, has_spoiler=m.has_spoiler))
                elif m.type == "video":
                    im.append(InputMediaVideo(media=media, has_spoiler=m.has_spoiler))
                elif m.type == "doc":
                    im.append(InputMediaDocument(media=media))
                else:
                    continue
                media_records.append(m)
            if im:
                res = await bot.send_media_group(chat_id=chat, media=im)
                sent_group_ids = [r.message_id for r in res]
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
    msg = await bot.send_message(chat_id=chat, text=post.text)
    return sent_group_ids, msg.message_id

@shared_task
def publish_post(post_id: int):
    post = Post.objects.select_related("channel").get(id=post_id)
    medias = list(post.media.all().order_by("order", "id"))
    coroutine = _publish_async(post, medias)
    try:
        result = asyncio.run(coroutine)
    except Forbidden as exc:
        coroutine.close()
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
        return None
    sent_group_ids, msg_id = result
    post.message_id = msg_id
    post.dupe_score = services.compute_dupe(post)
    post.status = "PUBLISHED"
    post.save()
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
