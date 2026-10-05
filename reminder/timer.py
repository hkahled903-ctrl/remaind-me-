"""Timer. One cycle per Telegram chat, and the pure rules that move it.

The state machine
-----------------

::

    IDLE      no interval set on this chat
      | activate(interval)          cycle += 1; activated_at = now; stopped = False
      v
    WAITING   due_at = activated_at + interval
      | now >= due_at              first reminder; asked_at = last_sent_at = now
      v
    PENDING   nudges = 0
      | now >= last_sent_at + 5m   nudge; nudges += 1; last_sent_at = now
      v
    PENDING   ... forever, until answered
      |
      +-- "I saved it"  ->  STOPPED   (asked_at = "", stopped = True)
      +-- "Not yet"     ->  PENDING   (unchanged; the cadence continues)

Why one document per chat
-------------------------
Settings and confirmation used to be two global records, so five people shared
one timer: whoever pressed "I saved it" silenced everybody, and the last person
to press Start owned the binding. Both halves of the answer to "did you save it"
belong to the same question, which belongs to one chat, so they belong in one
document. One key means one read, one write and no window in which the timer says
"waiting" while the question says "answered".

Why timestamps, not slots
-------------------------
An earlier version numbered periods from the Unix epoch, so a 15-minute timer
fired on :00/:15/:30/:45 regardless of when the user pressed the button -- and
because the marker was cleared on activation, the very first heartbeat after
pressing sent immediately. `activated_at` plus `interval_minutes` makes the next
due time a fact about this cycle rather than a fact about the wall clock, which
is what the product asks for and what the page's countdown has to agree with.

Everything here is pure except `CycleStore`. The transitions take the moment they
are deciding about, so every boundary in section 16 of the spec is testable
without a fake clock, a network or a filesystem.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Protocol

from .logs import log
from .policy import ALLOWED_INTERVALS

# The interval between follow-up asks. Fixed by the product, never a user choice.
NUDGE_MINUTES = 5

# How long a worker holds the send lock. Longer than the worst-case send
# (20s timeout x 3 attempts) so a slow Telegram call cannot overlap a second
# worker, and far shorter than NUDGE_MINUTES so a crashed worker never delays a
# legitimate beat.
CLAIM_TTL_SECONDS = 90

# Files used when no Redis is configured (a laptop, or CI without credentials).
TIMER_PATH = Path(__file__).resolve().parent.parent / "timer.json"
CHATS_PATH = Path(__file__).resolve().parent.parent / "chats.json"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def parse(value: str | None) -> datetime | None:
    """Read a stored stamp. Naive values are read as UTC, never as host local."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass
class Cycle:
    """Everything the reminder knows about one chat.

    `asked_at` is what makes the question real: empty means no reminder is
    outstanding, which is why "I saved it" can end the cycle by clearing it
    rather than by flipping a second flag that could drift out of step.
    """

    chat_id: str
    interval_minutes: int = 0
    activated_at: str = ""
    stopped: bool = False
    cycle: int = 0
    asked_at: str = ""
    last_sent_at: str = ""
    nudges: int = 0

    # -- derived; never stored, so it cannot disagree with its inputs --------

    @property
    def is_running(self) -> bool:
        return self.interval_minutes > 0 and not self.stopped

    @property
    def pending(self) -> bool:
        return bool(self.asked_at.strip())

    @property
    def due_at(self) -> datetime | None:
        """When the first reminder of this cycle is owed."""
        start = parse(self.activated_at)
        if start is None or self.interval_minutes <= 0:
            return None
        return start + timedelta(minutes=self.interval_minutes)

    @property
    def next_nudge_at(self) -> datetime | None:
        if not self.pending:
            return None
        last = parse(self.last_sent_at) or parse(self.asked_at)
        if last is None:
            return None
        return last + timedelta(minutes=NUDGE_MINUTES)

    @property
    def next_send_at(self) -> datetime | None:
        """The one instant this chat may next be messaged, whatever the reason.

        Before the first reminder that is the activation-relative due time; after
        it, the nudge time. The page countdown and both workers read this, so
        there is exactly one formula in the system.
        """
        if not self.is_running:
            return None
        return self.next_nudge_at if self.pending else self.due_at

    def seconds_until_next_send(self, now: datetime) -> int:
        target = self.next_send_at
        if target is None:
            return 0
        return max(0, int((target - now).total_seconds()))


# -- pure transitions ------------------------------------------------------


def activate(cycle: Cycle, minutes: int, now: datetime) -> Cycle:
    """Start a new cycle. The only way out of STOPPED.

    Everything about the previous question is discarded: a user who sets a new
    time is starting again, and must never inherit the old open question or its
    nudge count. `cycle` increments so a stale claim from the previous cycle can
    be recognised and ignored.
    """
    if minutes not in ALLOWED_INTERVALS:
        raise ValueError(f"interval must be one of {ALLOWED_INTERVALS} minutes")
    return Cycle(
        chat_id=cycle.chat_id,
        interval_minutes=minutes,
        activated_at=_iso(now),
        stopped=False,
        cycle=cycle.cycle + 1,
        asked_at="",
        last_sent_at="",
        nudges=0,
    )


def first_reminder_due(cycle: Cycle, now: datetime) -> bool:
    """Whether this beat owes the first reminder of the current cycle."""
    if not cycle.is_running or cycle.pending:
        return False
    due = cycle.due_at
    return due is not None and now >= due


def nudge_due(cycle: Cycle, now: datetime) -> bool:
    """Whether this beat owes another ask on an already-asked question."""
    if not cycle.is_running or not cycle.pending:
        return False
    due = cycle.next_nudge_at
    return due is not None and now >= due


def mark_sent(cycle: Cycle, now: datetime) -> Cycle:
    """Record that a message went out, and what kind it was.

    Deliberately has no ceiling: the product asks until the user answers, and a
    limit that expires silently turns into a fresh reminder cycle instead of
    silence. `nudges` stays for observability only.
    """
    first = not cycle.pending
    return Cycle(
        chat_id=cycle.chat_id,
        interval_minutes=cycle.interval_minutes,
        activated_at=cycle.activated_at,
        stopped=cycle.stopped,
        cycle=cycle.cycle,
        asked_at=cycle.asked_at or _iso(now),
        last_sent_at=_iso(now),
        nudges=0 if first else cycle.nudges + 1,
    )


def stop_cycle(cycle: Cycle) -> Cycle:
    """End the cycle. Nothing is sent again until `activate` runs."""
    return Cycle(
        chat_id=cycle.chat_id,
        interval_minutes=cycle.interval_minutes,
        activated_at=cycle.activated_at,
        stopped=True,
        cycle=cycle.cycle,
        asked_at="",
        last_sent_at="",
        nudges=0,
    )


def nudge_text(nudges: int) -> str:
    if nudges <= 1:
        return "Still open? Press one of the buttons below."
    return f"Still open ({nudges}). Press one of the buttons below."


# -- storage ---------------------------------------------------------------


def _key(chat_id: str) -> str:
    return f"reminder:user:{chat_id}"


def _claim_key(chat_id: str) -> str:
    return f"reminder:user:{chat_id}:claim"


CHATS_KEY = "reminder:chats"


class CycleStore(Protocol):
    def load(self, chat_id: str) -> Cycle: ...
    def save(self, cycle: Cycle) -> None: ...
    def known_chats(self) -> list[str]: ...
    def remember(self, chat_id: str) -> None: ...
    def claim(self, chat_id: str, cycle: int) -> bool: ...
    def release(self, chat_id: str) -> None: ...


def _from_raw(chat_id: str, raw) -> Cycle:
    if not isinstance(raw, dict):
        return Cycle(chat_id=chat_id)
    return Cycle(
        chat_id=str(raw.get("chat_id") or chat_id),
        interval_minutes=int(raw.get("interval_minutes", 0) or 0),
        activated_at=str(raw.get("activated_at") or ""),
        stopped=bool(raw.get("stopped", False)),
        cycle=int(raw.get("cycle", 0) or 0),
        asked_at=str(raw.get("asked_at") or ""),
        last_sent_at=str(raw.get("last_sent_at") or ""),
        nudges=int(raw.get("nudges", 0) or 0),
    )


class FileCycleStore:
    """One JSON file holding every chat's cycle. A laptop's answer to Redis."""

    def __init__(self, path: Path = TIMER_PATH):
        self._path = path

    def _read(self) -> dict:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _write(self, data: dict) -> None:
        tmp = self._path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as exc:
            log(f"could not write {self._path.name}: {exc}")

    def load(self, chat_id: str) -> Cycle:
        return _from_raw(chat_id, self._read().get(chat_id))

    def save(self, cycle: Cycle) -> None:
        data = self._read()
        data[cycle.chat_id] = asdict(cycle)
        self._write(data)

    def known_chats(self) -> list[str]:
        return sorted(self._read())

    def remember(self, chat_id: str) -> None:
        data = self._read()
        if chat_id in data:
            return
        data[chat_id] = asdict(Cycle(chat_id=chat_id))
        self._write(data)

    def claim(self, chat_id: str, cycle: int) -> bool:
        # A single local process is the whole world here, so there is nothing to
        # race against; the method exists to keep the interface honest.
        return True

    def release(self, chat_id: str) -> None:
        return None


class UpstashCycleStore:
    """The same records in Redis, one key per chat plus a chat index.

    `claim` is the duplicate-send guard. `SET NX` is the only atomic
    compare-and-set in stock Redis and it is all this needs: whichever worker
    creates the key owns this beat, and the other exits without sending. The key
    expires, so a worker that dies mid-send cannot lock its chat out forever.
    """

    def __init__(self, url: str, token: str):
        self._url = url.rstrip("/")
        self._token = token

    def command(self, *args: str):
        import urllib.error
        import urllib.request

        request = urllib.request.Request(
            self._url,
            data=json.dumps(list(args)).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"timer store returned HTTP {exc.code}") from None
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"timer store unreachable: {exc}") from None
        if isinstance(body, dict) and body.get("error"):
            raise RuntimeError(f"timer store rejected the command: {body['error']}")
        return body.get("result") if isinstance(body, dict) else None

    def load(self, chat_id: str) -> Cycle:
        raw = self.command("GET", _key(chat_id))
        if not raw:
            return Cycle(chat_id=chat_id)
        try:
            return _from_raw(chat_id, json.loads(raw))
        except (TypeError, ValueError):
            log("WARNING: timer store holds unreadable data; treating it as idle.")
            return Cycle(chat_id=chat_id)

    def save(self, cycle: Cycle) -> None:
        self.command("SET", _key(cycle.chat_id), json.dumps(asdict(cycle)))

    def known_chats(self) -> list[str]:
        try:
            members = self.command("SMEMBERS", CHATS_KEY) or []
        except RuntimeError:
            return []
        return sorted(str(m) for m in members if str(m))

    def remember(self, chat_id: str) -> None:
        self.command("SADD", CHATS_KEY, chat_id)

    def claim(self, chat_id: str, cycle: int) -> bool:
        result = self.command(
            "SET", _claim_key(chat_id), str(cycle), "NX", "EX", str(CLAIM_TTL_SECONDS)
        )
        return result is not None

    def release(self, chat_id: str) -> None:
        try:
            self.command("DEL", _claim_key(chat_id))
        except RuntimeError:
            pass


def cycle_store_from_env(env: dict | None = None) -> CycleStore:
    env = os.environ if env is None else env
    url = env.get("UPSTASH_REDIS_REST_URL", "").strip()
    token = env.get("UPSTASH_REDIS_REST_TOKEN", "").strip()
    if url and token:
        return UpstashCycleStore(url, token)
    return FileCycleStore()


def serve_cycles(store: CycleStore, now: datetime, chat_ids: Iterable[str] | None = None):
    """Yield every cycle this beat owes a message for, in chat order.

    Pure selection: it says which chats are due, never that a message went out.
    The caller claims, sends and records, so this stays testable on its own.
    """
    chats = list(chat_ids) if chat_ids is not None else store.known_chats()
    due = []
    for chat_id in sorted(chats):
        cycle = store.load(chat_id)
        if first_reminder_due(cycle, now) or nudge_due(cycle, now):
            due.append(cycle)
    return due