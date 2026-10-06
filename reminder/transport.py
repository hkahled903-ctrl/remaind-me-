"""Minimal Telegram Bot API transport."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

API_BASE = "https://api.telegram.org/bot{token}/{method}"


class TelegramError(RuntimeError):
    """Telegram rejected a request or could not be reached."""


class Telegram:
    def __init__(self, token: str, api_base: str = API_BASE):
        if not token.strip():
            raise ValueError("TELEGRAM_BOT_TOKEN is required")
        self._token = token.strip()
        self._api_base = api_base

    def call(self, method: str, payload: dict | None = None) -> dict:
        fields = {
            key: (
                json.dumps(value)
                if isinstance(value, (dict, list))
                else str(value).lower()
                if isinstance(value, bool)
                else str(value)
            )
            for key, value in (payload or {}).items()
        }
        request = urllib.request.Request(
            self._api_base.format(token=self._token, method=method),
            data=urllib.parse.urlencode(fields).encode("utf-8"),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise TelegramError(f"Telegram returned HTTP {exc.code}") from None
        except (OSError, ValueError) as exc:
            raise TelegramError(f"Telegram request failed: {exc}") from None
        if not isinstance(result, dict) or result.get("ok") is not True:
            description = result.get("description", "invalid Telegram response") if isinstance(result, dict) else "invalid Telegram response"
            raise TelegramError(f"Telegram rejected the request: {description}")
        return result

    def send_message(self, chat_id: str, text: str, reply_markup: dict | None = None) -> dict:
        payload = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return self.call("sendMessage", payload)

    def answer_callback(self, callback_id: str, text: str = "", alert: bool = False) -> dict:
        payload = {"callback_query_id": callback_id}
        if text:
            payload.update(text=text, show_alert=alert)
        return self.call("answerCallbackQuery", payload)

    def set_webhook(self, public_url: str, secret: str) -> dict:
        return self.call(
            "setWebhook",
            {
                "url": f"{public_url.rstrip('/')}/telegram/webhook",
                "secret_token": secret.strip(),
                "allowed_updates": ["message", "callback_query"],
            },
        )
