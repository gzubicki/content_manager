from __future__ import annotations

from django.contrib.auth.models import User
from django.db import models


class TelegramAccount(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="telegram_accounts")
    telegram_id = models.BigIntegerField(unique=True)
    telegram_username = models.CharField(max_length=255, blank=True)
    telegram_auth_date = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"{self.user.username} <-> {self.telegram_id}"
