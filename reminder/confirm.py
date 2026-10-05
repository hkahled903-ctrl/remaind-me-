"""Confirm. The question after the reminder: "did you save it?"

The shape of the feature
------------------------
A reminder is sent with two buttons. Until the user presses one, the bot asks
again every few minutes. Pressing "I saved" stops everything. Until the user sets
a new time, nothing is sent at all -- the timer is off, not paused-and-forgetting.

That last part is the whole design: `pending is None` is a real, resting state
rather than a missing value. A user who finished their report should not be
pestered, and should not have to press a second button to stop being pestered.

Why this is its own module
--------------------------
The rules here are pure and clock-injected, so every boundary is testable without
a fake clock or a network. It knows nothing about Telegram's wire format (that is
`transport`) or about HTTP (that is `webapp`).

Depends on `logs`, `binding` (for the durable store) and `policy`.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from .binding import UpstashStore
from .logs import log

PENDING_PATH = Path(__file__).resolve().parent.parent / "confirm.json"
REDIS_KEY = "reminder:confirm"

# How long to keep asking before giving up on a silent user. Without a ceiling a
# chat that never answers would nag forever, which is worse than not asking.
MAX_NUDGES = 12

# The two answers, as they travel in a Telegram callback payload. Kept short
# because Telegram caps `callback_data` at 64 bytes.
SAVED = "saved"
NOT_YET = "not_yet"


@dataclass
class Pending:
    """One open question: has this chat saved the report yet?

    `asked_at` is when the first question went out, so the nudge schedule is
    anchored to the original ask rather than drifting forward on every retry.
    """

    chat_id: str
    asked_at: str = ""
    last_asked_at: str = ""
    nudges: int = 0

    @property
    def is_active(self) -> bool:
        return bool(self.chat_id.strip())


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def _parse(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class ConfirmStore(Protocol):
    def load(self) -> Pending | None: ...
    def save(self, pending: Pending) -> None: ...
    def clear(self) -> None: ...


class FileConfirm:
    """One JSON file, written atomically. Right on a laptop."""

    def __init__(self, path: Path = PENDING_PATH):
        self._path = path

    def load(self) -> Pending | None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict):
            return None
        pending = Pending(
            chat_id=str(raw.get("chat_id", "")).strip(),
            asked_at=str(raw.get("asked_at", "")),
            last_asked_at=str(raw.get("last_asked_at", "")),
            nudges=int(raw.get("nudges", 0) or 0),
        )
        return pending if pending.is_active else None

    def save(self, pending: Pending) -> None:
        tmp = self._path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(asdict(pending), indent=2), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as exc:
            log(f"could not write {self._path.name}: {exc}")
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def clear(self) -> None:
        try:
            self._path.unlink(missing_ok=True)
        except OSError as exc:
            log(f"could not remove {self._path.name}: {exc}")


class UpstashConfirm(UpstashStore):
    """The same record in Upstash, so it survives a redeploy like the rest."""

    def __init__(self, url: str, token: str, key: str = REDIS_KEY):
        super().__init__(url, token, key=key)

    def command(self, *args: str):
        return self._command(*args)

    def _pending(self) -> Pending | None:
        raw = self.command("GET", self._key)
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            log("WARNING: confirmation store holds unreadable data; treating as clear.")
            return None
        pending = Pending(
            chat_id=str(data.get("chat_id", "")).strip(),
            asked_at=str(data.get("asked_at", "")),
            last_asked_at=str(data.get("last_asked_at", "")),
            nudges=int(data.get("nudges", 0) or 0),
        )
        return pending if pending.is_active else None

    def load(self) -> Pending | None:
        return self._pending()

    def save(self, pending: Pending) -> None:
        self.command("SET", self._key, json.dumps(asdict(pending)))

    def clear(self) -> None:
        self.command("DEL", self._key)


def confirm_store_from_env(env: dict | None = None) -> ConfirmStore:
    """Upstash when both credentials exist, otherwise a local file."""
    env = os.environ if env is None else env
    url = env.get("UPSTASH_REDIS_REST_URL", "").strip()
    token = env.get("UPSTASH_REDIS_REST_TOKEN", "").strip()
    if url and token:
        return UpstashConfirm(url, token)
    return FileConfirm()


def ask(chat_id: str, now: datetime) -> Pending:
    """Open a question. Re-asking does not restart the clock."""
    return Pending(chat_id=chat_id, asked_at=_iso(now), last_asked_at=_iso(now), nudges=0)


def is_due(pending: Pending | None, now: datetime, every_minutes: int) -> bool:
    """True when this chat should be asked again right now.

    Anchored to `last_asked_at`, so a runner that fires every 5 minutes against a
    15-minute setting stays quiet instead of asking on every beat.
    """
    if pending is None or not pending.is_active:
        return False
    if pending.nudges >= MAX_NUDGES:
        return False
    last = _parse(pending.last_asked_at) or _parse(pending.asked_at)
    if last is None:
        return True  # no usable stamp: better to ask than to go silent forever
    return now >= last + timedelta(minutes=max(1, every_minutes))


def record_nudge(pending: Pending, now: datetime) -> Pending:
    """Count the nudge and reset the wait. Returns the record to store."""
    return Pending(
        chat_id=pending.chat_id,
        asked_at=pending.asked_at or _iso(now),
        last_asked_at=_iso(now),
        nudges=pending.nudges + 1,
    )


def resolve(action: str) -> bool:
    """Whether an answer stops the timer. Anything unrecognised does not."""
    return action == SAVED


def seconds_until_next_ask(pending: Pending | None, now: datetime, every_minutes: int) -> int:
    """For the page: how long until the next question. 0 means "ask now"."""
    if pending is None or not pending.is_active:
        return 0
    if pending.nudges >= MAX_NUDGES:
        return 0
    last = _parse(pending.last_asked_at) or _parse(pending.asked_at)
    if last is None:
        return 0
    return max(0, int((last + timedelta(minutes=max(1, every_minutes)) - now).total_seconds()))


# -- Telegram wire format ---------------------------------------------------
# Kept here rather than in `transport` because this is the one place that knows
# what the question looks like on a phone.

SAVED_LABEL = "I saved it"
NOT_YET_LABEL = "Not yet"


def reminder_keyboard() -> dict:
    """The inline keyboard under a reminder."""
    return {
        "inline_keyboard": [
            [
                {"text": SAVED_LABEL, "callback_data": SAVED},
                {"text": NOT_YET_LABEL, "callback_data": NOT_YET},
            ]
        ]
    }


def nudge_text(nudge_number: int) -> str:
    """The follow-up message. Says how many times, so it never feels broken."""
    if nudge_number <= 1:
        return "Still open? Press one of the buttons below."
    return f"Still open ({nudge_number}). Press one of the buttons below."