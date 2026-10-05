"""The page countdown and the worker must be reading the same clock.

The bug this exists to prevent: the page used `seconds_until_next_slot`, which
counts to the next epoch-aligned boundary, while the worker used the cycle's
activation-relative due time. A user who pressed "15 minutes" at 07:20 was told
"10 minutes" by the page and left waiting until 07:35.

These tests drive the real HTTP endpoint rather than the helper, because the
endpoint is where the value is chosen.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from reminder.timer import Cycle, FileCycleStore, activate
from reminder.webapp import ConnectApp

UTC = timezone.utc


class _Binding:
    def __init__(self, chat_id: str):
        self.chat_id = chat_id


class _BindingStore:
    def __init__(self, chat_id: str):
        self._chat_id = chat_id

    def load(self):
        return _Binding(self._chat_id)

    def save(self, binding):
        return None


class CountdownContractTest(unittest.TestCase):
    """The served `seconds_until` is derived from the cycle, never from a slot."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = FileCycleStore(Path(self.dir.name) / "timer.json")
        self.app = ConnectApp(
            store=_BindingStore("555"),
            secret="s",
            bot_username="b",
            cycle_store=self.store,
            tz=UTC,
        )

    def activate_at(self, minutes: int, moment: datetime, chat_id: str = "555"):
        self.store.save(activate(Cycle(chat_id=chat_id), minutes, moment))
        self.store.remember(chat_id)

    def seconds_until(self, chat_id: str = "555") -> int:
        status, body = self.app.schedule({"chat": chat_id})
        self.assertEqual(status, 200)
        return body["seconds_until"]

    def test_activating_at_0720_shows_a_full_fifteen_minutes(self):
        moment = datetime.now(UTC)
        self.activate_at(15, moment)
        # A fresh activation owes the whole interval, not the remainder of some
        # wall-clock quarter-hour.
        self.assertGreaterEqual(self.seconds_until(), 15 * 60 - 5)
        self.assertLessEqual(self.seconds_until(), 15 * 60)

    def test_it_counts_down_rather_than_jumping_to_a_boundary(self):
        moment = datetime.now(UTC)
        self.activate_at(60, moment)
        first = self.seconds_until()
        self.assertAlmostEqual(first, 60 * 60, delta=5)
        # Two seconds later the page must be ~2 seconds later too, which epoch
        # slot maths could not produce: a slot boundary is not this instant.
        self.assertAlmostEqual(first - self.seconds_until(), 0, delta=5)

    def test_the_served_value_equals_the_cycles_own_next_send(self):
        moment = datetime.now(UTC) - timedelta(minutes=5)
        self.activate_at(30, moment)
        cycle = self.store.load("555")
        expected = cycle.seconds_until_next_send(datetime.now(UTC))
        self.assertEqual(self.seconds_until(), expected)

    def test_after_the_first_reminder_the_countdown_targets_the_nudge(self):
        moment = datetime.now(UTC) - timedelta(minutes=16)
        self.activate_at(15, moment)
        self.store.save(self.store.load("555").__class__(**{
            **vars(self.store.load("555")),
            "asked_at": (moment + timedelta(minutes=15)).isoformat(),
            "last_sent_at": (moment + timedelta(minutes=15)).isoformat(),
        }))
        # Five minutes after the reminder, not fifteen after activation.
        self.assertLessEqual(self.seconds_until(), 5 * 60 + 5)

    def test_a_stopped_timer_shows_no_countdown_at_all(self):
        moment = datetime.now(UTC)
        self.activate_at(15, moment)
        self.store.save(self.store.load("555").__class__(**{
            **vars(self.store.load("555")), "stopped": True,
        }))
        status, body = self.app.schedule({"chat": "555"})
        self.assertTrue(body["stopped"])
        self.assertEqual(body["seconds_until"], 0)

    def test_two_chats_count_down_independently(self):
        now = datetime.now(UTC)
        self.activate_at(15, now, chat_id="555")
        self.activate_at(180, now - timedelta(minutes=120), chat_id="777")
        self.assertLessEqual(self.seconds_until("555"), 15 * 60 + 5)
        self.assertGreater(self.seconds_until("777"), 55 * 60, "777 is still hours away")

    def test_each_page_reads_its_own_chat(self):
        now = datetime.now(UTC)
        self.activate_at(15, now, chat_id="555")
        self.activate_at(30, now, chat_id="777")
        status, body = self.app.schedule({"chat": "777"})
        self.assertEqual(body["chat_id"], "777")
        self.assertEqual(body["interval_minutes"], 30)

    def test_an_unregistered_chat_falls_back_to_the_connected_one(self):
        self.activate_at(60, datetime.now(UTC), chat_id="555")
        status, body = self.app.schedule({"chat": "not-registered"})
        self.assertEqual(body["chat_id"], "555", "an unknown id must not be trusted")

    def test_the_page_never_uses_the_epoch_slot_helper(self):
        """A regression guard on the source, not just the behaviour."""
        source = Path(__file__).resolve().parents[1] / "reminder" / "webapp.py"
        text = source.read_text(encoding="utf-8")
        cycle_branch = text.index("if self.cycle_store is not None:")
        self.assertIn(
            "return self._cycle_schedule(",
            text[cycle_branch : cycle_branch + 200],
            "the cycle branch must come first, before any epoch-slot maths",
        )


class PageMarkupTest(unittest.TestCase):
    """The page renders server values; it must not recompute a schedule."""

    def setUp(self):
        self.page = (Path(__file__).resolve().parents[1] / "reminder" / "page.py").read_text(
            encoding="utf-8"
        )

    def test_the_countdown_comes_from_the_server(self):
        self.assertIn("data.seconds_until", self.page)

    def test_there_is_no_epoch_slot_call_in_the_page(self):
        self.assertNotIn(
            "seconds_until_next_slot", self.page, "the page must not compute its own schedule"
        )

    def test_it_makes_no_daily_scheduling_promise(self):
        self.assertNotIn('"Until tomorrow morning"', self.page)
        self.assertNotIn('"One message a day"', self.page)


if __name__ == "__main__":
    unittest.main()