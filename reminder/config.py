"""Configuration. Owns where things live and whether the settings are valid.

This module never decides *when* to send, and never calls the network.
Validation failures raise ConfigError so the CLI owns the exit code.
"""

from __future__ import annotations

import json
import os
from datetime import timezone
from pathlib import Path

from .logs import log

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state.json"

DEFAULT_CONFIG = {
    "telegram_chat_id": "",
    "reminder_time": "09:00",
    "timezone": "Africa/Cairo",
    "message": "Reminder: your daily report is due.",
}


class ConfigError(Exception):
    """Settings are missing or malformed."""


def load_config(path: Path = CONFIG_PATH) -> dict:
    """Read settings, falling back to defaults for keys the file omits."""
    config = dict(DEFAULT_CONFIG)
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConfigError(f"could not read {path.name}: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ConfigError(f"{path.name} must contain a JSON object")
        config.update(loaded)
    return config


def parse_hhmm(value: str) -> tuple[int, int]:
    """Parse 'HH:MM' into (hour, minute). Raises ConfigError."""
    try:
        hour, minute = value.strip().split(":")
        hour, minute = int(hour), int(minute)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("out of range")
    except ValueError as exc:
        raise ConfigError(
            f"reminder_time {value!r} is not valid; expected HH:MM like '09:00'"
        ) from exc
    return hour, minute


def resolve_timezone(name: str):
    """Return the tzinfo for `name`.

    Falls back to UTC when the platform has no tz database (common on Windows
    without `pip install tzdata`). The fallback is preserved so a reminder is
    never silently dropped, but it is logged loudly because it shifts the hour.
    """
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception as exc:
        log(f"WARNING: cannot load timezone {name!r} ({exc}).")
        log("WARNING: falling back to UTC -- the reminder will fire at the wrong hour.")
        log("WARNING: fix with: pip install tzdata")
        return timezone.utc


def token_from_env() -> str:
    """Read the bot token from the environment. Raises ConfigError if unset."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise ConfigError(
            "TELEGRAM_BOT_TOKEN is not set. Put it in the environment "
            "(GitHub: Settings > Secrets and variables > Actions > "
            "TELEGRAM_BOT_TOKEN)."
        )
    return token


def chat_id_from(config: dict) -> str:
    """Read a usable chat id out of settings. Raises ConfigError if there is none."""
    chat_id = str(config.get("telegram_chat_id", "")).strip()
    if not chat_id:
        raise ConfigError("telegram_chat_id is empty. Run --whoami to find it.")
    return chat_id
