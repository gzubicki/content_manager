from django.contrib import admin
from django import forms
from .models import Post, PostMedia, Channel
from .validators import validate_post_text_for_channel
from . import services

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
    actions = ["act_fill_to_20"]
    
    @admin.action(description="Uzupełnij do 20 (zaznaczone kanały)")
    def act_fill_to_20(self, request, queryset):
        total = 0
        for ch in queryset:
            total += services.ensure_min_drafts(ch)
        self.message_user(request, f"Dodano {total} draftów")

@admin.register(Post)
class PostAdmin(admin.ModelAdmin):
    form = PostForm
    list_display = ("id","channel","status","scheduled_at","created_at","dupe_score","short")
    list_filter = ("channel","status","schedule_mode")
    actions = ["act_fill_to_20","act_approve","act_schedule","act_publish_now","act_delete"]

    def short(self, obj): return obj.text[:80] + ("…" if len(obj.text)>80 else "")

    @admin.action(description="Uzupełnij do 20 (bieżący kanał)")
    def act_fill_to_20(self, request, qs):
        channels = {p.channel for p in qs} or set(Channel.objects.all())
        added = 0
        for ch in channels:
            added += services.ensure_min_drafts(ch)
        self.message_user(request, f"Dodano {added} draftów")

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

@admin.register(PostMedia)
class PostMediaAdmin(admin.ModelAdmin):
    list_display = ("id","post","type","order","has_spoiler","cache_path","tg_file_id")
