"""Timestamp and duration helpers for the persistent countdown timer."""

from __future__ import annotations

from datetime import datetime, timezone
import re

MIN_DURATION_MINUTES = 1
MAX_DURATION_MINUTES = 24 * 60
REMINDER_DELAY_SECONDS = 5 * 60


def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def new_id() -> str:
    import secrets

    return secrets.token_urlsafe(16)


def parse_duration(value: str) -> int:
    """Accept a whole number of minutes in the supported one-day range."""
    raw = value.strip()
    if not re.fullmatch(r"[0-9]{1,4}", raw):
        raise ValueError("Use a whole number of minutes, from 1 to 1440.")
    minutes = int(raw)
    if not MIN_DURATION_MINUTES <= minutes <= MAX_DURATION_MINUTES:
        raise ValueError("Timer duration must be from 1 to 1440 minutes.")
    return minutes * 60


def callback_data(action: str, timer_id: str, confirmation_id: str) -> str:
    if action not in ("yes", "more"):
        raise ValueError("unsupported callback action")
    value = f"{action}|{timer_id}|{confirmation_id}"
    if len(value.encode("utf-8")) > 64:
        raise ValueError("callback payload exceeds Telegram's limit")
    return value


def parse_callback_data(value: str) -> tuple[str, str, str] | None:
    parts = value.split("|")
    if (
        len(parts) != 3
        or parts[0] not in ("yes", "more")
        or not re.fullmatch(r"[A-Za-z0-9_-]{20,24}", parts[1])
        or not re.fullmatch(r"[A-Za-z0-9_-]{20,24}", parts[2])
    ):
        return None
    return parts[0], parts[1], parts[2]
