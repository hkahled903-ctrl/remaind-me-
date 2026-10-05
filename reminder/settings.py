"""Settings. The repeat interval, and which period has already been served.

Why this is not in config.json
------------------------------
`config.json` lives in git, so a runner sees whatever was last committed. If the
interval were set there, changing it on the Connect page would edit a file the
runner never reads, and the schedule would silently keep the old value. Worse,
the "have I already sent this period?" marker cannot live in git either: a
GitHub runner writes its file and throws it away, so every heartbeat would look
like a fresh period and the reminder would repeat on every single run.

So both live in the same durable store the chat binding uses -- Upstash when it is
configured, a local file otherwise. That is the whole reason gap G2 has to be
closed before an interval can mean anything.

Depends on `logs` and (for the hosted backend) `binding`. Knows nothing about
Telegram, HTTP servers or schedules in the daily sense.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from .binding import UpstashStore
from .logs import log
from .policy import ALLOWED_INTERVALS

SETTINGS_PATH = Path(__file__).resolve().parent.parent / "settings.json"
REDIS_KEY = "reminder:settings"


@dataclass
class Settings:
    """The repeat interval, and the last period served.

    `interval_minutes == 0` means the original daily mode, where `reminder_time`
    in config.json still governs. Keeping the two modes apart means nothing about
    the daily behaviour had to change, and a user who never opens the new picker
    is unaffected.

    `stopped` is the resting state after the user presses "I saved". It is a real
    flag rather than the absence of a question, because the two are different:
    a question being answered ends *that* report, while `stopped` ends the
    schedule itself until a new time is set. Without it, "I saved" would silence
    one nudge and the next period would fire as if nothing had happened.
    """

    interval_minutes: int = 0
    last_slot: str = ""
    stopped: bool = False

    @property
    def is_interval(self) -> bool:
        return self.interval_minutes > 0

    @property
    def is_silent(self) -> bool:
        """Whether every sending mode should hold its fire right now."""
        return self.stopped


def validate_interval(value) -> int:
    """Accept only an offered interval, or 0 for daily mode.

    Rejecting rather than coercing matters: a stray value here decides how often
    someone gets interrupted, so an unrecognised number must not become 15.

    `bool` and `float` are refused on purpose. `int(True)` is 1, and
    `int(15.9)` is 15 -- both would silently change the schedule, and both
    arrive easily from a JSON body or a spreadsheet.
    """
    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        raise ValueError("interval must be a number of minutes, not a yes/no")
    if isinstance(value, float):
        raise ValueError("interval must be a whole number of minutes")
    if isinstance(value, str):
        # int() would accept "+30" and " 30 " silently; both are fine, but
        # "30.0" must not become 30 by another route.
        try:
            minutes = int(value.strip())
        except ValueError:
            raise ValueError("interval must be a whole number of minutes") from None
    elif isinstance(value, int):
        minutes = value
    else:
        raise ValueError("interval must be a whole number of minutes")
    if minutes == 0:
        return 0
    if minutes not in ALLOWED_INTERVALS:
        raise ValueError(
            f"interval must be one of {', '.join(str(m) for m in ALLOWED_INTERVALS)} minutes"
        )
    return minutes


class SettingsStore(Protocol):
    def load(self) -> Settings: ...
    def save(self, settings: Settings) -> None: ...


class FileSettings:
    """One JSON file. Correct on a laptop and on a host with a real disk."""

    def __init__(self, path: Path = SETTINGS_PATH):
        self._path = path

    def load(self) -> Settings:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return Settings()  # absent or corrupt: run the daily default
        if not isinstance(raw, dict):
            return Settings()
        try:
            interval = validate_interval(raw.get("interval_minutes", 0))
        except ValueError as exc:
            log(f"WARNING: ignoring stored interval ({exc}); using the daily default.")
            interval = 0
        return Settings(
            interval_minutes=interval,
            last_slot=str(raw.get("last_slot", "")),
            stopped=bool(raw.get("stopped", False)),
        )

    def save(self, settings: Settings) -> None:
        tmp = self._path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(asdict(settings), indent=2), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as exc:
            log(f"could not write {self._path.name}: {exc}")
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


class UpstashSettings(UpstashStore):
    """The same record in Upstash, reachable from the runner.

    Subclasses `UpstashStore` only to expose its REST command helper; every byte
    on the wire is code this project already tested in `tests/test_connect.py`.
    """

    def __init__(self, url: str, token: str, key: str = REDIS_KEY):
        super().__init__(url, token, key=key)

    def command(self, *args: str):
        return self._command(*args)

    def load(self) -> Settings:
        raw = self.command("GET", self._key)
        if not raw:
            return Settings()
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            log("WARNING: settings store holds unreadable data; using the daily default.")
            return Settings()
        try:
            interval = validate_interval(data.get("interval_minutes", 0))
        except ValueError as exc:
            log(f"WARNING: ignoring stored interval ({exc}); using the daily default.")
            interval = 0
        return Settings(
            interval_minutes=interval,
            last_slot=str(data.get("last_slot", "")),
            stopped=bool(data.get("stopped", False)),
        )

    def save(self, settings: Settings) -> None:
        self.command("SET", self._key, json.dumps(asdict(settings)))


def settings_store_from_env(env: dict | None = None) -> SettingsStore:
    """Upstash when both credentials exist, otherwise a local file.

    A store that is unreachable must not read as "daily mode", because that would
    quietly stop the interval reminders without a word. `load` lets the error
    escape; the caller decides.
    """
    env = os.environ if env is None else env
    url = env.get("UPSTASH_REDIS_REST_URL", "").strip()
    token = env.get("UPSTASH_REDIS_REST_TOKEN", "").strip()
    if url and token:
        return UpstashSettings(url, token)
    if url or token:
        log("WARNING: only one of UPSTASH_REDIS_REST_URL / _TOKEN is set; using a local file.")
        log("WARNING: an interval reminder will repeat on every run without Upstash.")
    return FileSettings()


def interval_mode_is_safe(store: SettingsStore, env: dict | None = None) -> bool:
    """Whether this store can remember a served period between the next runs.

    A network store always can. A file store can only if the disk outlives the
    process, which is true on a laptop and false on an ephemeral CI runner, where
    the file is written and thrown away and every heartbeat looks like a fresh
    period. That is the difference between a reminder and 288 messages a day, so
    it is worth asking before the mode is allowed to send anything.

    `REMINDER_ALLOW_FILE_INTERVAL=1` is the escape hatch for a host with a real
    disk that is simply not running on a recognised CI variable.
    """
    env = os.environ if env is None else env
    if not isinstance(store, FileSettings):
        return True
    if env.get("REMINDER_ALLOW_FILE_INTERVAL", "").strip() in ("1", "true", "yes"):
        return True
    return not any(
        env.get(name, "").strip()
        for name in ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "BUILDKITE", "TF_BUILD")
    )