from django.contrib import admin, messages
from django.contrib.admin.helpers import ActionForm as AdminActionForm
from django import forms
from .models import Post, PostMedia, Channel, DraftPost, ScheduledPost
from . import services
from .validators import validate_post_text_for_channel
from .tasks import task_gpt_rewrite_post, task_gpt_generate_for_channel



class PostActionForm(AdminActionForm):
    prompt = forms.CharField(label="Prompt korekty (opcjonalny)", required=False)

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

    def short(self, obj): return obj.text[:80] + ("…" if len(obj.text)>80 else "")

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return self.filter_queryset(qs)

    def filter_queryset(self, qs):
        return qs

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
