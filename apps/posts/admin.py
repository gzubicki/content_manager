import os
from pathlib import Path
from uuid import uuid4

from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.contrib.admin import helpers
from django.contrib.admin.helpers import ActionForm as AdminActionForm
from django.contrib.admin.widgets import AdminSplitDateTime
from django.core.exceptions import PermissionDenied
from django.core.files.storage import default_storage
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.html import format_html

from . import services
from .models import Channel, DraftPost, Post, PostMedia, ScheduledPost
from .tasks import task_gpt_generate_for_channel, task_gpt_rewrite_post
from .validators import validate_post_text_for_channel


def media_public_url(media: PostMedia) -> str:
    cache_path = (media.cache_path or "").strip()
    if cache_path:
        media_root = Path(settings.MEDIA_ROOT).resolve()
        try:
            rel = Path(cache_path).resolve().relative_to(media_root)
        except ValueError:
            rel = None
        if rel is not None:
            return settings.MEDIA_URL.rstrip("/") + "/" + rel.as_posix()
    src = (media.source_url or "").strip()
    return src



class PostActionForm(AdminActionForm):
    prompt = forms.CharField(label="Prompt korekty (opcjonalny)", required=False)


class RescheduleForm(forms.Form):
    schedule_mode = forms.ChoiceField(
        label="Tryb planowania",
        choices=Post._meta.get_field("schedule_mode").choices,
        help_text="Wybierz AUTO, aby nadać termin zgodnie z harmonogramem kanału.",
    )
    scheduled_at = forms.SplitDateTimeField(
        label="Data publikacji",
        required=False,
        widget=AdminSplitDateTime(),
        help_text="Dla trybu ręcznego ustaw dokładną datę i godzinę publikacji.",
    )

    def clean(self):
        cleaned = super().clean()
        mode = cleaned.get("schedule_mode")
        dt = cleaned.get("scheduled_at")
        if mode == "MANUAL" and not dt:
            self.add_error("scheduled_at", "Podaj konkretną datę w trybie ręcznym.")
        return cleaned


class PostForm(forms.ModelForm):
    class Meta:
        model = Post
        fields = ["channel","text","status","scheduled_at","schedule_mode"]

    def clean(self):
        cleaned = super().clean()
        obj = self.instance
        if obj and obj.channel_id:
            validate_post_text_for_channel(obj)
        return cleaned


class PostMediaInlineForm(forms.ModelForm):
    upload = forms.FileField(label="Plik", required=False)

    class Meta:
        model = PostMedia
        fields = ["order", "type", "has_spoiler", "upload", "source_url"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["source_url"].widget.attrs.setdefault("placeholder", "https://...")
        self.fields["source_url"].widget.attrs["data-preview-source"] = "1"
        self.fields["upload"].widget.attrs.setdefault("accept", "image/*,video/*")
        self.fields["upload"].widget.attrs["data-preview-upload"] = "1"
        self.fields["type"].widget.attrs["data-preview-type"] = "1"
        self.fields["order"].widget.attrs["data-preview-order"] = "1"
        self.fields["has_spoiler"].widget.attrs["data-preview-spoiler"] = "1"

        existing_url = ""
        if self.instance and self.instance.pk:
            existing_url = media_public_url(self.instance)
        if existing_url:
            self.fields["source_url"].widget.attrs["data-existing-src"] = existing_url
            name_guess = existing_url.rstrip("/").split("/")[-1]
            self.fields["source_url"].widget.attrs["data-existing-name"] = name_guess or existing_url
        help_bits = []
        if existing_url:
            help_bits.append(f"Aktualny podgląd: {existing_url}")
        help_bits.append("Wgraj plik lub podaj URL – pozostaw jedno z pól puste.")
        self.fields["upload"].help_text = " ".join(help_bits)

    def clean(self):
        cleaned = super().clean()
        if not self.has_changed():
            return cleaned
        if cleaned.get("DELETE"):
            return cleaned
        upload = cleaned.get("upload")
        url = (cleaned.get("source_url") or "").strip()
        if not (upload or url or (self.instance and self.instance.pk)):
            raise forms.ValidationError("Dodaj plik lub URL dla medium.")
        return cleaned

    def save(self, commit=True):
        instance = super().save(commit=False)
        upload = self.cleaned_data.get("upload")
        if upload:
            ext = Path(upload.name or "").suffix or ".bin"
            filename = f"post_media/{uuid4().hex}{ext}"
            stored_path = default_storage.save(filename, upload)
            previous = (self.instance.cache_path or "").strip()
            if previous and os.path.exists(previous):
                try:
                    os.remove(previous)
                except OSError:
                    pass
            try:
                instance.cache_path = default_storage.path(stored_path)
            except NotImplementedError:
                instance.cache_path = stored_path
            instance.source_url = ""
        if commit:
            instance.save()
            self.save_m2m()
        return instance


class PostMediaInline(admin.StackedInline):
    model = PostMedia
    form = PostMediaInlineForm
    extra = 1
    verbose_name = "Medium"
    verbose_name_plural = "Media wpisu"
    fields = ("order", "type", "has_spoiler", "upload", "source_url", "existing_file")
    readonly_fields = ("existing_file",)
    classes = ("post-media-inline",)
    ordering = ("order", "id")

    def existing_file(self, obj):
        if not obj or not obj.pk:
            return "—"
        url = media_public_url(obj)
        if not url:
            return "—"
        if obj.type == "video":
            return format_html(
                '<video src="{}" controls preload="metadata" style="max-width: 260px; border-radius: 8px;"></video>',
                url,
            )
        if obj.type == "doc":
            return format_html('<a href="{0}" target="_blank" rel="noopener">📎 {0}</a>', url)
        return format_html(
            '<img src="{}" alt="Podgląd" style="max-width: 260px; border-radius: 8px;">',
            url,
        )

    existing_file.short_description = "Podgląd"

@admin.register(Channel)
class ChannelAdmin(admin.ModelAdmin):
    list_display = ("id","name","slug","tg_channel_id","language","draft_target_count")
    search_fields = ("name","slug","tg_channel_id")
    actions = ["act_fill_to_20","act_gpt_generate_20"]

    @admin.action(description="Uzupełnij do 20 (zaznaczone kanały)")
    def act_fill_to_20(self, request, queryset):
        queued = 0
        for ch in queryset:
            need = ch.draft_target_count - ch.posts.filter(status="DRAFT").count()
            if need > 0:
                task_gpt_generate_for_channel.delay(ch.id, need)
                queued += need
        self.message_user(
            request,
            ("Zlecono wygenerowanie %d draftów (GPT) w tle." % queued) if queued
            else "Zaznaczone kanały mają już komplet draftów.",
            level=messages.INFO if queued else messages.WARNING,
        )

    @admin.action(description="GPT: wygeneruj 20 draftów (async)")
    def act_gpt_generate_20(self, request, queryset):
        n = 0
        for ch in queryset:
            task_gpt_generate_for_channel.delay(ch.id, 20)
            n += 1
        self.message_user(request, f"Zlecono generowanie GPT dla {n} kanał(ów) – sprawdź za chwilę DRAFTY.")

class BasePostAdmin(admin.ModelAdmin):
    form = PostForm
    action_form = PostActionForm
    list_display = ("id","channel","status","scheduled_at","created_at","dupe_score","short")
    list_filter = ("channel","status","schedule_mode")
    actions = ["act_fill_to_20","act_approve","act_schedule","act_publish_now","act_delete","act_gpt_rewrite"]
    ordering = ("-created_at",)
    change_list_template = "admin/posts/post_cards.html"
    change_form_template = "admin/posts/change_form.html"
    reschedule_template = "admin/posts/reschedule.html"
    inlines = [PostMediaInline]

    def short(self, obj): return obj.text[:80] + ("…" if len(obj.text)>80 else "")

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        qs = qs.select_related("channel", "approved_by").prefetch_related("media")
        return self.filter_queryset(qs)

    def filter_queryset(self, qs):
        return qs

    def _object_url(self, obj, action):
        opts = self.model._meta
        return reverse(f"admin:{opts.app_label}_{opts.model_name}_{action}", args=[obj.pk])

    def _media_to_url(self, media: PostMedia) -> str:
        return media_public_url(media)

    def _build_preview_media(self, post: Post) -> list[dict]:
        urls: list[dict] = []
        media_manager = getattr(post, "media", None)
        if media_manager is None:
            return urls
        for media in media_manager.all():
            url = self._media_to_url(media)
            if url:
                name_source = media.cache_path or media.source_url or ""
                filename = Path(name_source).name if name_source else ""
                urls.append({
                    "src": url,
                    "type": media.type or "photo",
                    "name": filename,
                })
        return urls

    def _choice_label(self, field_name: str, value: str) -> str:
        field = self.model._meta.get_field(field_name)
        choices = dict(field.choices or [])
        return choices.get(value, value)

    def _build_preview_context(self, request, context, obj: Post | None) -> dict:
        adminform = context.get("adminform")
        form = getattr(adminform, "form", None)
        form_data = None
        if form is not None and form.is_bound:
            form_data = form.data
        elif request.method == "POST":
            form_data = request.POST

        channel = obj.channel if obj and obj.channel_id else None
        if form_data:
            channel_id = (form_data.get("channel") or "").strip()
            if channel_id:
                channel = Channel.objects.filter(pk=channel_id).first() or channel

        status_value = (form_data.get("status") if form_data and "status" in form_data else None) or (
            obj.status if obj else self.model._meta.get_field("status").get_default()
        )
        schedule_mode_value = (
            form_data.get("schedule_mode") if form_data and "schedule_mode" in form_data else None
        ) or (obj.schedule_mode if obj else self.model._meta.get_field("schedule_mode").get_default())

        text_value = ""
        if form_data and "text" in form_data:
            text_value = form_data.get("text", "")
        elif obj:
            text_value = obj.text

        scheduled_display = ""
        if form_data and (form_data.get("scheduled_at_0") or form_data.get("scheduled_at_1")):
            date_part = (form_data.get("scheduled_at_0") or "").strip()
            time_part = (form_data.get("scheduled_at_1") or "").strip()
            scheduled_display = f"{date_part} {time_part}".strip()
        elif obj and obj.scheduled_at:
            scheduled_display = date_format(timezone.localtime(obj.scheduled_at), "d.m.Y H:i")

        expires_display = ""
        if obj and obj.expires_at:
            expires_display = date_format(timezone.localtime(obj.expires_at), "d.m.Y H:i")

        created_display = ""
        if obj and obj.created_at:
            created_display = date_format(timezone.localtime(obj.created_at), "d.m.Y H:i")

        dupe_display = ""
        if obj and obj.dupe_score is not None:
            dupe_display = f"{obj.dupe_score:.2f}"

        preview_media = self._build_preview_media(obj) if obj else []

        return {
            "id": obj.pk if obj else None,
            "channel_name": str(channel) if channel else "(wybierz kanał)",
            "status": status_value,
            "status_display": self._choice_label("status", status_value),
            "schedule_mode": schedule_mode_value,
            "schedule_mode_display": self._choice_label("schedule_mode", schedule_mode_value),
            "scheduled_at_display": scheduled_display,
            "expires_at_display": expires_display,
            "created_at_display": created_display,
            "dupe_score_display": dupe_display or "–",
            "text": text_value,
            "media": preview_media,
        }

    def render_change_form(self, request, context, add=False, change=False, form_url="", obj=None):
        preview = self._build_preview_context(request, context, obj)
        context["preview"] = preview
        return super().render_change_form(request, context, add=add, change=change, form_url=form_url, obj=obj)

    def changelist_view(self, request, extra_context=None):
        response = super().changelist_view(request, extra_context=extra_context)
        if hasattr(response, "context_data"):
            cl = response.context_data.get("cl")
            if cl:
                response.context_data.setdefault("action_checkbox_name", helpers.ACTION_CHECKBOX_NAME)
                for post in cl.result_list:
                    post.preview_media = self._build_preview_media(post)
                    post.change_url = self._object_url(post, "change")
                    post.delete_url = self._object_url(post, "delete")
                    post.reschedule_url = self._object_url(post, "reschedule")
        return response

    def get_urls(self):
        urls = super().get_urls()
        opts = self.model._meta
        custom = [
            path(
                "<int:object_id>/przeloz/",
                self.admin_site.admin_view(self.reschedule_view),
                name=f"{opts.app_label}_{opts.model_name}_reschedule",
            )
        ]
        return custom + urls

    def reschedule_view(self, request, object_id):
        post = get_object_or_404(self.model, pk=object_id)
        if not self.has_change_permission(request, post):
            raise PermissionDenied

        tzinfo = timezone.get_current_timezone()
        initial = {"schedule_mode": post.schedule_mode}
        if post.scheduled_at:
            local_dt = timezone.localtime(post.scheduled_at, tzinfo)
            initial["scheduled_at"] = timezone.make_naive(local_dt, tzinfo)

        form = RescheduleForm(request.POST or None, initial=initial)

        if request.method == "POST" and form.is_valid():
            mode = form.cleaned_data["schedule_mode"]
            post.schedule_mode = mode
            if mode == "AUTO":
                services.assign_auto_slot(post)
                msg = "Wyznaczono termin publikacji automatycznie."
            else:
                scheduled_at = form.cleaned_data.get("scheduled_at")
                if scheduled_at:
                    if timezone.is_naive(scheduled_at):
                        scheduled_at = timezone.make_aware(scheduled_at, tzinfo)
                    post.scheduled_at = scheduled_at
                post.status = "SCHEDULED"
                post.dupe_score = services.compute_dupe(post)
                post.save()
                msg = "Zmieniono termin publikacji."
            self.message_user(request, msg, level=messages.SUCCESS)
            changelist_url = reverse(f"admin:{self.model._meta.app_label}_{self.model._meta.model_name}_changelist")
            return redirect(changelist_url)

        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "original": post,
            "title": "Przełóż publikację",
            "form": form,
            "media": form.media,
            "changelist_url": reverse(f"admin:{self.model._meta.app_label}_{self.model._meta.model_name}_changelist"),
        }
        return TemplateResponse(request, self.reschedule_template, context)

    @admin.action(description="Uzupełnij do 20 (bieżący kanał / wszystkie jeśli brak selekcji)")
    def act_fill_to_20(self, request, qs):
        channels = {p.channel for p in qs} or set(Channel.objects.all())
        queued = 0
        for ch in channels:
            need = ch.draft_target_count - ch.posts.filter(status="DRAFT").count()
            if need > 0:
                task_gpt_generate_for_channel.delay(ch.id, need)
                queued += need
        self.message_user(
            request,
            ("Zlecono wygenerowanie %d draftów (GPT) w tle." % queued) if queued
            else "Kanały mają już komplet draftów.",
            level=messages.INFO if queued else messages.WARNING,
        )

    @admin.action(description="Zatwierdź i nadaj slot AUTO")
    def act_approve(self, request, qs):
        for p in qs:
            p.status = "APPROVED"; p.approved_by = request.user; p.save()
            services.assign_auto_slot(p)

    @admin.action(description="Przelicz slot AUTO")
    def act_schedule(self, request, qs):
        for p in qs: services.assign_auto_slot(p)

    @admin.action(description="Opublikuj teraz")
    def act_publish_now(self, request, qs):
        from .tasks import publish_post
        for p in qs: publish_post.delay(p.id)

    @admin.action(description="Usuń")
    def act_delete(self, request, qs):
        qs.delete()

    @admin.action(description="GPT: korekta zaznaczonych (z promptem)")
    def act_gpt_rewrite(self, request, queryset):
        prompt = (request.POST or {}).get("prompt", "").strip()
        cnt = 0
        for p in queryset:
            task_gpt_rewrite_post.delay(p.id, prompt or "Popraw styl i klarowność, zachowaj treść i stopkę.")
            cnt += 1
        self.message_user(request, f"Zlecono korektę GPT dla {cnt} wpisów.", level=messages.INFO)


@admin.register(DraftPost)
class DraftPostAdmin(BasePostAdmin):
    def filter_queryset(self, qs):
        return qs.filter(status="DRAFT")


@admin.register(ScheduledPost)
class ScheduledPostAdmin(BasePostAdmin):
    actions = ["act_schedule","act_publish_now","act_delete","act_gpt_rewrite"]
    ordering = ("scheduled_at",)
    date_hierarchy = "scheduled_at"

    def filter_queryset(self, qs):
        return qs.filter(status__in=["APPROVED","SCHEDULED"], scheduled_at__isnull=False)


@admin.register(PostMedia)
class PostMediaAdmin(admin.ModelAdmin):
    list_display = ("id","post","type","order","has_spoiler","cache_path","tg_file_id")
