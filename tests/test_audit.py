"""Audit regressions. Every test here reproduces a bug that was actually observed.

Each one is named for the failure it prevents, not for the function it calls, so
that a future reader can tell whether deleting it would actually lose anything.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from reminder.commands import cmd_interval, cmd_whoami  # noqa: E402
from reminder.policy import (  # noqa: E402
    ALLOWED_INTERVALS,
    EARLY_TOLERANCE,
    MIN_INTERVAL_MINUTES,
    WINDOW,
    seconds_until_next_slot,
    should_send,
    slot_for,
)
from reminder.settings import (  # noqa: E402
    FileSettings,
    Settings,
    interval_mode_is_safe,
    validate_interval,
)
from reminder.transport import Telegram  # noqa: E402

CAIRO = timezone(timedelta(hours=2))
CONFIG = {"telegram_chat_id": "555", "message": "hi", "reminder_time": "09:00"}

_CI_VARS = ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "BUILDKITE", "TF_BUILD")


class RecordingTelegram:
    """Stands in for the transport so nothing touches the network."""

    def __init__(self, username: str = "testbot", webhook_url: str = ""):
        self.sent: list[str] = []
        self.username = username
        self.webhook_url = webhook_url

    def call(self, method: str, payload: dict | None = None):
        if method == "getMe":
            return {"ok": True, "result": {"username": self.username}}
        if method == "getWebhookInfo":
            return {"ok": True, "result": {"url": self.webhook_url}}
        if method == "getUpdates":
            return {"ok": True, "result": []}
        raise AssertionError(f"unexpected call {method}")

    def send_message(self, chat_id: str, text: str, reply_markup=None) -> None:
        self.sent.append(chat_id)


class DurableStore:
    """A store that remembers, so the interval tests are not about durability."""

    def __init__(self, settings: Settings | None = None):
        self._settings = settings or Settings()

    def load(self) -> Settings:
        return Settings(self._settings.interval_minutes, self._settings.last_slot)

    def save(self, settings: Settings) -> None:
        self._settings = settings


class SlotMathTest(unittest.TestCase):
    """`slot_for` decides whether a reminder is duplicate or new. It must never
    be computed against the wrong clock."""

    def test_a01_a_naive_datetime_is_refused_not_read_as_host_local(self):
        """Reproduced on this machine: a naive moment gave slot 1990200 while the
        same moment as UTC gave 1990212. Two hours of silence, or a double send."""
        naive = datetime(2026, 10, 5, 9, 0)
        with self.assertRaises(ValueError):
            slot_for(naive, 15)
        with self.assertRaises(ValueError):
            seconds_until_next_slot(naive, 15)

    def test_a02_the_same_instant_always_gives_the_same_slot(self):
        """DST must not shift a period. Cairo has no DST; New York does."""
        moment = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
        as_utc = slot_for(moment, 15)
        as_cario = slot_for(moment.astimezone(CAIRO), 15)
        self.assertEqual(as_utc, as_cario)

    def test_a03_an_interval_below_the_minimum_is_refused(self):
        """A 1-minute interval would ask GitHub for a beat it cannot give, and
        would produce 1440 sends a day."""
        for bad in (0, 1, -5, -15):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    slot_for(datetime.now(timezone.utc), bad)
                with self.assertRaises(ValueError):
                    seconds_until_next_slot(datetime.now(timezone.utc), bad)

    def test_a04_a_non_integer_interval_is_refused_rather_than_coerced(self):
        for bad in (15.5, "15", None, True):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    slot_for(datetime.now(timezone.utc), bad)

    def test_a05_the_countdown_never_promises_zero_seconds(self):
        """0 would read on the page as 'due now', which looks broken."""
        moment = datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)  # exactly on a 15m edge
        self.assertEqual(seconds_until_next_slot(moment, 15), 15 * 60)
        just_before = moment - timedelta(seconds=1)
        self.assertGreaterEqual(seconds_until_next_slot(just_before, 15), 1)


class ValidateIntervalTest(unittest.TestCase):
    """An unrecognised number must never become a schedule."""

    def test_a06_a_fractional_interval_is_refused(self):
        """Reproduced: validate_interval(15.9) returned 15, silently changing the
        schedule the user asked for."""
        for bad in (15.9, 30.7, -0.5):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    validate_interval(bad)

    def test_a07_a_boolean_is_not_a_number_of_minutes(self):
        """`value == ''` matched False, so False silently meant daily mode."""
        with self.assertRaises(ValueError):
            validate_interval(True)

    def test_a08_daily_mode_is_still_reachable_every_way_it_is_written(self):
        for value in (None, "", 0, "0", " 0 "):
            with self.subTest(value=value):
                self.assertEqual(validate_interval(value), 0)

    def test_a09_every_offered_interval_survives_validation(self):
        for value in ALLOWED_INTERVALS:
            with self.subTest(value=value):
                self.assertEqual(validate_interval(value), value)
                self.assertEqual(validate_interval(str(value)), value)


class DailyWindowTest(unittest.TestCase):
    def test_a10_a_run_just_before_the_time_still_sends(self):
        """Reproduced: a 08:00 run returned False and the day was then skipped
        until tomorrow. The whole day's reminder silently disappeared."""
        moment = datetime(2026, 10, 5, 9, 0, tzinfo=CAIRO) - EARLY_TOLERANCE
        self.assertTrue(should_send(moment, "09:00"))

    def test_a11_a_run_far_early_does_not_send(self):
        """The tolerance is a tolerance, not a licence to send at midnight."""
        moment = datetime(2026, 10, 5, 9, 0, tzinfo=CAIRO) - EARLY_TOLERANCE - timedelta(seconds=1)
        self.assertFalse(should_send(moment, "09:00"))

    def test_a12_the_late_edge_is_still_two_hours(self):
        moment = datetime(2026, 10, 5, 9, 0, tzinfo=CAIRO) + WINDOW
        self.assertTrue(should_send(moment, "09:00"))
        self.assertFalse(should_send(moment + timedelta(seconds=1), "09:00"))


class IntervalSpamGuardTest(unittest.TestCase):
    """The dangerous case is interval mode ON a host that cannot remember."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = FileSettings(Path(self.tmp.name) / "settings.json")
        self.store.save(Settings(interval_minutes=15))

    def test_a13_repeated_heartbeats_never_send_twice(self):
        """Reproduced: on a */5 cron an unserved marker meant 288 messages a day.

        The guard is no longer "is this disk durable" -- it is that the cycle is
        written before the send, so a second beat on the same store finds the
        question already open and owes nothing.
        """
        from reminder.timer import Cycle, FileCycleStore, activate

        store = FileCycleStore(Path(tempfile.mkdtemp()) / "timer.json")
        store.save(activate(Cycle(chat_id="555"), 30, datetime.now(timezone.utc)))
        client = RecordingTelegram()
        later = datetime.now(timezone.utc) + timedelta(minutes=30)
        for _ in range(5):
            cmd_interval(CONFIG, CAIRO, client, store, now=later)
        self.assertEqual(len(client.sent), 1, "five beats, one message")

    def test_a14_a_file_store_on_a_laptop_is_fine(self):
        """A laptop is the one place a file store is durable, and it sends."""
        from reminder.timer import Cycle, FileCycleStore, activate

        store = FileCycleStore(Path(tempfile.mkdtemp()) / "timer.json")
        store.save(activate(Cycle(chat_id="555"), 30, datetime.now(timezone.utc)))
        client = RecordingTelegram()
        code = cmd_interval(CONFIG, CAIRO, client, store, now=datetime.now(timezone.utc) + timedelta(minutes=30))
        self.assertEqual(code, 0)
        self.assertEqual(len(client.sent), 1)

    def test_a15_a_durable_store_is_allowed_on_ci(self):
        self.assertTrue(interval_mode_is_safe(DurableStore(), {"CI": "true"}))

    def test_a16_the_escape_hatch_is_honoured(self):
        env = {"CI": "true", "REMINDER_ALLOW_FILE_INTERVAL": "1"}
        self.assertTrue(interval_mode_is_safe(self.store, env))

    def test_a17_every_recognised_ci_variable_is_caught(self):
        for name in ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "BUILDKITE", "TF_BUILD"):
            with self.subTest(name=name):
                self.assertFalse(interval_mode_is_safe(self.store, {name: "true"}))

    def test_a18_an_empty_ci_variable_does_not_block_a_laptop(self):
        """CI=false is a lie many shells tell; only a set value counts."""
        self.assertTrue(interval_mode_is_safe(self.store, {"CI": ""}))


class WhoAmISafetyTest(unittest.TestCase):
    def test_a19_whoami_refuses_when_a_webhook_is_registered(self):
        """getUpdates cannot coexist with a webhook, and trying would fail with a
        409 that looks like a broken token."""
        client = RecordingTelegram(webhook_url="https://example.com/telegram/webhook")
        self.assertEqual(cmd_whoami(client), 1)
        self.assertEqual(client.sent, [])


class TransportGuardTest(unittest.TestCase):
    """The retry budget decides how long a failed send blocks the runner."""

    def _failing(self, code: int):
        import urllib.error
        import io

        def opener(request, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url, code, "boom", {}, io.BytesIO(b'{"description":"no"}')
            )

        return opener

    def test_a20_a_4xx_fails_immediately_rather_than_retrying(self):
        """A bad chat id will never become a good one. Three attempts would just
        burn the runner's five-minute timeout."""
        slept: list[float] = []
        client = Telegram("t", sleep=slept.append)
        import unittest.mock as mock

        with mock.patch("urllib.request.urlopen", self._failing(400)):
            with self.assertRaises(RuntimeError) as caught:
                client.call("sendMessage", {"chat_id": "1", "text": "x"})
        self.assertEqual(slept, [], "a 4xx must not sleep between attempts")
        self.assertIn("400", str(caught.exception))

    def test_a21_a_5xx_is_retried_then_reported(self):
        """A server-side blip is worth another try; giving up would drop the day."""
        slept: list[float] = []
        client = Telegram("t", sleep=slept.append)
        import unittest.mock as mock

        with mock.patch("urllib.request.urlopen", self._failing(503)):
            with self.assertRaises(RuntimeError) as caught:
                client.call("sendMessage", {"chat_id": "1", "text": "x"})
        self.assertEqual(len(slept), 2, "three attempts means two waits")
        self.assertIn("503", str(caught.exception))

    def test_a22_a_429_is_retried_because_it_is_rate_limiting(self):
        slept: list[float] = []
        client = Telegram("t", sleep=slept.append)
        import unittest.mock as mock

        with mock.patch("urllib.request.urlopen", self._failing(429)):
            with self.assertRaises(RuntimeError):
                client.call("sendMessage", {"chat_id": "1", "text": "x"})
        self.assertEqual(len(slept), 2)


if __name__ == "__main__":
    unittest.main()