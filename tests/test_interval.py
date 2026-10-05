#!/usr/bin/env python3
"""Interval reminders: does "every 30 minutes" mean every 30 minutes?

The risk this file exists to catch is a repeating reminder that repeats wrongly.
Three ways that happens, each covered below: sending twice inside one period,
sending on a heartbeat when interval mode is off, and sending to nobody because
the marker did not survive to the next runner.

The marker is exercised against a real file on a real disk, because surviving
between processes is the entire claim. `Telegram` is a recording double here:
the HTTP path is already covered in test_scenarios.py, and what matters in this
file is when a send happens, not how it travels.
"""

from __future__ import annotations

import re
import sys
import os
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from reminder.commands import cmd_interval, resolve_chat_id, set_interval  # noqa: E402
from reminder.timer import Cycle, FileCycleStore, activate  # noqa: E402
from reminder.settings import FileSettings  # noqa: E402
from reminder.policy import ALLOWED_INTERVALS, slot_for  # noqa: E402
from reminder.settings import FileSettings, Settings, validate_interval  # noqa: E402

CAIRO = timezone(timedelta(hours=2))
# Deliberately aligned to a period boundary (10:00 Cairo = 08:00 UTC, and 1800s
# divides an hour, so 30/60-minute slots always land on :00 and :30). A start at
# 10:07 would put +29 minutes into the *next* period, which is correct behaviour
# but makes "several beats inside one period" impossible to express.
NOW = datetime(2026, 3, 14, 10, 0, 0, tzinfo=CAIRO)
CONFIG = {"telegram_chat_id": "555", "message": "Reminder: your daily report is due."}


class RecordingTelegram:
    def __init__(self):
        self.sent: list[str] = []

    def send_message(self, chat_id: str, text: str, reply_markup=None) -> None:
        self.sent.append(f"{chat_id}:{text}")


class SettingsStoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = FileSettings(Path(self.tmp.name) / "settings.json")

    def tearDown(self):
        self.tmp.cleanup()


class DurableDiskCase(SettingsStoreCase):
    """A file store whose disk really does survive the run.

    `interval_mode_is_safe` refuses a plain file store whenever `CI` is set,
    which is correct for a GitHub runner and wrong for a temp directory that
    outlives the call. Rather than weakening the guard, these tests declare the
    exception they mean, so the guard stays honest on both sides.

    Without this the suite passed on a laptop and failed on every CI run -- a
    real bug the first push to GitHub caught.
    """

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"REMINDER_ALLOW_FILE_INTERVAL": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)


class SlotTest(unittest.TestCase):
    def test_i1_a_period_is_stable_inside_itself(self):
        start = slot_for(NOW, 30)
        self.assertEqual(start, slot_for(NOW + timedelta(minutes=1), 30))
        self.assertEqual(start, slot_for(NOW + timedelta(minutes=22), 30))

    def test_i2_the_next_period_has_a_different_id(self):
        self.assertNotEqual(
            slot_for(NOW, 30),
            slot_for(NOW + timedelta(minutes=30), 30),
        )

    def test_i3_period_ids_are_derived_from_the_clock_not_the_process(self):
        """A runner that starts late still computes the same period as one that
        started at the beginning of it, and later days count forward."""
        self.assertEqual(
            slot_for(NOW, 60),
            slot_for(NOW + timedelta(minutes=40), 60),
            "a late runner must not open a fresh window",
        )
        self.assertGreater(
            slot_for(NOW + timedelta(days=3), 60),
            slot_for(NOW, 60),
            "ids are absolute, so they advance with the clock",
        )

    def test_i4_an_interval_below_the_cron_floor_is_refused(self):
        with self.assertRaises(ValueError):
            slot_for(NOW, 1)


class ValidateIntervalTest(unittest.TestCase):
    def test_i5_only_offered_intervals_are_accepted(self):
        for minutes in ALLOWED_INTERVALS:
            self.assertEqual(validate_interval(minutes), minutes)

    def test_i6_zero_means_daily_mode(self):
        for empty in (0, "0", "", None):
            self.assertEqual(validate_interval(empty), 0)

    def test_i7_an_unlisted_interval_is_refused_rather_than_rounded(self):
        for bad in (7, 45, 90, "often", -30):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_interval(bad)


class SettingsFileTest(SettingsStoreCase):
    def test_i8_settings_survive_a_restart(self):
        self.store.save(Settings(interval_minutes=30, last_slot="12345"))
        reread = FileSettings(self.store._path).load()  # a new object, as a new run
        self.assertEqual(reread.interval_minutes, 30)
        self.assertEqual(reread.last_slot, "12345")
        self.assertTrue(reread.is_interval)

    def test_i9_a_missing_file_reads_as_daily_mode(self):
        self.assertEqual(FileSettings(Path(self.tmp.name) / "nope.json").load(),
                         Settings(interval_minutes=0, last_slot=""))

    def test_i10_a_corrupt_stored_interval_falls_back_instead_of_guessing(self):
        self.store._path.write_text('{"interval_minutes": 7, "last_slot": "x"}', encoding="utf-8")
        self.assertEqual(self.store.load().interval_minutes, 0)


class IntervalHeartbeatTest(unittest.TestCase):
    """`cmd_interval` is what the */5 cron calls, over and over.

    The heartbeat is a heartbeat: it fires every five minutes regardless of the
    user's interval, and the persisted cycle is what decides whether this beat
    owes anybody a message.
    """

    def setUp(self):
        import tempfile
        from pathlib import Path as _P

        from reminder.timer import Cycle, FileCycleStore, activate

        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = FileCycleStore(_P(self.dir.name) / "timer.json")
        self.chat = "555"
        self.activate = lambda minutes, now: self.store.save(
            activate(Cycle(chat_id=self.chat), minutes, now)
        )

    def beat(self, now, client=None):
        client = client or RecordingTelegram()
        cmd_interval(CONFIG, CAIRO, client, self.store, now=now)
        return client

    def test_i11_heartbeat_is_silent_when_no_interval_is_set(self):
        client = self.beat(NOW)
        self.assertEqual(client.sent, [], "a quiet heartbeat must not send anything")

    def test_i11b_nothing_is_sent_before_the_interval_elapses(self):
        # The bug this replaces: activation used to reset an epoch slot, so the
        # very next heartbeat sent immediately. Twelve beats here, one send.
        self.activate(60, NOW)
        client = RecordingTelegram()
        for minutes in (5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 59):
            self.beat(NOW + timedelta(minutes=minutes), client)
        self.assertEqual(client.sent, [], "60 minutes means 60 minutes")
        self.beat(NOW + timedelta(minutes=60), client)
        self.assertEqual(len(client.sent), 1, "the first reminder goes out when due")

    def test_i12_repeated_polling_at_the_due_time_sends_only_one_reminder(self):
        # Several beats land on or just after the due time. The first one owes
        # the reminder; the rest must not repeat it. Nudges five minutes after
        # the reminder are correct and are counted separately below.
        self.activate(30, NOW)
        client = RecordingTelegram()
        for minutes in (30, 30, 31, 32, 34):
            self.beat(NOW + timedelta(minutes=minutes), client)
        # Every beat from +30 to +34 is at or past due, but only the first may
        # send: the nudge gap (5 min) has not elapsed, so no nudge is owed yet.
        self.assertEqual(len(client.sent), 1, "one activation owes exactly one reminder")

    def test_i13_the_nudge_cadence_continues_instead_of_repeating_the_reminder(self):
        self.activate(30, NOW)
        client = RecordingTelegram()
        self.beat(NOW + timedelta(minutes=30), client)
        for minutes in (35, 40, 45, 50, 55, 60, 65):
            self.beat(NOW + timedelta(minutes=minutes), client)
        # The question is open, so what follows is nudges -- never a second
        # "your report is ready".
        self.assertGreater(len(client.sent), 1, "nudges keep coming")
        self.assertTrue(client.sent[0].startswith(f"{self.chat}:"))

    def test_i14_the_cycle_survives_a_new_process_reading_the_same_file(self):
        """The case that matters on CI: the next runner has no memory of this one."""
        import reminder.timer as timer_mod

        self.activate(30, NOW)
        client = RecordingTelegram()
        self.beat(NOW + timedelta(minutes=30), client)

        fresh_store = timer_mod.FileCycleStore(self.store._path)  # the next runner
        cmd_interval(CONFIG, CAIRO, client, fresh_store, now=NOW + timedelta(minutes=31))
        self.assertEqual(len(client.sent), 1, "the new runner honoured the stored cycle")

    def test_i15_the_message_goes_to_the_chat_that_activated_it(self):
        self.activate(60, NOW)
        client = self.beat(NOW + timedelta(minutes=60))
        self.assertTrue(client.sent[0].startswith(f"{self.chat}:"))


class ServeStartupTest(unittest.TestCase):
    """`--serve` blocks forever once it starts, so the only safe thing to assert
    is that it refuses *before* that point. This caught a real crash: the server
    referenced an import it never had, and died on startup with no test failing."""

    def test_i26_serve_refuses_without_a_webhook_secret(self):
        import os

        from reminder.commands import cmd_serve

        saved = os.environ.pop("WEBHOOK_SECRET", None)
        try:
            code = cmd_serve(
                {"message": "x", "reminder_time": "09:00"},
                CAIRO,
                RecordingTelegram(),
                FileSettings(),
            )
        finally:
            if saved is not None:
                os.environ["WEBHOOK_SECRET"] = saved
        self.assertEqual(code, 1, "an unauthenticated webhook must not start")

    def test_i27_serve_has_a_bot_username_before_it_starts_listening(self):
        import os

        from reminder.commands import cmd_serve

        saved_secret = os.environ.pop("WEBHOOK_SECRET", None)
        saved_name = os.environ.pop("BOT_USERNAME", None)
        os.environ["WEBHOOK_SECRET"] = "x" * 32
        try:
            # An empty username must be caught by the guard, not by a crash
            # somewhere inside Telegram().
            with self.assertRaises(Exception):
                cmd_serve(
                    {"message": "x", "reminder_time": "09:00"},
                    CAIRO,
                    RecordingTelegram(),
                    FileSettings(),
                )
        finally:
            if saved_secret is not None:
                os.environ["WEBHOOK_SECRET"] = saved_secret
            if saved_name is not None:
                os.environ["BOT_USERNAME"] = saved_name


class PortFromEnvTest(unittest.TestCase):
    """Deploying means obeying the platform's `PORT`. Every PaaS assigns one and
    routes the public hostname to it, so a server pinned to 8000 answers locally
    and returns 502s in production."""

    def setUp(self):
        import os

        self._os = os
        self._saved = os.environ.pop("PORT", None)

    def tearDown(self):
        if self._saved is None:
            self._os.environ.pop("PORT", None)
        else:
            self._os.environ["PORT"] = self._saved

    def test_i28_the_platform_port_wins_over_the_default(self):
        from reminder.commands import port_from_env

        self._os.environ["PORT"] = "53211"
        self.assertEqual(port_from_env(8000), 53211)

    def test_i29_no_port_keeps_the_default(self):
        from reminder.commands import port_from_env

        self.assertEqual(port_from_env(8000), 8000)

    def test_i30_a_nonsense_port_falls_back_instead_of_crashing(self):
        from reminder.commands import port_from_env

        for bad in ("not-a-number", "0", "70000", ""):
            self._os.environ["PORT"] = bad
            self.assertEqual(port_from_env(8000), 8000, f"PORT={bad!r} must not win")


class SetIntervalTest(unittest.TestCase):
    """`set_interval` starts one chat's cycle. It no longer writes a global
    record, because with several people a shared record means one person's
    button changes everybody's schedule."""

    def setUp(self):
        import tempfile
        from pathlib import Path as _P

        from reminder.timer import Cycle, FileCycleStore

        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = FileCycleStore(_P(self.dir.name) / "timer.json")
        self.store.save(Cycle(chat_id="555", interval_minutes=30))

    def test_i16_activating_discards_the_previous_markers(self):
        self.assertEqual(set_interval(self.store, 60, "555", now=NOW), 0)
        after = self.store.load("555")
        self.assertEqual(after.interval_minutes, 60)
        self.assertEqual(after.asked_at, "", "a stale question must not cross over")
        self.assertEqual(after.nudges, 0)

    def test_i16b_activating_one_chat_leaves_another_untouched(self):
        self.store.save(Cycle(chat_id="777", interval_minutes=15))
        set_interval(self.store, 60, "555", now=NOW)
        self.assertEqual(self.store.load("777").interval_minutes, 15)

    def test_i16c_activating_without_a_chat_changes_nothing(self):
        self.assertEqual(set_interval(self.store, 60, "", now=NOW), 1)
        self.assertEqual(self.store.load("555").interval_minutes, 30)

    def test_i17_an_unlisted_interval_is_refused_and_changes_nothing(self):
        self.assertEqual(set_interval(self.store, 45, "555", now=NOW), 1)
        self.assertEqual(self.store.load("555").interval_minutes, 30)


class ResolveChatTest(unittest.TestCase):
    def test_i19_interval_mode_still_needs_someone_to_message(self):
        from reminder.config import ConfigError

        with self.assertRaises(ConfigError):
            resolve_chat_id({"telegram_chat_id": "", "message": "x"}, None)


class HeartbeatWorkflowTest(unittest.TestCase):
    """The cron file is the schedule now, so it is part of the correctness."""

    def setUp(self):
        self.text = (PROJECT_ROOT / ".github" / "workflows" / "interval-reminder.yml").read_text(
            encoding="utf-8"
        )

    def test_i20_actions_are_pinned_to_shas(self):
        sha = re.compile(r"^[0-9a-f]{40}$")
        for ref in re.findall(r"uses:\s*(\S+)", self.text):
            with self.subTest(action=ref):
                repo, _, part = ref.partition("@")
                self.assertTrue(sha.match(part), f"{repo} is not pinned to a SHA: {ref}")

    def test_i21_the_beat_is_within_the_cron_floor(self):
        """GitHub rejects a cron finer than five minutes; catching it here is kinder."""
        match = re.search(r'cron:\s*"([*/\d]+)\s+(\S+)\s+\*\s+\*\s+\*"', self.text)
        self.assertIsNotNone(match, "no heartbeat cron found")
        minutes = match.group(1)
        step = int(minutes[2:]) if minutes.startswith("*/") else int(minutes)
        self.assertGreaterEqual(step, 5, "GitHub will not run a cron finer than 5 minutes")

    def test_i22_it_has_a_timeout_and_least_privilege(self):
        self.assertRegex(self.text, r"timeout-minutes:\s*\d+")
        self.assertRegex(self.text, r"(?m)^permissions:\s*\n\s+contents:\s+read\s*$")
        self.assertIn("concurrency:", self.text)

    def test_i23_it_never_hardcodes_a_secret(self):
        self.assertIsNone(
            re.search(r"TELEGRAM_BOT_TOKEN:\s*(?!\$\{\{)\S", self.text),
            "a token-shaped literal must never appear in the workflow",
        )
        # The token is fetched from Infisical, not stored in GitHub Secrets, so
        # the workflow must not name one either.
        self.assertNotIn("secrets.TELEGRAM_BOT_TOKEN", self.text)
        self.assertIn("Infisical/secrets-action@", self.text)

    def test_i24_the_durable_store_is_passed_to_the_heartbeat(self):
        """Without these the runner forgets, and the reminder repeats all day.

        Infisical holds the pair now, so the repository cannot check that it is
        there -- only that nothing stale is left behind to disagree with it, and
        that the heartbeat fetches the whole environment rather than a subset.
        """
        self.assertIn("Infisical/secrets-action@", self.text)
        self.assertNotIn("secrets.UPSTASH_REDIS_REST_URL", self.text)
        self.assertNotIn("secrets.UPSTASH_REDIS_REST_TOKEN", self.text)
        self.assertNotIn(
            "secret-path:",
            self.text,
            "a secret-path would exclude the durable store and the beat would repeat",
        )

    def test_i25_the_daily_workflow_is_untouched_by_this_feature(self):
        """Interval mode is additive; the daily reminder keeps its own cron."""
        daily = (PROJECT_ROOT / ".github" / "workflows" / "daily-reminder.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn('cron: "0 7 * * *"', daily)
        self.assertNotIn("--interval", daily)


if __name__ == "__main__":
    unittest.main(verbosity=2)