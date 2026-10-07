"""Telegram commands and confirmation callbacks."""

from __future__ import annotations

import logging

from .storage import TimerStore
from .timer import callback_data, new_id, now_ms, parse_callback_data, parse_duration
from .transport import Telegram

log = logging.getLogger("timerbot")


class TimerBot:
    def __init__(self, store: TimerStore, telegram: Telegram):
        self.store = store
        self.telegram = telegram

    def handle_update(self, update: dict) -> tuple[int, dict]:
        query = update.get("callback_query")
        if isinstance(query, dict):
            return self._callback(query)
        message = update.get("message")
        if not isinstance(message, dict):
            return 200, {"ok": True}
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id", ""))
        if chat.get("type") != "private" or not chat_id:
            return 200, {"ok": True}
        text = str(message.get("text", "")).strip()
        command = text.split(maxsplit=1)[0].split("@", 1)[0] if text else ""
        parts = text.split(maxsplit=1)
        if command == "/start" and len(parts) == 2:
            return self._website_link(chat_id, parts[1])
        if command == "/start" or command == "/help":
            self.telegram.send_message(
                chat_id,
                "I can remind you when a timer ends.\n"
                "Start one with /timer 30 (minutes, from 1 to 1440).",
            )
            return 200, {"ok": True}
        if command == "/timer":
            update_id = update.get("update_id")
            dedupe_id = str(update_id) if isinstance(update_id, int) and not isinstance(update_id, bool) else ""
            return self._start_timer(chat_id, text, dedupe_id)
        if command == "/status":
            return self._status(chat_id)
        return 200, {"ok": True}

    def _start_timer(self, chat_id: str, text: str, update_id: str) -> tuple[int, dict]:
        parts = text.split()
        if len(parts) != 2:
            self.telegram.send_message(chat_id, "Use /timer 30 with a duration in minutes.")
            return 200, {"ok": True, "started": False}
        try:
            duration_seconds = parse_duration(parts[1])
        except ValueError as exc:
            self.telegram.send_message(chat_id, str(exc))
            return 200, {"ok": True, "started": False}
        result = self.start_timer(chat_id, duration_seconds, update_id)
        if result == "duplicate":
            return 200, {"ok": True, "started": False, "duplicate": True}
        if result == "active":
            self.telegram.send_message(
                chat_id, "You already have a timer running. It must finish before starting another."
            )
            return 200, {"ok": True, "started": False}
        return 200, {"ok": True, "started": True}

    def start_timer(self, chat_id: str, duration_seconds: int, update_id: str = "") -> str:
        moment = now_ms()
        started = self.store.start(chat_id, duration_seconds, moment, new_id(), update_id)
        if started == -1:
            log.info("duplicate Telegram update ignored chat_id=%s update_id=%s", chat_id, update_id)
            return "duplicate"
        if started == 0:
            return "active"
        log.info("timer started chat_id=%s duration_seconds=%s", chat_id, duration_seconds)
        self.telegram.send_message(
            chat_id, f"Timer started for {duration_seconds // 60} minutes."
        )
        return "started"

    def _website_link(self, chat_id: str, link_token: str) -> tuple[int, dict]:
        result = self.store.complete_website_link(link_token, chat_id)
        if result in ("linked", "duplicate"):
            self.telegram.send_message(
                chat_id, "Telegram is connected. Return to the website to start your timer."
            )
        else:
            self.telegram.send_message(
                chat_id, "That website link is invalid or expired. Connect Telegram again."
            )
        return 200, {"ok": True}

    def _status(self, chat_id: str) -> tuple[int, dict]:
        timer = self.store.get(chat_id)
        state = timer.get("state", "IDLE") if timer else "IDLE"
        if state == "IDLE":
            text = "No timer is active."
        elif state == "RUNNING":
            text = f"Timer running for {timer['duration_seconds']} seconds total."
        else:
            text = "Waiting for your answer: did you save it?"
        self.telegram.send_message(chat_id, text)
        return 200, {"ok": True, "state": state}

    def _callback(self, query: dict) -> tuple[int, dict]:
        callback_id = str(query.get("id", ""))
        raw_data = str(query.get("data", ""))
        parsed = parse_callback_data(raw_data)
        message = query.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id", ""))
        sender_id = str((query.get("from") or {}).get("id", ""))
        if not callback_id or not parsed or chat.get("type") != "private" or chat_id != sender_id:
            if callback_id:
                self.telegram.answer_callback(callback_id, "This button is not valid.", True)
            return 200, {"ok": True, "changed": False}

        action, timer_id, confirmation_id = parsed
        result = self.store.callback(
            chat_id, action, timer_id, confirmation_id, now_ms(), new_id()
        )
        if result == "busy":
            return 503, {"error": "timer notification is still being delivered; retry callback"}
        if result == "completed":
            self.telegram.answer_callback(callback_id, "Saved. No more reminders.")
            log.info("task completed chat_id=%s timer_id=%s", chat_id, timer_id)
            return 200, {"ok": True, "changed": True, "state": "IDLE"}
        if result == "restarted":
            self.telegram.answer_callback(callback_id, "More time: your timer has restarted.")
            log.info("more time requested chat_id=%s previous_timer_id=%s", chat_id, timer_id)
            return 200, {"ok": True, "changed": True, "state": "RUNNING"}
        self.telegram.answer_callback(callback_id, "This reminder is no longer active.", True)
        return 200, {"ok": True, "changed": False, "state": result}


def confirmation_keyboard(timer_id: str, confirmation_id: str) -> dict:
    return {
        "inline_keyboard": [
            [{"text": "✅ Yes, I saved it", "callback_data": callback_data("yes", timer_id, confirmation_id)}],
            [{"text": "⏱️ Give me more time", "callback_data": callback_data("more", timer_id, confirmation_id)}],
        ]
    }


def register_webhook(public_url: str, secret: str, telegram: Telegram) -> None:
    telegram.set_webhook(public_url, secret)
    log.info("Telegram webhook registered")
