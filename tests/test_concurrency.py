"""Duplicate-send safety under overlapping workers.

The engine claims a beat before it sends and writes the cycle before it calls
Telegram, so the guarantee is **at-most-once**, not exactly-once: `sendMessage`
is not transactional with Redis and cannot be made so. These tests pin that
promise to the behaviour it actually produces, so the word cannot quietly drift
into a claim stronger than the code keeps.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from reminder.commands import cmd_interval, cmd_nudge, stop_timer
from reminder.timer import (
    CLAIM_TTL_SECONDS,
    Cycle,
    FileCycleStore,
    activate,
    mark_sent,
)

UTC = timezone.utc
CONFIG = {"telegram_chat_id": "555", "message": "Report ready.", "reminder_time": "09:00"}


class Bot:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send_message(self, chat_id: str, text: str, reply_markup=None) -> None:
        self.sent.append((str(chat_id), text))

    @property
    def nudges(self) -> int:
        return len([t for _, t in self.sent if t.startswith("Still open")])


class ExplodingBot(Bot):
    """Telegram refuses the message, as a 4xx would."""

    def send_message(self, chat_id: str, text: str, reply_markup=None) -> None:
        raise RuntimeError("Telegram rejected the request: HTTP 400")


class _ClaimOnceStore(FileCycleStore):
    """A file store with a real exclusive claim, so two workers can contend.

    `FileCycleStore.claim` is unconditionally True because a single local
    process is the whole world on a laptop. These tests need the contention the
    Redis `SET NX` creates, so they take the file store's persistence and give
    it an atomic create.
    """

    def __init__(self, path: Path):
        super().__init__(path)
        self._claims: dict[str, int] = {}
        self.held: set[str] = set()

    def claim(self, chat_id: str, cycle: int) -> bool:
        if chat_id in self.held:
            return False
        self.held.add(chat_id)
        self._claims[chat_id] = cycle
        return True

    def release(self, chat_id: str) -> None:
        self.held.discard(chat_id)


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "timer.json"
        self.store = _ClaimOnceStore(self.path)

    def start(self, chat_id: str, minutes: int, at: datetime) -> None:
        self.store.save(activate(Cycle(chat_id=chat_id), minutes, at))
        self.store.remember(chat_id)


class ConcurrentWorkerTest(StoreCase):
    """1. Two workers, one due beat."""

    def test_only_one_worker_can_hold_the_claim(self):
        self.start("555", 15, datetime(2026, 10, 5, 7, 20, tzinfo=UTC))
        first = self.store.claim("555", 1)
        second = self.store.claim("555", 1)
        self.assertTrue(first, "the first worker owns the beat")
        self.assertFalse(second, "the second must be told to stand down")

    def test_two_workers_on_one_due_beat_produce_one_message(self):
        at = datetime(2026, 10, 5, 7, 35, tzinfo=UTC)
        self.start("555", 15, at - timedelta(minutes=15))

        bot = Bot()
        cmd_interval(CONFIG, UTC, bot, self.store, now=at)
        self.store.release("555")  # the claim expires, as a TTL guarantees
        cmd_interval(CONFIG, UTC, bot, self.store, now=at)

        # Two beats, one at the same instant: the question is now open, so the
        # second owes nothing until the nudge gap elapses.
        self.assertEqual(len(bot.sent), 1, f"exactly one message, got {bot.sent}")

    def test_a_worker_that_never_releases_the_claim_blocks_the_next_beat(self):
        """And that block is bounded by the TTL, which is why it is short."""
        at = datetime(2026, 10, 5, 7, 35, tzinfo=UTC)
        self.start("555", 15, at - timedelta(minutes=15))
        bot = Bot()
        cmd_interval(CONFIG, UTC, bot, self.store, now=at)
        # The claim is still held: a stuck worker must not cause a second send.
        cmd_interval(CONFIG, UTC, bot, self.store, now=at + timedelta(minutes=1))
        self.assertEqual(len(bot.sent), 1)
        self.assertLess(CLAIM_TTL_SECONDS, 5 * 60, "the lock must not outlast a nudge gap")


class RestartSafetyTest(StoreCase):
    """2. A worker that dies between the write and the send."""

    def test_state_is_committed_before_telegram_is_called(self):
        """Proves the ordering that makes the guarantee at-most-once.

        The bot records what the store held at the instant it was called. If the
        cycle were still un-served, a crash right after the send would let the
        next runner send it again.
        """
        at = datetime(2026, 10, 5, 7, 35, tzinfo=UTC)
        self.start("555", 15, at - timedelta(minutes=15))
        seen: list[bool] = []

        outer = self

        class PeekingBot(Bot):
            def send_message(self, chat_id, text, reply_markup=None):
                seen.append(outer.store.load("555").pending)
                super().send_message(chat_id, text, reply_markup)

        bot = PeekingBot()
        cmd_interval(CONFIG, UTC, bot, self.store, now=at)
        self.assertEqual(seen, [True], "the cycle was already marked sent when Telegram was called")

    def test_a_restart_after_the_commit_does_not_resend(self):
        at = datetime(2026, 10, 5, 7, 35, tzinfo=UTC)
        self.start("555", 15, at - timedelta(minutes=15))
        bot = Bot()
        cmd_interval(CONFIG, UTC, bot, self.store, now=at)

        # A brand-new store object: the next runner has no memory of this one.
        fresh = FileCycleStore(self.path)
        cmd_interval(CONFIG, UTC, bot, fresh, now=at + timedelta(seconds=30))
        self.assertEqual(len(bot.sent), 1, "the committed cycle survives the restart")

    def test_a_failed_send_costs_that_beat_and_not_a_duplicate(self):
        at = datetime(2026, 10, 5, 7, 35, tzinfo=UTC)
        self.start("555", 15, at - timedelta(minutes=15))
        with self.assertRaises(RuntimeError):
            cmd_interval(CONFIG, UTC, ExplodingBot(), self.store, now=at)

        # The cycle was committed before the call, so the retry does not resend
        # it either -- this is the deliberate trade: miss rather than double.
        bot = Bot()
        cmd_interval(CONFIG, UTC, bot, self.store, now=at + timedelta(seconds=30))
        self.assertEqual(bot.sent, [], "at-most-once means a retry does not re-send")


class StoppedCycleTest(StoreCase):
    """3. A stopped cycle is silent to every worker."""

    def test_neither_worker_can_send_after_stop(self):
        at = datetime(2026, 10, 5, 7, 35, tzinfo=UTC)
        self.start("555", 15, at - timedelta(minutes=15))
        bot = Bot()
        cmd_interval(CONFIG, UTC, bot, self.store, now=at)
        before = len(bot.sent)

        stop_timer(self.store, None, "555")
        for minute in range(0, 240, 5):
            moment = at + timedelta(minutes=minute)
            cmd_interval(CONFIG, UTC, bot, self.store, now=moment)
            cmd_nudge(bot, self.store, now=moment)
        self.assertEqual(len(bot.sent), before, "nothing may follow STOP")

    def test_a_stopped_cycle_never_becomes_due_again(self):
        at = datetime(2026, 10, 5, 7, 35, tzinfo=UTC)
        self.start("555", 15, at - timedelta(minutes=15))
        bot = Bot()
        cmd_interval(CONFIG, UTC, bot, self.store, now=at)
        stop_timer(self.store, None, "555")
        for minute in range(0, 600, 5):
            self.store.release("555")
            cmd_interval(CONFIG, UTC, bot, self.store, now=at + timedelta(minutes=minute))
        self.assertEqual(len(bot.sent), 1, "one message, then silence forever")


class IsolationTest(StoreCase):
    """4. Two chats never touch each other."""

    def test_a_cannot_claim_b_s_beat(self):
        self.start("555", 15, datetime(2026, 10, 5, 7, 20, tzinfo=UTC))
        self.start("777", 60, datetime(2026, 10, 5, 7, 20, tzinfo=UTC))
        self.assertTrue(self.store.claim("555", 1))
        self.assertTrue(
            self.store.claim("777", 1),
            "chat 777's claim is its own; 555 holding one must not block it",
        )

    def test_stopping_a_does_not_stop_b(self):
        at = datetime(2026, 10, 5, 7, 35, tzinfo=UTC)
        self.start("555", 15, at - timedelta(minutes=15))
        self.start("777", 15, at - timedelta(minutes=15))
        # Both have actually been asked, so "still pending" means something.
        bot = Bot()
        cmd_interval(CONFIG, UTC, bot, self.store, now=at)
        stop_timer(self.store, None, "555")

        self.assertTrue(self.store.load("555").stopped)
        self.assertFalse(self.store.load("777").stopped, "777 must be untouched")
        self.assertTrue(self.store.load("777").pending, "777's question is still open")

    def test_b_keeps_being_served_after_a_is_stopped(self):
        at = datetime(2026, 10, 5, 7, 35, tzinfo=UTC)
        self.start("555", 15, at - timedelta(minutes=15))
        self.start("777", 15, at - timedelta(minutes=15))
        bot = Bot()
        cmd_interval(CONFIG, UTC, bot, self.store, now=at)
        stop_timer(self.store, None, "555")

        for minute in range(40, 100, 5):
            self.store.release("777")
            cmd_interval(CONFIG, UTC, bot, self.store, now=at + timedelta(minutes=minute))
        self.assertTrue(
            [c for c, _ in bot.sent if c == "777"], "chat 777 must still be nudged"
        )
        self.assertFalse(
            [c for c, _ in bot.sent if c == "555"][1:], "chat 555 must stay silent"
        )

    def test_activation_times_and_intervals_stay_independent(self):
        self.start("555", 15, datetime(2026, 10, 5, 7, 20, tzinfo=UTC))
        self.start("777", 180, datetime(2026, 10, 5, 9, 0, tzinfo=UTC))
        self.assertEqual(self.store.load("555").due_at, datetime(2026, 10, 5, 7, 35, tzinfo=UTC))
        self.assertEqual(self.store.load("777").due_at, datetime(2026, 10, 5, 12, 0, tzinfo=UTC))

    def test_reactivating_a_does_not_disturb_b(self):
        self.start("555", 15, datetime(2026, 10, 5, 7, 20, tzinfo=UTC))
        self.start("777", 60, datetime(2026, 10, 5, 7, 20, tzinfo=UTC))
        self.store.save(
            activate(
                self.store.load("555"), 30, datetime(2026, 10, 5, 10, 0, tzinfo=UTC)
            )
        )
        self.assertEqual(self.store.load("555").interval_minutes, 30)
        self.assertEqual(self.store.load("777").interval_minutes, 60)
        self.assertEqual(self.store.load("777").due_at, datetime(2026, 10, 5, 8, 20, tzinfo=UTC))

    def test_five_chats_can_run_at_once(self):
        at = datetime(2026, 10, 5, 7, 20, tzinfo=UTC)
        for index, minutes in enumerate((15, 30, 60, 180, 15), start=1):
            self.start(str(index), minutes, at)
        self.assertEqual(sorted(self.store.known_chats()), ["1", "2", "3", "4", "5"])
        due = {c.chat_id for c in __import__("reminder.timer", fromlist=["serve_cycles"]).serve_cycles(self.store, at + timedelta(minutes=15))}
        self.assertEqual(due, {"1", "5"}, "only the 15-minute chats are due at 07:35")


class LongNudgeTest(StoreCase):
    """5. There is no ceiling on asking."""

    def test_thirty_consecutive_nudges_are_all_delivered(self):
        at = datetime(2026, 10, 5, 7, 35, tzinfo=UTC)
        self.start("555", 15, at - timedelta(minutes=15))
        bot = Bot()
        cmd_interval(CONFIG, UTC, bot, self.store, now=at)
        for minute in range(5, 5 * 31, 5):
            self.store.release("555")
            cmd_nudge(bot, self.store, now=at + timedelta(minutes=minute))
        self.assertEqual(bot.nudges, 30, "the old build stopped at 12")

    def test_the_nudge_count_keeps_climbing_for_observability(self):
        at = datetime(2026, 10, 5, 7, 35, tzinfo=UTC)
        self.start("555", 15, at - timedelta(minutes=15))
        bot = Bot()
        cmd_interval(CONFIG, UTC, bot, self.store, now=at)
        for minute in range(5, 5 * 16, 5):
            self.store.release("555")
            cmd_nudge(bot, self.store, now=at + timedelta(minutes=minute))
        self.assertEqual(self.store.load("555").nudges, 15)

    def test_the_cycle_is_still_running_after_all_those_nudges(self):
        at = datetime(2026, 10, 5, 7, 35, tzinfo=UTC)
        self.start("555", 15, at - timedelta(minutes=15))
        bot = Bot()
        cmd_interval(CONFIG, UTC, bot, self.store, now=at)
        for minute in range(5, 5 * 31, 5):
            self.store.release("555")
            cmd_nudge(bot, self.store, now=at + timedelta(minutes=minute))
        cycle = self.store.load("555")
        self.assertTrue(cycle.pending)
        self.assertFalse(cycle.stopped)
        self.assertTrue(cycle.is_running)

    def test_only_one_reminder_is_ever_sent_however_long_nudging_runs(self):
        at = datetime(2026, 10, 5, 7, 35, tzinfo=UTC)
        self.start("555", 15, at - timedelta(minutes=15))
        bot = Bot()
        cmd_interval(CONFIG, UTC, bot, self.store, now=at)
        for minute in range(5, 5 * 41, 5):
            self.store.release("555")
            cmd_nudge(bot, self.store, now=at + timedelta(minutes=minute))
        reminders = [t for _, t in bot.sent if not t.startswith("Still open")]
        self.assertEqual(len(reminders), 1, "nudging must not repeat the reminder")


if __name__ == "__main__":
    unittest.main()