import json
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from uuid import uuid4

from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.contrib.admin import helpers
from django.contrib.admin.views.main import SEARCH_VAR
from django.contrib.admin.widgets import AdminSplitDateTime
from django.core.exceptions import PermissionDenied
from django.core.serializers.json import DjangoJSONEncoder
from django.core.files.storage import default_storage
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.html import format_html, format_html_join
from django.utils.text import Truncator
from django.utils.translation import gettext, ngettext

from . import services
from .models import Channel, DraftPost, Post, PostMedia, ScheduledPost
from .tasks import task_gpt_generate_for_channel, task_gpt_rewrite_post
from .drafts import iter_missing_draft_requirements
from .validators import validate_post_text_for_channel


def enqueue_missing_drafts(channels: Iterable[Channel]) -> tuple[int, int]:
    """Queue GPT draft generation tasks to reach configured targets.

    Returns a tuple ``(queued_posts, affected_channels)``.
    """

    queued = 0
    affected = 0
    for channel_id, need in iter_missing_draft_requirements(channels):
        task_gpt_generate_for_channel.delay(channel_id, need)
        queued += need
        affected += 1
    return queued, affected


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


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".mpg", ".mpeg"}


def guess_media_type(name: str, content_type: str = "") -> str:
    name = (name or "").lower()
    content_type = (content_type or "").lower()
    suffix = Path(name).suffix
    if content_type.startswith("image/") or suffix in IMAGE_EXTENSIONS:
        return "photo"
    if content_type.startswith("video/") or suffix in VIDEO_EXTENSIONS:
        return "video"
    return "doc"



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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        text_field = self.fields.get("text")
        if text_field:
            widget = text_field.widget
            widget.attrs.setdefault("data-telegram-editor", "1")
            widget.attrs.setdefault("rows", 14)

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
        self.fields["type"].required = False
        self.fields["type"].widget = forms.HiddenInput()
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

        guessed = None
        if upload:
            guessed = guess_media_type(getattr(upload, "name", ""), getattr(upload, "content_type", ""))
        elif url:
            guessed = guess_media_type(url)

        if guessed:
            cleaned["type"] = guessed
        cleaned["type"] = cleaned.get("type") or (self.instance.type if self.instance and self.instance.pk else "photo")
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
    actions = ["act_fill_to_target","act_gpt_fill_missing"]

    def _handle_draft_fill_action(self, request, queryset, empty_message: str) -> None:
        queued, affected = enqueue_missing_drafts(queryset)
        if queued:
            self.message_user(
                request,
                f"Zlecono wygenerowanie brakujących draftów (GPT) w tle dla {affected} kanał(ów) (łącznie {queued}).",
                level=messages.INFO,
            )
        else:
            self.message_user(request, empty_message, level=messages.WARNING)

    @admin.action(description="Uzupełnij drafty")
    def act_fill_to_target(self, request, queryset):
        self._handle_draft_fill_action(
            request,
            queryset,
            "Zaznaczone kanały mają już komplet draftów.",
        )

    @admin.action(description="GPT: uzupełnij brakujące drafty (async)")
    def act_gpt_fill_missing(self, request, queryset):
        self._handle_draft_fill_action(
            request,
            queryset,
            "Zaznaczone kanały mają już komplet draftów.",
        )

DEFAULT_REWRITE_PROMPT = "Popraw styl i klarowność, zachowaj treść i stopkę."


class RewritePromptForm(forms.Form):
    prompt = forms.CharField(
        label="Dodatkowy prompt dla GPT",
        required=False,
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text="Pozostaw puste, aby użyć domyślnej korekty stylu.",
    )


class BasePostAdmin(admin.ModelAdmin):
    form = PostForm
    list_display = ("id","channel","status","scheduled_at","created_at","dupe_score","short")
    list_filter = ("channel","status","schedule_mode")
    actions = ["act_fill_to_target","act_approve","act_schedule","act_publish_now","act_delete"]
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

    def _changelist_url(self):
        opts = self.model._meta
        return reverse(f"admin:{opts.app_label}_{opts.model_name}_changelist")

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

    def _serialize_media(self, media: list[dict]) -> str:
        data = json.dumps(media, cls=DjangoJSONEncoder)
        return (
            data.replace("</", "\\u003C/")
            .replace("\u2028", "\\u2028")
            .replace("\u2029", "\\u2029")
        )

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

        media_json = self._serialize_media(preview_media)

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
            "media_json": media_json,
        }

    def render_change_form(self, request, context, add=False, change=False, form_url="", obj=None):
        preview = self._build_preview_context(request, context, obj)
        context["preview"] = preview
        context["preview_media_json"] = preview.get("media_json", "[]")
        if obj and obj.pk:
            context["rewrite_url"] = self._object_url(obj, "rewrite")
            context["status_url"] = self._object_url(obj, "status")
        channel_meta = list(Channel.objects.values("id", "name", "max_chars", "emoji_min", "emoji_max", "no_links_in_text"))
        context["channel_metadata_json"] = self._serialize_media(channel_meta)
        return super().render_change_form(request, context, add=add, change=change, form_url=form_url, obj=obj)

    def _serialize_post_state(self, post: Post) -> dict:
        channel_name = post.channel.name if post.channel_id and post.channel else ""
        scheduled_iso = None
        scheduled_date = ""
        scheduled_time = ""
        scheduled_display = ""
        if post.scheduled_at:
            scheduled_local = timezone.localtime(post.scheduled_at)
            scheduled_iso = scheduled_local.isoformat()
            scheduled_date = scheduled_local.strftime("%Y-%m-%d")
            scheduled_time = scheduled_local.strftime("%H:%M:%S")
            scheduled_display = date_format(scheduled_local, "d.m.Y H:i")
        return {
            "id": post.pk,
            "text": post.text,
            "status": post.status,
            "status_label": post.get_status_display(),
            "schedule_mode": post.schedule_mode,
            "schedule_mode_label": post.get_schedule_mode_display(),
            "scheduled_at": scheduled_iso,
            "scheduled_date": scheduled_date,
            "scheduled_time": scheduled_time,
            "scheduled_display": scheduled_display,
            "channel_id": post.channel_id,
            "channel_name": channel_name,
            "generated_at": timezone.now().isoformat(),
        }

    def _serialize_media_state(self, media_items: Iterable[PostMedia]) -> list[dict]:
        serialized: list[dict] = []
        for media in media_items:
            public_url = self._media_to_url(media)
            name_source = media.cache_path or media.source_url or ""
            serialized.append(
                {
                    "id": media.pk,
                    "type": media.type or "photo",
                    "order": media.order,
                    "has_spoiler": media.has_spoiler,
                    "source_url": media.source_url or "",
                    "media_public_url": public_url,
                    "name": Path(name_source).name if name_source else "",
                }
            )
        return serialized

    def _filters_session_key(self) -> str:
        opts = self.model._meta
        return f"admin:filters:{opts.app_label}.{opts.model_name}"

    def _restore_filters_if_needed(self, request):
        if request.method != "GET":
            return None

        session_key = self._filters_session_key()
        if request.GET.get("_clear_session_filters"):
            request.session.pop(session_key, None)
            params = request.GET.copy()
            params.pop("_clear_session_filters", None)
            url = request.path
            query = params.urlencode()
            return redirect(f"{url}?{query}" if query else url)

        saved = request.session.get(session_key)
        if not saved:
            return None

        saved_filters = saved.get("filters") or {}
        saved_search = saved.get("search")

        params = request.GET.copy()
        updated = False
        for key, value in saved_filters.items():
            if key in params:
                continue
            if isinstance(value, (list, tuple)):
                params.setlist(key, list(value))
            else:
                params[key] = value
            updated = True
        if saved_search and SEARCH_VAR not in params:
            params[SEARCH_VAR] = saved_search
            updated = True

        if not updated:
            return None

        params.pop("_changelist_filters", None)
        url = request.path
        query = params.urlencode()
        return redirect(f"{url}?{query}" if query else url)

    def _store_filters_in_session(self, request, changelist) -> None:
        session_key = self._filters_session_key()
        filters = dict(changelist.get_filters_params()) if changelist else {}
        stored: dict[str, Any] = {}
        if filters:
            stored["filters"] = filters
        search_value = getattr(changelist, "query", "") or ""
        if search_value:
            stored["search"] = search_value

        if stored:
            request.session[session_key] = stored
        else:
            request.session.pop(session_key, None)

    cards_refresh_interval_ms = 20000

    def get_cards_refresh_interval(self) -> int:
        interval = int(self.cards_refresh_interval_ms or 0)
        return interval if interval > 0 else 20000

    def _is_cards_partial_request(self, request) -> bool:
        if request.GET.get("_partial") == "cards":
            return True
        requested_with = request.headers.get("X-Requested-With") or request.META.get("HTTP_X_REQUESTED_WITH", "")
        return requested_with.lower() == "xmlhttprequest" and request.GET.get("_cards") == "1"

    def _render_cards_partial(self, request, context):
        cl = context.get("cl")
        if cl is None:
            cl = type("EmptyChangeList", (), {"result_list": [], "result_count": 0})()
        partial_context = {
            "cl": cl,
            "action_checkbox_name": context.get("action_checkbox_name", helpers.ACTION_CHECKBOX_NAME),
            "actions_selection_counter": context.get("actions_selection_counter"),
            "selection_note_template": context.get(
                "selection_note_template",
                gettext("%(sel)s of %(cnt)s selected"),
            ),
            "selection_note_all_template": context.get(
                "selection_note_all_template",
                ngettext("%(total_count)s selected", "All %(total_count)s selected", getattr(cl, "result_count", 0)),
            ),
            "post_cards_refresh_interval": context.get("post_cards_refresh_interval", self.get_cards_refresh_interval()),
        }
        return TemplateResponse(
            request,
            "admin/posts/includes/post_card_grid.html",
            partial_context,
        )

    def changelist_view(self, request, extra_context=None):
        redirect_response = self._restore_filters_if_needed(request)
        if redirect_response is not None:
            return redirect_response

        response = super().changelist_view(request, extra_context=extra_context)
        if hasattr(response, "context_data"):
            cl = response.context_data.get("cl")
            remembered = bool(request.session.get(self._filters_session_key()))
            if cl:
                response.context_data.setdefault("action_checkbox_name", helpers.ACTION_CHECKBOX_NAME)
                response.context_data.setdefault(
                    "selection_note_template",
                    gettext("%(sel)s of %(cnt)s selected"),
                )
                response.context_data.setdefault(
                    "selection_note_all_template",
                    ngettext("%(total_count)s selected", "All %(total_count)s selected", cl.result_count),
                )
                response.context_data.setdefault(
                    "post_cards_refresh_interval", self.get_cards_refresh_interval()
                )
                for post in cl.result_list:
                    post.preview_media = self._build_preview_media(post)
                    post.change_url = self._object_url(post, "change")
                    post.delete_url = self._object_url(post, "delete")
                    post.reschedule_url = self._object_url(post, "reschedule")
                    post.rewrite_url = self._object_url(post, "rewrite")
                    post.approve_url = self.get_approve_url(post)
                    post.is_draft = post.status == Post.Status.DRAFT
                self._store_filters_in_session(request, cl)
                remembered = bool(request.session.get(self._filters_session_key()))
            response.context_data.setdefault("request", request)
            response.context_data["session_filters_remembered"] = remembered
            if self._is_cards_partial_request(request):
                return self._render_cards_partial(request, response.context_data)
        return response

    def get_approve_url(self, post):
        return None

    def get_urls(self):
        urls = super().get_urls()
        opts = self.model._meta
        custom = [
            path(
                "<int:object_id>/przeloz/",
                self.admin_site.admin_view(self.reschedule_view),
                name=f"{opts.app_label}_{opts.model_name}_reschedule",
            ),
            path(
                "<int:object_id>/gpt-korekta/",
                self.admin_site.admin_view(self.rewrite_view),
                name=f"{opts.app_label}_{opts.model_name}_rewrite",
            ),
            path(
                "<int:object_id>/status/",
                self.admin_site.admin_view(self.status_view),
                name=f"{opts.app_label}_{opts.model_name}_status",
            ),
        ]
        return custom + urls

    def status_view(self, request, object_id):
        if request.method != "GET":
            raise PermissionDenied
        queryset = (
            self.model._default_manager.select_related("channel").prefetch_related("media")
        )
        post = get_object_or_404(queryset, pk=object_id)
        if not (
            self.has_view_permission(request, post)
            or self.has_change_permission(request, post)
        ):
            raise PermissionDenied
        data = {
            "post": self._serialize_post_state(post),
            "media": self._serialize_media_state(post.media.all()),
        }
        return JsonResponse(data, encoder=DjangoJSONEncoder)

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
            "title": "Zmień termin publikacji",
            "form": form,
            "media": form.media,
            "changelist_url": reverse(f"admin:{self.model._meta.app_label}_{self.model._meta.model_name}_changelist"),
        }
        return TemplateResponse(request, self.reschedule_template, context)

    def rewrite_view(self, request, object_id):
        post = get_object_or_404(self.model, pk=object_id)
        if not self.has_change_permission(request, post):
            raise PermissionDenied

        form = RewritePromptForm(request.POST or None)

        if request.method == "POST" and form.is_valid():
            prompt = (form.cleaned_data.get("prompt") or "").strip() or DEFAULT_REWRITE_PROMPT
            task_gpt_rewrite_post.delay(post.id, prompt)
            self.message_user(
                request,
                "Zlecono korektę wpisu przy użyciu GPT.",
                level=messages.INFO,
            )
            changelist_url = reverse(f"admin:{self.model._meta.app_label}_{self.model._meta.model_name}_changelist")
            return redirect(changelist_url)

        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "original": post,
            "title": "Korekta wpisu przez GPT",
            "form": form,
            "media": form.media,
            "default_prompt": DEFAULT_REWRITE_PROMPT,
            "changelist_url": reverse(f"admin:{self.model._meta.app_label}_{self.model._meta.model_name}_changelist"),
        }
        return TemplateResponse(request, "admin/posts/rewrite.html", context)

    @admin.action(description="Uzupełnij drafty")
    def act_fill_to_target(self, request, qs):
        channels = {p.channel for p in qs if getattr(p, "channel_id", None)} or set(Channel.objects.all())
        queued, affected = enqueue_missing_drafts(channels)
        if queued:
            self.message_user(
                request,
                f"Zlecono wygenerowanie brakujących draftów (GPT) w tle dla {affected} kanał(ów) (łącznie {queued}).",
                level=messages.INFO,
            )
        else:
            self.message_user(
                request,
                "Kanały mają już komplet draftów.",
                level=messages.WARNING,
            )

    @admin.action(description="Zatwierdź i nadaj slot AUTO")
    def act_approve(self, request, qs):
        for p in qs:
            services.approve_post(p, request.user)

    @admin.action(description="Przelicz slot AUTO")
    def act_schedule(self, request, qs):
        for post in qs:
            services.assign_auto_slot(post)

    @admin.action(description="Opublikuj teraz")
    def act_publish_now(self, request, qs):
        from .tasks import publish_post
        for post in qs:
            publish_post.delay(post.id)

    @admin.action(description="Usuń")
    def act_delete(self, request, qs):
        qs.delete()

@admin.register(DraftPost)
class DraftPostAdmin(BasePostAdmin):
    approve_action = "approve"
    approve_path = "<int:object_id>/akceptuj/"

    def filter_queryset(self, qs):
        return qs.filter(status=Post.Status.DRAFT)

    def get_urls(self):
        urls = super().get_urls()
        opts = self.model._meta
        custom = [
            path(
                self.approve_path,
                self.admin_site.admin_view(self.approve_view),
                name=f"{opts.app_label}_{opts.model_name}_{self.approve_action}",
            ),
        ]
        return custom + urls

    def get_approve_url(self, post):
        return self._object_url(post, self.approve_action)

    def approve_view(self, request, object_id):
        post = get_object_or_404(self.model, pk=object_id)
        if not self.has_change_permission(request, post):
            raise PermissionDenied

        if request.method != "POST":
            return redirect(self._changelist_url())

        if post.status != Post.Status.DRAFT:
            self.message_user(
                request,
                "Ten wpis nie jest już draftem.",
                level=messages.WARNING,
            )
        else:
            services.approve_post(post, request.user)
            self.message_user(
                request,
                "Draft zatwierdzony i zaplanowany automatycznie.",
                level=messages.SUCCESS,
            )

        return redirect(self._changelist_url())


@admin.register(ScheduledPost)
class ScheduledPostAdmin(BasePostAdmin):
    actions = ["act_schedule","act_publish_now","act_delete"]
    ordering = ("scheduled_at",)
    date_hierarchy = "scheduled_at"

    def filter_queryset(self, qs):
        return qs.filter(
            status__in=[Post.Status.APPROVED, Post.Status.SCHEDULED],
            scheduled_at__isnull=False,
        )


@admin.register(PostMedia)
class PostMediaAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "preview",
        "post_with_channel",
        "type",
        "source_link",
        "related_posts",
        "order",
        "has_spoiler",
        "created_at",
        "tg_file_id",
    )
    list_select_related = ("post", "post__channel")
    ordering = ("-created_at", "-id")

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        self._related_posts_cache: dict[str, list[Post]] = {}
        return qs.select_related("post", "post__channel")

    @admin.display(description="Wpis", ordering="post__id")
    def post_with_channel(self, obj: PostMedia) -> str:
        post = obj.post
        post_id = getattr(post, "id", None)
        if not post_id:
            return "—"
        channel = getattr(post, "channel", None)
        channel_name = getattr(channel, "name", "?")
        url = reverse(
            f"admin:{Post._meta.app_label}_{Post._meta.model_name}_change",
            args=[post_id],
        )
        return format_html("<a href='{}'>#{} – {}</a>", url, post_id, channel_name)

    @admin.display(description="Podgląd")
    def preview(self, obj: PostMedia) -> str:
        url = media_public_url(obj)
        if not url:
            return "—"
        if obj.type == "photo":
            return format_html(
                "<img src='{}' alt='' style='max-height:80px;max-width:120px;"
                "object-fit:cover;border-radius:4px;' />",
                url,
            )
        if obj.type == "video":
            return format_html(
                "<video src='{}' controls style='max-height:80px;max-width:120px;'>"
                "Twoja przeglądarka nie obsługuje podglądu wideo.</video>",
                url,
            )
        return format_html(
            "<a href='{}' target='_blank' rel='noopener'>Pobierz</a>",
            url,
        )

    @admin.display(description="Źródłowy URL", ordering="source_url")
    def source_link(self, obj: PostMedia) -> str:
        url = (obj.source_url or "").strip()
        if not url:
            return "—"
        label = Truncator(url).chars(60)
        return format_html(
            "<a href='{}' target='_blank' rel='noopener'>{}</a>",
            url,
            label,
        )

    @admin.display(description="Powiązane posty")
    def related_posts(self, obj: PostMedia) -> str:
        posts = self._get_related_posts(obj)
        if not posts:
            return "—"
        return format_html_join(
            "<br>",
            "<a href='{}'>#{} – {}</a>",
            (
                (
                    reverse(
                        f"admin:{Post._meta.app_label}_{Post._meta.model_name}_change",
                        args=[post.id],
                    ),
                    post.id,
                    getattr(post.channel, "name", "?"),
                )
                for post in posts
            ),
        )

    def _get_related_posts(self, obj: PostMedia) -> list[Post]:
        source_url = (obj.source_url or "").strip()
        if not source_url:
            return []
        cached = self._related_posts_cache.get(source_url)
        if cached is not None:
            return [post for post in cached if post.id != obj.post_id]
        posts = list(
            Post.objects.filter(media__source_url=source_url)
            .select_related("channel")
            .distinct()
        )
        self._related_posts_cache[source_url] = posts
        return [post for post in posts if post.id != obj.post_id]
