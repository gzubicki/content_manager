import json
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any
import logging
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
from django.urls import NoReverseMatch, path, reverse
from django.db.models import Q
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.html import format_html, format_html_join
from django.utils.text import Truncator
from django.utils.translation import gettext, ngettext

from django.contrib.admin.templatetags.admin_urls import admin_urlname

from . import services
from .models import Channel, ChannelSource, DraftPost, HistoryPost, Post, PostMedia, ScheduledPost
from .tasks import (
    task_gpt_generate_for_channel,
    task_gpt_generate_from_article,
    task_gpt_rewrite_post,
)


logger = logging.getLogger(__name__)
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



ADMIN_PAGE_SIZE = 20


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
        fields = ["channel", "text", "source_url", "status", "scheduled_at", "schedule_mode"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        text_field = self.fields.get("text")
        if text_field:
            widget = text_field.widget
            widget.attrs.setdefault("data-telegram-editor", "1")
            widget.attrs.setdefault("rows", 14)
        source_field = self.fields.get("source_url")
        if source_field:
            widget = source_field.widget
            widget.attrs.setdefault("placeholder", "https://...")

    def clean(self):
        cleaned = super().clean()
        obj = self.instance
        if obj and obj.channel_id:
            validate_post_text_for_channel(obj)
        return cleaned

    def clean_source_url(self) -> str:
        value = (self.cleaned_data.get("source_url") or "").strip()
        return value


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

class ChannelAdminForm(forms.ModelForm):
    class Meta:
        model = Channel
        fields = "__all__"
        widgets = {
            "style_prompt": forms.Textarea(
                attrs={
                    "rows": 18,
                    "class": "vLargeTextField channel-style-prompt",
                    "style": "min-height: 320px; font-family: var(--font-family-monospace, monospace);",
                }
            )
        }


class ChannelSourceInline(admin.TabularInline):
    model = ChannelSource
    extra = 1
    fields = ("name", "url", "priority", "is_active")
    ordering = ("-is_active", "-priority", "name")
    verbose_name = "Źródło"
    verbose_name_plural = "Źródła"


@admin.register(Channel)
class ChannelAdmin(admin.ModelAdmin):
    form = ChannelAdminForm
    list_per_page = ADMIN_PAGE_SIZE
    list_display = ("id","name","slug","tg_channel_id","language","draft_target_count")
    search_fields = ("name","slug","tg_channel_id")
    actions = ["act_fill_to_target","act_gpt_fill_missing"]
    inlines = [ChannelSourceInline]

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


class MultiFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class GptDraftRequestForm(forms.Form):
    channel = forms.ModelChoiceField(
        label="Kanał",
        queryset=Channel.objects.none(),
    )
    title = forms.CharField(
        label="Tytuł / temat",
        required=False,
        max_length=256,
    )
    summary = forms.CharField(
        label="Streszczenie",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    text = forms.CharField(
        label="Treść źródłowa",
        required=False,
        widget=forms.Textarea(attrs={"rows": 10}),
    )
    source_url = forms.URLField(
        label="Źródło (URL)",
        required=False,
    )
    attachments = forms.FileField(
        label="Załączniki (opcjonalne)",
        required=False,
        widget=MultiFileInput(attrs={"multiple": True}),
        help_text="Dodaj pliki, które mają pomóc GPT w przygotowaniu draftu.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["channel"].queryset = Channel.objects.all().order_by("name")
        self.fields["channel"].empty_label = None
        self.fields["source_url"].widget.attrs.setdefault("placeholder", "https://...")

    def clean_attachments(self):
        if not self.files:
            return []
        return self.files.getlist(self.add_prefix("attachments"))

    def clean(self):
        cleaned = super().clean()
        has_text = bool((cleaned.get("text") or "").strip())
        has_summary = bool((cleaned.get("summary") or "").strip())
        has_source = bool((cleaned.get("source_url") or "").strip())
        has_attachments = bool(cleaned.get("attachments"))
        if not any((has_text, has_summary, has_source, has_attachments)):
            raise forms.ValidationError(
                "Podaj treść, streszczenie, źródło lub dodaj załączniki, aby wysłać dane do GPT."
            )
        return cleaned


class DraftImportForm(forms.Form):
    drafts_file = forms.FileField(
        label="Plik JSON",
        help_text="Oczekiwany jest plik JSON z listą draftów lub obiekt z polem drafts.",
    )
    default_channel = forms.ModelChoiceField(
        label="Kanał domyślny",
        required=False,
        queryset=Channel.objects.none(),
        help_text="Zostanie użyty, gdy rekord nie określa kanału.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["default_channel"].queryset = Channel.objects.all().order_by("name")
        self.fields["default_channel"].empty_label = "— wybierz kanał —"

    def clean_drafts_file(self):
        uploaded = self.files.get(self.add_prefix("drafts_file"))
        if not uploaded:
            raise forms.ValidationError("Wybierz plik JSON do importu.")
        try:
            raw = uploaded.read()
        except Exception as exc:  # pragma: no cover - defensywne
            raise forms.ValidationError("Nie udało się odczytać pliku JSON.") from exc
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise forms.ValidationError("Plik JSON musi być zakodowany w UTF-8.") from exc
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise forms.ValidationError(f"Niepoprawny JSON: {exc}") from exc

        entries = self._coerce_entries(parsed)
        if not entries:
            raise forms.ValidationError("Plik JSON nie zawiera żadnych draftów do importu.")
        return entries

    def clean(self):
        cleaned = super().clean()
        entries = cleaned.get("drafts_file") or []
        default_channel = cleaned.get("default_channel")
        missing: list[int] = []
        for index, entry in enumerate(entries, start=1):
            if not self._has_channel_hint(entry) and default_channel is None:
                missing.append(index)
        if missing:
            preview = ", ".join(str(num) for num in missing[:5])
            if len(missing) > 5:
                preview += ", …"
            raise forms.ValidationError(
                (
                    "Drafty #{numbers} nie wskazują kanału – podaj channel_id/channel_slug "
                    "w pliku lub wybierz kanał domyślny."
                ).format(numbers=preview)
            )
        return cleaned

    @staticmethod
    def _coerce_entries(parsed: Any) -> list[dict[str, Any]]:
        if isinstance(parsed, dict):
            if isinstance(parsed.get("drafts"), list):
                items = parsed.get("drafts") or []
            else:
                items = [parsed]
        elif isinstance(parsed, list):
            items = parsed
        else:
            raise forms.ValidationError("Oczekiwano listy lub obiektu JSON z polem drafts.")

        entries: list[dict[str, Any]] = []
        for index, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                raise forms.ValidationError(
                    f"Element #{index} w pliku JSON nie jest obiektem – oczekiwano mapy klucz/wartość."
                )
            entries.append(item)
        return entries

    @staticmethod
    def _has_channel_hint(entry: dict[str, Any]) -> bool:
        if not isinstance(entry, dict):
            return False
        if any(key in entry for key in ("channel_id", "channel_slug")):
            return True
        channel_field = entry.get("channel")
        if isinstance(channel_field, dict):
            return any(key in channel_field for key in ("id", "slug"))
        if isinstance(channel_field, (int, str)) and str(channel_field).strip():
            return True
        return False


class BasePostAdmin(admin.ModelAdmin):
    form = PostForm
    list_per_page = ADMIN_PAGE_SIZE
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
                    "resolver": getattr(media, "resolver", ""),
                    "reference": media.reference_data if isinstance(media.reference_data, dict) else {},
                    "source_url": media.source_url or "",
                    "cache_path": media.cache_path or "",
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
        rewrite_state = self._serialize_rewrite_state(obj) if obj else {}
        context["rewrite_state"] = rewrite_state
        context["rewrite_state_json"] = (
            json.dumps(rewrite_state, cls=DjangoJSONEncoder)
            .replace("</", "\\u003C/")
            .replace("\u2028", "\\u2028")
            .replace("\u2029", "\\u2029")
        )
        channel_meta = list(Channel.objects.values("id", "name", "max_chars", "no_links_in_text"))
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
            "source_url": post.source_url or "",
            "generated_at": timezone.now().isoformat(),
        }

    def _serialize_rewrite_state(self, post: Post | None) -> dict:
        if post is None:
            return {}
        metadata = getattr(post, "source_metadata", {})
        if not isinstance(metadata, dict):
            return {}
        rewrite = metadata.get("rewrite")
        if not isinstance(rewrite, dict):
            return {}
        return {
            "status": str(rewrite.get("status") or ""),
            "prompt": rewrite.get("prompt") or "",
            "requested_at": rewrite.get("requested_at") or "",
            "requested_display": rewrite.get("requested_display") or "",
            "completed_at": rewrite.get("completed_at") or "",
            "completed_display": rewrite.get("completed_display") or "",
            "text_checksum": rewrite.get("text_checksum") or "",
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
                    post.publish_now_url = self.get_publish_now_url(post)
                    post.is_draft = post.status == Post.Status.DRAFT
                    metadata = post.source_metadata if isinstance(getattr(post, "source_metadata", {}), dict) else {}
                    post.source_entries = metadata.get("media", []) if isinstance(metadata, dict) else []
                    rewrite_state = self._serialize_rewrite_state(post)
                    post.rewrite_state = rewrite_state
                    post.rewrite_status = rewrite_state.get("status")
                    post.rewrite_requested_display = rewrite_state.get("requested_display")
                    post.rewrite_completed_display = rewrite_state.get("completed_display")
                self._store_filters_in_session(request, cl)
                remembered = bool(request.session.get(self._filters_session_key()))
            response.context_data.setdefault("request", request)
            response.context_data["session_filters_remembered"] = remembered
            if self._is_cards_partial_request(request):
                return self._render_cards_partial(request, response.context_data)
        return response

    def get_approve_url(self, post):
        return None

    def get_publish_now_url(self, post):
        return None

    def _prepare_for_immediate_publication(self, post: Post) -> None:
        now = timezone.now()
        updated_fields: set[str] = set()
        if post.scheduled_at is None or post.scheduled_at > now:
            post.scheduled_at = now
            updated_fields.add("scheduled_at")
        if post.status == Post.Status.APPROVED:
            post.status = Post.Status.SCHEDULED
            updated_fields.add("status")
        publication = services.mark_publication_requested(post, auto_save=False)
        if publication:
            updated_fields.add("source_metadata")
        if updated_fields:
            post.save(update_fields=sorted(updated_fields))

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
            "rewrite": self._serialize_rewrite_state(post),
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
            services.mark_rewrite_requested(post, prompt=prompt)
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
        allowed_statuses = {Post.Status.APPROVED, Post.Status.SCHEDULED}
        for post in qs:
            if post.status not in allowed_statuses:
                continue
            self._prepare_for_immediate_publication(post)
            publish_post.delay(post.id)

    @admin.action(description="Usuń")
    def act_delete(self, request, qs):
        qs.delete()

@admin.register(DraftPost)
class DraftPostAdmin(BasePostAdmin):
    approve_action = "approve"
    approve_path = "<int:object_id>/akceptuj/"
    gpt_article_action = "gpt_article"
    gpt_article_path = "dodaj-z-gpt/"
    draft_import_action = "import_json"
    draft_import_path = "import-json/"

    def filter_queryset(self, qs):
        return qs.filter(status=Post.Status.DRAFT)

    def get_urls(self):
        urls = super().get_urls()
        opts = self.model._meta
        custom = [
            path(
                self.draft_import_path,
                self.admin_site.admin_view(self.import_view),
                name=f"{opts.app_label}_{opts.model_name}_{self.draft_import_action}",
            ),
            path(
                self.gpt_article_path,
                self.admin_site.admin_view(self.gpt_article_view),
                name=f"{opts.app_label}_{opts.model_name}_{self.gpt_article_action}",
            ),
            path(
                self.approve_path,
                self.admin_site.admin_view(self.approve_view),
                name=f"{opts.app_label}_{opts.model_name}_{self.approve_action}",
            ),
        ]
        return custom + urls

    def changelist_view(self, request, extra_context=None):
        response = super().changelist_view(request, extra_context=extra_context)
        if hasattr(response, "context_data"):
            try:
                response.context_data["gpt_article_url"] = reverse(
                    f"admin:{self.model._meta.app_label}_{self.model._meta.model_name}_{self.gpt_article_action}"
                )
            except NoReverseMatch:
                response.context_data["gpt_article_url"] = None
            try:
                response.context_data["draft_import_url"] = reverse(
                    f"admin:{self.model._meta.app_label}_{self.model._meta.model_name}_{self.draft_import_action}"
                )
            except NoReverseMatch:
                response.context_data["draft_import_url"] = None
        return response

    def get_approve_url(self, post):
        return self._object_url(post, self.approve_action)

    def _resolve_import_channel(
        self,
        entry: dict[str, Any],
        default_channel: Channel | None,
        cache: dict[tuple[str, str | int], Channel],
    ) -> Channel:
        channel_field = entry.get("channel")
        channel_id = entry.get("channel_id")
        channel_slug = entry.get("channel_slug")

        if isinstance(channel_field, dict):
            channel_id = channel_field.get("id", channel_id)
            channel_slug = channel_field.get("slug", channel_slug)
        elif isinstance(channel_field, (int, str)) and str(channel_field).strip():
            if isinstance(channel_field, int) or str(channel_field).strip().isdigit():
                channel_id = channel_field
            else:
                channel_slug = channel_field

        if channel_id is not None:
            try:
                channel_id_int = int(channel_id)
            except (TypeError, ValueError):
                raise ValueError("channel_id musi być liczbą całkowitą.")
            cache_key = ("id", channel_id_int)
            if cache_key not in cache:
                try:
                    cache[cache_key] = Channel.objects.get(id=channel_id_int)
                except Channel.DoesNotExist:
                    raise ValueError(f"Kanał o ID {channel_id_int} nie istnieje.")
            return cache[cache_key]

        if channel_slug:
            slug = str(channel_slug).strip()
            if not slug:
                raise ValueError("channel_slug nie może być pusty.")
            cache_key = ("slug", slug)
            if cache_key not in cache:
                try:
                    cache[cache_key] = Channel.objects.get(slug=slug)
                except Channel.DoesNotExist:
                    raise ValueError(f"Kanał o slugu '{slug}' nie istnieje.")
            return cache[cache_key]

        if default_channel is not None:
            return default_channel

        raise ValueError("Brak kanału – uzupełnij channel_id/channel_slug lub wybierz kanał domyślny.")

    def _extract_import_payload(self, entry: dict[str, Any]) -> dict[str, Any]:
        payload = entry.get("payload")
        if payload is not None:
            if not isinstance(payload, dict):
                raise ValueError("Sekcja payload musi być obiektem JSON.")
            return dict(payload)

        payload = {
            key: value
            for key, value in entry.items()
            if key not in {"channel", "channel_id", "channel_slug"}
        }
        if not payload:
            raise ValueError("Brak danych draftu w rekordzie JSON.")
        if not isinstance(payload, dict):
            raise ValueError("Dane draftu muszą być obiektem JSON.")
        return dict(payload)

    def _absolute_public_url(self, request, url: str) -> str:
        url = (url or "").strip()
        if not url:
            return ""
        if url.startswith("http://") or url.startswith("https://"):
            return url
        try:
            return request.build_absolute_uri(url)
        except Exception:
            return url

    def _store_gpt_attachment(self, request, upload) -> dict[str, str] | None:
        try:
            original_name = getattr(upload, "name", "") or ""
            ext = Path(original_name).suffix or ".bin"
            filename = f"gpt_sources/{uuid4().hex}{ext}"
            stored_path = default_storage.save(filename, upload)
            public_url = default_storage.url(stored_path)
        except Exception:
            logger.exception("Nie udało się zapisać załącznika dla GPT.")
            return None

        absolute_url = self._absolute_public_url(request, public_url)
        media_type = guess_media_type(original_name, getattr(upload, "content_type", ""))
        caption = Path(original_name).name if original_name else ""

        payload: dict[str, str] = {
            "type": media_type,
            "source_url": absolute_url,
        }
        if caption:
            payload["caption"] = caption
        return payload

    def _build_article_payload(self, request, cleaned_data: dict) -> dict[str, Any] | None:
        article: dict[str, Any] = {}
        post_section: dict[str, Any] = {}

        title = (cleaned_data.get("title") or "").strip()
        summary = (cleaned_data.get("summary") or "").strip()
        text = (cleaned_data.get("text") or "").strip()
        source_url = (cleaned_data.get("source_url") or "").strip()

        if title:
            post_section["title"] = title
        if summary:
            post_section["summary"] = summary
        if text:
            post_section["text"] = text
        if source_url:
            post_section["source"] = [{"url": source_url}]

        if post_section:
            article["post"] = post_section

        attachments = cleaned_data.get("attachments") or []
        media_entries: list[dict[str, str]] = []
        for upload in attachments:
            stored = self._store_gpt_attachment(request, upload)
            if stored:
                media_entries.append(stored)

        if media_entries:
            article["media"] = media_entries

        return article or None

    def gpt_article_view(self, request):
        if not self.has_add_permission(request):
            raise PermissionDenied

        form = GptDraftRequestForm(request.POST or None, request.FILES or None)

        if request.method == "POST" and form.is_valid():
            channel: Channel = form.cleaned_data["channel"]
            article_payload = self._build_article_payload(request, form.cleaned_data) or None
            task_gpt_generate_from_article.delay(channel.id, article_payload)
            self.message_user(
                request,
                "Wysłano dane do GPT – odśwież listę, aby zobaczyć nowy draft.",
                level=messages.INFO,
            )
            return redirect(self._changelist_url())

        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": "Nowy draft z GPT",
            "form": form,
            "media": form.media,
            "changelist_url": self._changelist_url(),
        }
        return TemplateResponse(request, "admin/posts/gpt_draft_request.html", context)

    def import_view(self, request):
        if not self.has_add_permission(request):
            raise PermissionDenied

        form = DraftImportForm(request.POST or None, request.FILES or None)

        if request.method == "POST" and form.is_valid():
            entries: list[dict[str, Any]] = form.cleaned_data.get("drafts_file") or []
            default_channel: Channel | None = form.cleaned_data.get("default_channel")
            cache: dict[tuple[str, str | int], Channel] = {}
            created = 0
            errors: list[str] = []

            for index, entry in enumerate(entries, start=1):
                if not isinstance(entry, dict):
                    errors.append(f"#{index}: pominięto – oczekiwano obiektu JSON.")
                    continue
                try:
                    channel = self._resolve_import_channel(entry, default_channel, cache)
                except ValueError as exc:
                    errors.append(f"#{index}: {exc}")
                    continue

                try:
                    payload = self._extract_import_payload(entry)
                except ValueError as exc:
                    errors.append(f"#{index}: {exc}")
                    continue

                try:
                    services.create_post_from_payload(channel, payload)
                except Exception as exc:  # pragma: no cover - defensywne logowanie
                    logger.exception(
                        "Import draftu #%s dla kanału %s nie powiódł się", index, channel.id
                    )
                    errors.append(f"#{index}: błąd tworzenia draftu ({exc})")
                    continue

                created += 1

            if created:
                suffix = "draft" if created == 1 else ("drafty" if created <= 4 else "draftów")
                self.message_user(
                    request,
                    f"Zaimportowano {created} {suffix}.",
                    level=messages.SUCCESS,
                )

            if errors:
                error_preview = format_html_join("<br>", "{}", ((err,) for err in errors[:5]))
                if len(errors) > 5:
                    error_preview = format_html(
                        "{}<br>… (łącznie {} błędów)",
                        error_preview,
                        len(errors),
                    )
                self.message_user(
                    request,
                    format_html("Pominięto część wpisów:<br>{}", error_preview),
                    level=messages.WARNING,
                )

            if not errors:
                return redirect(self._changelist_url())

        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": "Import draftów z JSON",
            "form": form,
            "media": form.media,
            "changelist_url": self._changelist_url(),
            "example_path": "docs/examples/drafts_import_sample.json",
        }
        return TemplateResponse(request, "admin/posts/draft_import.html", context)

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
    publish_now_action = "publish_now"
    publish_now_path = "<int:object_id>/publish-now/"

    def filter_queryset(self, qs):
        return qs.filter(
            status__in=[
                Post.Status.APPROVED,
                Post.Status.SCHEDULED,
            ],
            scheduled_at__isnull=False,
        )

    def get_publish_now_url(self, post):
        return self._object_url(post, self.publish_now_action)

    def get_urls(self):
        urls = super().get_urls()
        opts = self.model._meta
        custom = [
            path(
                self.publish_now_path,
                self.admin_site.admin_view(self.publish_now_view),
                name=f"{opts.app_label}_{opts.model_name}_{self.publish_now_action}",
            ),
        ]
        return custom + urls

    def publish_now_view(self, request, object_id):
        post = get_object_or_404(self.model, pk=object_id)
        if not self.has_change_permission(request, post):
            raise PermissionDenied

        if request.method != "POST":
            return redirect(self._changelist_url())

        if post.status not in {Post.Status.APPROVED, Post.Status.SCHEDULED}:
            self.message_user(
                request,
                "Ten wpis nie może być teraz opublikowany.",
                level=messages.WARNING,
            )
            return redirect(self._changelist_url())

        from .tasks import publish_post

        self._prepare_for_immediate_publication(post)
        publish_post.delay(post.id)
        self.message_user(
            request,
            "Zlecono natychmiastową publikację wpisu.",
            level=messages.SUCCESS,
        )
        return redirect(self._changelist_url())


@admin.register(HistoryPost)
class HistoryPostAdmin(BasePostAdmin):
    change_list_template = "admin/posts/post_history_list.html"
    list_display = (
        "id",
        "channel",
        "published_display",
        "schedule_mode",
        "dupe_score",
        "short",
    )
    list_filter = ("channel", "schedule_mode")
    ordering = ("-published_at", "-id")
    actions = ["act_delete"]
    date_hierarchy = "published_at"
    cards_refresh_interval_ms = 0

    def filter_queryset(self, qs):
        return qs.filter(status=Post.Status.PUBLISHED)

    def has_add_permission(self, request):
        return False

    @admin.display(ordering="published_at", description="Opublikowano")
    def published_display(self, obj: Post) -> str:
        dt = obj.published_at or obj.scheduled_at or obj.created_at
        if not dt:
            return "—"
        if timezone.is_naive(dt):
            return date_format(dt, "d.m.Y H:i")
        return date_format(timezone.localtime(dt), "d.m.Y H:i")


@admin.register(PostMedia)
class PostMediaAdmin(admin.ModelAdmin):
    list_per_page = ADMIN_PAGE_SIZE
    list_display = (
        "id",
        "preview",
        "post_with_channel_display",
        "type",
        "resolver",
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
    def post_with_channel_display(self, obj: PostMedia) -> str:
        post = obj.post
        post_id = getattr(post, "id", None)
        if not post_id:
            return "—"
        channel = getattr(post, "channel", None)
        channel_name = getattr(channel, "name", "?")
        link = self._reverse_post_change_url(post, post_id)
        if not link:
            return format_html("#{} – {}", post_id, channel_name)
        return format_html("<a href='{}'>#{} – {}</a>", link, post_id, channel_name)

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
        reference = obj.reference_data if isinstance(obj.reference_data, dict) else {}
        original = (reference.get("original_url") or reference.get("tg_post_url") or "").strip()
        resolved = (obj.source_url or "").strip()
        parts = []
        if original:
            parts.append(
                format_html(
                    "<a href='{}' target='_blank' rel='noopener'>{}</a>",
                    original,
                    Truncator(original).chars(60),
                )
            )
        if resolved and (not original or resolved != original):
            parts.append(
                format_html(
                    "<a href='{}' target='_blank' rel='noopener'>{}</a>",
                    resolved,
                    Truncator(resolved).chars(60),
                )
            )
        if not parts:
            return "—"
        return format_html_join("<br>", "{}", ((p,) for p in parts))

    @admin.display(description="Powiązane posty")
    def related_posts(self, obj: PostMedia) -> str:
        posts = self._get_related_posts(obj)
        if not posts:
            return "—"
        items = []
        for related in posts:
            label = f"#{related.id} – {getattr(related.channel, 'name', '?')}"
            link = self._reverse_post_change_url(related, related.id)
            if link:
                items.append(format_html("<a href='{}'>{}</a>", link, label))
            else:
                items.append(label)
        return format_html_join("<br>", "{}", ((item,) for item in items))

    def _get_related_posts(self, obj: PostMedia) -> list[Post]:
        ref = obj.reference_data or {}
        ref_url = ""
        if isinstance(ref, dict):
            ref_url = (ref.get("tg_post_url") or ref.get("original_url") or "").strip()
        key = ref_url or (obj.source_url or "").strip()
        if not key:
            return []
        cached = self._related_posts_cache.get(key)
        if cached is not None:
            return [post for post in cached if post.id != obj.post_id]
        qs = Post.objects.select_related("channel").distinct()
        if ref_url:
            qs = qs.filter(
                Q(media__reference_data__contains={"tg_post_url": ref_url})
                | Q(media__reference_data__contains={"original_url": ref_url})
            )
        else:
            qs = qs.filter(media__source_url=obj.source_url)
        posts = list(qs)
        self._related_posts_cache[key] = posts
        return [post for post in posts if post.id != obj.post_id]

    def _reverse_post_change_url(self, post: Post, post_id: int) -> str | None:
        admin_models = [post.__class__, DraftPost, ScheduledPost, Post]
        seen = set()
        for model in admin_models:
            opts = getattr(model, "_meta", None)
            if opts is None:
                continue
            key = (opts.app_label, opts.model_name)
            if key in seen:
                continue
            seen.add(key)
            try:
                return reverse(f"admin:{opts.app_label}_{opts.model_name}_change", args=[post_id])
            except NoReverseMatch:
                continue
        return None
