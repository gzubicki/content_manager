from pathlib import Path

from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.contrib.admin import helpers
from django.contrib.admin.helpers import ActionForm as AdminActionForm
from django.contrib.admin.widgets import AdminSplitDateTime
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone

from . import services
from .models import Channel, DraftPost, Post, PostMedia, ScheduledPost
from .tasks import task_gpt_generate_for_channel, task_gpt_rewrite_post
from .validators import validate_post_text_for_channel



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
    reschedule_template = "admin/posts/reschedule.html"

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
        cache_path = (media.cache_path or "").strip()
        if cache_path:
            media_root = Path(settings.MEDIA_ROOT).resolve()
            try:
                rel = Path(cache_path).resolve().relative_to(media_root)
                return settings.MEDIA_URL.rstrip("/") + "/" + rel.as_posix()
            except ValueError:
                pass
        src = (media.source_url or "").strip()
        return src

    def _build_preview_media(self, post: Post) -> list[str]:
        urls: list[str] = []
        media_manager = getattr(post, "media", None)
        if media_manager is None:
            return urls
        for media in media_manager.all():
            url = self._media_to_url(media)
            if url:
                urls.append(url)
        return urls

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
