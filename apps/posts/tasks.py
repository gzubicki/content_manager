from celery import shared_task
from django.utils import timezone
from telegram import InputMediaPhoto, InputMediaVideo
from .models import Post, Channel
from . import services
import asyncio

@shared_task
def task_ensure_min_drafts():
    for ch in Channel.objects.all():
        services.ensure_min_drafts(ch)

@shared_task
def task_housekeeping():
    from .models import PostMedia
    Post.objects.filter(status="DRAFT", expires_at__lt=timezone.now()).delete()
    services.purge_cache()

@shared_task
def task_publish_due():
    now = timezone.now()
    due = Post.objects.select_related("channel").filter(status__in=("APPROVED","SCHEDULED"), scheduled_at__lte=now)
    for p in due:
        publish_post.delay(p.id)

async def _publish_async(post: Post):
    bot = services._bot_for(post.channel)
    chat = post.channel.tg_channel_id
    medias = list(post.media.all())
    sent_group_ids = []
    if medias:
        im = []
        for m in medias:
            media = m.tg_file_id or (open(m.cache_path, "rb") if m.cache_path else None)
            if m.type == "photo":
                im.append(InputMediaPhoto(media=media, has_spoiler=m.has_spoiler))
            elif m.type == "video":
                im.append(InputMediaVideo(media=media, has_spoiler=m.has_spoiler))
        res = await bot.send_media_group(chat_id=chat, media=im)
        sent_group_ids = [r.message_id for r in res]
        for x in im:
            f = getattr(x.media, "file", None) or getattr(x.media, "fp", None)
            try:
                if hasattr(f, "close"): f.close()
            except Exception:
                pass
    msg = await bot.send_message(chat_id=chat, text=post.text)
    return sent_group_ids, msg.message_id

@shared_task
def publish_post(post_id: int):
    post = Post.objects.select_related("channel").get(id=post_id)
    sent_group_ids, msg_id = asyncio.run(_publish_async(post))
    post.message_id = msg_id
    post.status = "PUBLISHED"
    post.save()
    return {"group": sent_group_ids, "text": msg_id}

@shared_task
def task_gpt_generate_for_channel(channel_id: int, count: int = 1):
    ch = Channel.objects.get(id=channel_id)
    added = 0
    for _ in range(count):
        text = services.gpt_new_draft(ch)
        Post.objects.create(channel=ch, text=text, status="DRAFT", origin="gpt")
        added += 1
    return added

@shared_task
def task_gpt_rewrite_post(post_id: int, editor_prompt: str):
    p = Post.objects.select_related("channel").get(id=post_id)
    new_text = services.gpt_rewrite_text(p.channel, p.text, editor_prompt)
    p.text = new_text
    p.save()
    return p.id
