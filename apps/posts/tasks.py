from celery import shared_task
from django.utils import timezone
from telegram import InputMediaPhoto, InputMediaVideo
from .models import Post
from . import services

@shared_task
def task_ensure_min_drafts():
    from .models import Channel
    for ch in Channel.objects.all():
        services.ensure_min_drafts(ch)

@shared_task
def task_housekeeping():
    from django.utils import timezone
    from .models import Post, PostMedia
    Post.objects.filter(status="DRAFT", expires_at__lt=timezone.now()).delete()
    services.purge_cache()

@shared_task
def task_publish_due():
    now = timezone.now()
    due = Post.objects.select_related("channel").filter(status__in=("APPROVED","SCHEDULED"), scheduled_at__lte=now)
    for p in due:
        publish_post.delay(p.id)

@shared_task
def publish_post(post_id: int):
    post = Post.objects.select_related("channel").get(id=post_id)
    bot = services._bot_for(post.channel)
    chat = post.channel.tg_channel_id
    medias = list(post.media.all())
    sent_group_ids = []

    if medias:
        im = []
        for m in medias:
            if m.type == "photo":
                im.append(InputMediaPhoto(media=m.tg_file_id or open(m.cache_path, "rb"), has_spoiler=m.has_spoiler))
            elif m.type == "video":
                im.append(InputMediaVideo(media=m.tg_file_id or open(m.cache_path, "rb"), has_spoiler=m.has_spoiler))
        res = bot.send_media_group(chat_id=chat, media=im)
        sent_group_ids = [r.message_id for r in res]

    msg = bot.send_message(chat_id=chat, text=post.text)
    post.message_id = msg.message_id
    post.status = "PUBLISHED"
    post.save()
    return {"group": sent_group_ids, "text": msg.message_id}
