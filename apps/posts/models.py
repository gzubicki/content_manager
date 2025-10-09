from django.db import models
from django.utils import timezone

class Channel(models.Model):
    name = models.CharField("Nazwa", max_length=64)
    slug = models.SlugField("Slug", unique=True)
    tg_channel_id = models.CharField("ID kanału Telegram", max_length=128)
    bot_token = models.CharField("Token bota", max_length=128, blank=True, default="")
    # styl/limity
    language = models.CharField("Język", max_length=8, default="pl")
    max_chars = models.IntegerField("Maks. liczba znaków", default=1000)
    emoji_min = models.IntegerField("Min. liczba emoji", default=1)
    emoji_max = models.IntegerField("Maks. liczba emoji", default=6)
    footer_text = models.TextField("Stopka (2 linie)", default="🇵🇱 t.me/sztuka_wojny\n💬 @sztukawojny")
    no_links_in_text = models.BooleanField("Bez linków w treści", default=True)
    auto_blur_default = models.BooleanField("Domyślny blur (spoiler)", default=True)
    draft_target_count = models.IntegerField("Docelowa liczba draftów", default=20)
    draft_ttl_days = models.IntegerField("Czas życia draftu (dni)", default=3)
    # sloty
    slot_step_min = models.IntegerField("Krok slotów (minuty)", default=30)
    slot_start_hour = models.IntegerField("Godzina startu", default=6)
    slot_end_hour = models.IntegerField("Godzina końca", default=23)
    slot_end_minute = models.IntegerField("Minuta końca", default=30)
    # prompt
    style_prompt = models.TextField("Prompt stylu (PL)", default=(
        "Jesteś redaktorem polskojęzycznego kanału informacyjnego. Piszesz TYLKO po polsku (UTF-8). "
        "Styl: krótko, wojskowo, rzeczowo; 1–3 akapity z liczbami i faktami; ≤1000 znaków; 1–3 emoji; "
        "bez linków/hashtagów w treści; na końcu stopka w 2 liniach."
    ))

    class Meta:
        verbose_name = "Kanał"
        verbose_name_plural = "Kanały"

    def __str__(self): return self.name


class ChannelSource(models.Model):
    channel = models.ForeignKey(
        Channel,
        verbose_name="Kanał",
        related_name="sources",
        on_delete=models.CASCADE,
    )
    name = models.CharField("Nazwa", max_length=128)
    url = models.URLField("Adres URL", max_length=500)
    priority = models.PositiveIntegerField("Priorytet", default=1)
    is_active = models.BooleanField("Aktywne", default=True)
    created_at = models.DateTimeField("Utworzono", auto_now_add=True)
    updated_at = models.DateTimeField("Zmieniono", auto_now=True)

    class Meta:
        verbose_name = "Źródło kanału"
        verbose_name_plural = "Źródła kanału"
        ordering = ("-is_active", "-priority", "name")
        constraints = [
            models.UniqueConstraint(
                fields=["channel", "url"],
                name="posts_channelsource_unique_channel_url",
            )
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.url})"

class Post(models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "DRAFT"
        APPROVED = "APPROVED", "APPROVED"
        SCHEDULED = "SCHEDULED", "SCHEDULED"
        PUBLISHED = "PUBLISHED", "PUBLISHED"
        REJECTED = "REJECTED", "REJECTED"

    STATUS = Status.choices
    channel = models.ForeignKey(Channel, verbose_name="Kanał", related_name="posts", on_delete=models.CASCADE)
    text = models.TextField("Treść")
    status = models.CharField(
        "Status",
        max_length=16,
        choices=STATUS,
        default=Status.DRAFT,
    )
    scheduled_at = models.DateTimeField("Zaplanowano na", null=True, blank=True)
    schedule_mode = models.CharField("Tryb planowania", max_length=6, choices=[("AUTO","AUTO"),("MANUAL","MANUAL")], default="AUTO")
    created_at = models.DateTimeField("Utworzono", auto_now_add=True)
    approved_by = models.ForeignKey("auth.User", verbose_name="Zatwierdził", null=True, blank=True, on_delete=models.SET_NULL)
    dupe_score = models.FloatField("Podobieństwo (duplikat)", null=True, blank=True)
    origin = models.CharField("Pochodzenie", max_length=8, default="gpt")
    source_url = models.URLField("Źródło treści", max_length=500, blank=True, default="")
    generated_prompt = models.TextField("Prompt generujący", blank=True, default="")
    expires_at = models.DateTimeField("Wygasa", null=True, blank=True)
    message_id = models.BigIntegerField("ID wiadomości (tekst)", null=True, blank=True)
    source_metadata = models.JSONField("Metadane źródłowe", blank=True, default=dict)

    class Meta:
        verbose_name = "Wpis"
        verbose_name_plural = "Wpisy"

    def save(self, *a, **kw):
        if self.status == self.Status.APPROVED and self.scheduled_at:
            self.status = self.Status.SCHEDULED
        if self.status == self.Status.DRAFT and not self.expires_at:
            ttl = getattr(self.channel, "draft_ttl_days", 3)
            self.expires_at = timezone.now() + timezone.timedelta(days=ttl)
        super().save(*a, **kw)


class DraftPost(Post):
    class Meta:
        proxy = True
        verbose_name = "Draft"
        verbose_name_plural = "Drafty"


class ScheduledPost(Post):
    class Meta:
        proxy = True
        verbose_name = "Pozycja harmonogramu"
        verbose_name_plural = "Harmonogram"


class PostMedia(models.Model):
    TYPE = [(t,t) for t in ["photo","video","doc"]]
    post = models.ForeignKey(Post, verbose_name="Wpis", related_name="media", on_delete=models.CASCADE)
    type = models.CharField("Typ", max_length=8, choices=TYPE)
    source_url = models.TextField("Źródłowy URL", blank=True, default="")
    resolver = models.CharField("Resolver", max_length=64, blank=True, default="")
    reference_data = models.JSONField("Dane źródła", blank=True, default=dict)
    cache_path = models.TextField("Ścieżka cache", blank=True, default="")
    tg_file_id = models.TextField("Telegram file_id", blank=True, default="")
    order = models.PositiveIntegerField("Kolejność", default=0)
    has_spoiler = models.BooleanField("Ukryj (spoiler)", default=False)
    created_at = models.DateTimeField("Utworzono", auto_now_add=True)
    expires_at = models.DateTimeField("Wygasa", null=True, blank=True)

    class Meta:
        ordering = ["order", "id"]
        verbose_name = "Medium wpisu"
        verbose_name_plural = "Media wpisu"
