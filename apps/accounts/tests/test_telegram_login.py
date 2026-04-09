from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import TelegramAccount


class TelegramLoginSecurityTest(TestCase):
    @patch("apps.accounts.views.verify_telegram_auth", return_value=True)
    def test_username_collision_does_not_login_existing_user_without_telegram_link(self, _verify) -> None:
        user_model = get_user_model()
        admin = user_model.objects.create_user(username="admin", password="secret")

        response = self.client.post(
            reverse("tg_login"),
            {
                "id": "123456789",
                "username": "admin",
                "first_name": "Eve",
                "auth_date": "1700000000",
                "hash": "ignored-in-test",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/admin/")

        session_user_id = int(self.client.session["_auth_user_id"])
        self.assertNotEqual(session_user_id, admin.id)

        telegram_user = user_model.objects.get(id=session_user_id)
        self.assertEqual(telegram_user.username, "tg_123456789")
        self.assertEqual(telegram_user.first_name, "Eve")

        telegram_account = TelegramAccount.objects.get(telegram_id=123456789)
        self.assertEqual(telegram_account.user_id, telegram_user.id)
        self.assertEqual(telegram_account.telegram_username, "admin")
