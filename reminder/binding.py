"""Binding. Owns the one question the reminder cannot answer without: who is it for?

`config.json` holds a static `telegram_chat_id`. That is fine until the chat id
changes, until you use a second device, or until you simply do not know the number
yet -- which is the normal state on first run. A Binding is the answer to the same
question, discovered by the user pressing Start in Telegram rather than copied by
hand.

Depends on `logs` only. Knows nothing about HTTP servers, Telegram update shapes
or schedules -- that is `webapp`'s job. Two backends so the same code runs on a
laptop (one JSON file) and on a host that has no disk worth trusting (Upstash REST).

Standard library only, like the rest of the runtime.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from .logs import log

BASE_DIR = Path(__file__).resolve().parent.parent
BINDING_PATH = BASE_DIR / "binding.json"

# Redis key for the hosted backend. Namespaced so it cannot collide with anything
# else that may later share this database.
REDIS_KEY = "reminder:binding"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Binding:
    """A chat that has been proven to accept messages from this bot.

    `bound_at` is not decoration: it is how a status poll started before the user
    pressed Start can tell the difference between "not yet" and "already". It is
    UTC and monotonic per machine, which is all that is needed to compare two
    moments from the same clock.
    """

    chat_id: str
    username: str = ""
    display_name: str = ""
    bound_at: str = ""

    @property
    def label(self) -> str:
        return self.username or self.display_name or self.chat_id


def binding_from_update(chat: dict) -> Binding:
    """Build a Binding from the `chat` block of a Telegram update.

    Only the fields Telegram actually sends are read. A bot can be added to a
    group, and a group id is a negative number; nothing here rejects that, because
    whether a group is a valid destination is the caller's decision, not ours.
    """
    return Binding(
        chat_id=str(chat.get("id", "")).strip(),
        username=str(chat.get("username", "") or ""),
        display_name=" ".join(
            part
            for part in (chat.get("first_name", ""), chat.get("last_name", ""))
            if part
        ).strip(),
        bound_at=_now_iso(),
    )


class BindingStore(Protocol):
    """Where a Binding lives between the moment it is discovered and the moment
    a reminder needs it. Two implementations, same three methods."""

    def load(self) -> Binding | None: ...
    def save(self, binding: Binding) -> None: ...
    def clear(self) -> None: ...


class FileStore:
    """A Binding in one JSON file. Correct only where the disk survives.

    Write is atomic via a temp file + replace, because the process that discovers
    the binding is not the process that later reads it, and a half-written file
    read at the wrong moment would look like "no chat bound" -- the exact failure
    this module exists to prevent.
    """

    def __init__(self, path: Path = BINDING_PATH):
        self._path = path

    def load(self) -> Binding | None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None  # absent or corrupt: indistinguishable, both mean "unbound"
        chat_id = str(raw.get("chat_id", "")).strip()
        if not chat_id:
            return None
        return Binding(
            chat_id=chat_id,
            username=str(raw.get("username", "")),
            display_name=str(raw.get("display_name", "")),
            bound_at=str(raw.get("bound_at", "")),
        )

    def save(self, binding: Binding) -> None:
        tmp = self._path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(asdict(binding), indent=2), encoding="utf-8")
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


class UpstashStore:
    """A Binding in Upstash Redis, over plain HTTPS.

    Chosen because it is the one store that is reachable from both sides of this
    system without a network to speak of: the hosted web app and a GitHub Actions
    runner, which is where the 09:00 send actually happens. A Postgres URL would
    need a client library; this is a REST GET and a REST SET.

    A store that is unreachable raises, because "I could not check whether you are
    connected" and "you are not connected" must never look the same. The send path
    catches the error and falls back, so an outage cannot silently stop reminders.
    """

    def __init__(self, url: str, token: str, key: str = REDIS_KEY, ttl: int | None = None):
        self._url = url.rstrip("/")
        self._token = token
        self._key = key
        self._ttl = ttl

    def _command(self, *args: str) -> object:
        """One Upstash REST command. Returns the decoded `result`."""
        payload = json.dumps(list(args)).encode("utf-8")
        request = urllib.request.Request(
            self._url,
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                body = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            exc.close()  # otherwise the socket waits on the garbage collector
            raise RuntimeError(f"binding store returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise RuntimeError(f"binding store unreachable: {exc}") from exc
        if isinstance(body, dict) and body.get("error"):
            raise RuntimeError(f"binding store error: {body['error']}")
        return body.get("result") if isinstance(body, dict) else body

    def load(self) -> Binding | None:
        raw = self._command("GET", self._key)
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            log("WARNING: binding store holds unreadable data; treating as unbound.")
            return None
        chat_id = str(data.get("chat_id", "")).strip()
        if not chat_id:
            return None
        return Binding(
            chat_id=chat_id,
            username=str(data.get("username", "")),
            display_name=str(data.get("display_name", "")),
            bound_at=str(data.get("bound_at", "")),
        )

    def save(self, binding: Binding) -> None:
        if self._ttl:
            self._command("SET", self._key, json.dumps(asdict(binding)), "EX", str(self._ttl))
        else:
            self._command("SET", self._key, json.dumps(asdict(binding)))

    def clear(self) -> None:
        self._command("DEL", self._key)


def store_from_env(env: dict | None = None) -> BindingStore:
    """Pick a backend from the environment.

    Upstash wins when its URL is configured, because that is the host talking to
    itself across the internet and a local file there would vanish on redeploy.
    Otherwise a file, which is the right answer on a laptop and in the test suite.
    """
    env = os.environ if env is None else env
    url = env.get("UPSTASH_REDIS_REST_URL", "").strip()
    token = env.get("UPSTASH_REDIS_REST_TOKEN", "").strip()
    if url and token:
        return UpstashStore(url, token)
    if url or token:
        log("WARNING: only one of UPSTASH_REDIS_REST_URL / _TOKEN is set; using a local file.")
    return FileStore()