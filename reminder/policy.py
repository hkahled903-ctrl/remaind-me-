"""Policy. The single answer to "is it time to send?"

Pure: no network, no disk, no clock reads of its own. Every caller passes the
moment it is deciding about, which is what makes the boundaries testable.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .config import parse_hhmm

# How long after reminder_time a run still counts as today's reminder. Verified
# in tests/test_scenarios.py (test_s5). Absorbs cron drift in the late direction.
WINDOW = timedelta(hours=2)

# How early a run may still count as today's reminder. Without this the window is
# one-sided: a workflow that fires before reminder_time -- because the cron was
# edited, or the runner's clock leads -- skips, and the next attempt is tomorrow.
# A run this far early sends the right day's message a little early, which is a
# far smaller failure than never sending it (ROADMAP.md gap G1).
EARLY_TOLERANCE = timedelta(minutes=30)

# Intervals offered on the Connect page, in minutes. Defined here rather than in
# the UI so the list and the rule that rejects an unsupported value are one thing.
ALLOWED_INTERVALS = (15, 30, 60, 180)

# GitHub Actions refuses a cron finer than every five minutes, so a 15-minute
# reminder cannot rely on the heartbeat firing more often than this.
MIN_INTERVAL_MINUTES = 5


def _require_aware(now: datetime) -> datetime:
    """Refuse a naive moment rather than guessing at the host's timezone.

    `datetime.timestamp()` on a naive value silently reads it as host-local time.
    On a CI runner that is UTC while the user is in Cairo, which shifts the slot
    by two hours and can double-send or skip a period. Failing loudly is the only
    honest option, because the alternative is a wrong answer nobody sees.
    """
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise ValueError("slot math needs a timezone-aware datetime")
    return now


def should_send(now: datetime, reminder_time: str) -> bool:
    """True when `now` is inside today's reminder window.

    The window opens EARLY_TOLERANCE before reminder_time and closes WINDOW
    after it. Opening early is deliberate: a run before the opening would skip,
    and skipping means the day's reminder never arrives.
    """
    hour, minute = parse_hhmm(reminder_time)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return -EARLY_TOLERANCE <= (now - target) <= WINDOW


def next_occurrence(now: datetime, reminder_time: str) -> datetime:
    """The next moment the reminder should fire. Strictly after `now`."""
    hour, minute = parse_hhmm(reminder_time)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def _check_interval(interval_minutes: int) -> int:
    """Reject an interval that cannot produce a sane slot.

    Guarded in one place so `slot_for` and `seconds_until_next_slot` cannot
    disagree: the page renders one and the runner enforces the other.
    """
    if not isinstance(interval_minutes, int) or isinstance(interval_minutes, bool):
        raise ValueError(f"interval must be a whole number of minutes, got {interval_minutes!r}")
    if interval_minutes < MIN_INTERVAL_MINUTES:
        raise ValueError(f"interval must be at least {MIN_INTERVAL_MINUTES} minutes")
    return interval_minutes


def slot_for(now: datetime, interval_minutes: int) -> str:
    """Which period of the repeating cycle `now` falls in.

    Deliberately absolute: slots are numbered from the Unix epoch rather than from
    the first run, so two runners that disagree about history still agree about
    which period they are in, and a restart cannot silently open a fresh window.
    """
    _require_aware(now)
    _check_interval(interval_minutes)
    seconds = interval_minutes * 60
    return str(int(now.timestamp()) // seconds)


def seconds_until_next_slot(now: datetime, interval_minutes: int) -> int:
    """How long until the next period opens. Drives the on-page countdown.

    Always at least 1: returning 0 would tell the page the reminder is due now,
    which reads as broken.
    """
    _require_aware(now)
    _check_interval(interval_minutes)
    seconds = interval_minutes * 60
    return max(1, seconds - (int(now.timestamp()) % seconds))