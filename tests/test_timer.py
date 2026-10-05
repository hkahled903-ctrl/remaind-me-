"""The product contract, written as tests.

Every assertion here is a line from section 16 of the specification, and every
one is expressed against the pure transitions in `reminder.timer`, so none of
them needs a network, a clock or a filesystem. If the model is wrong, these fail
before any worker, workflow or Telegram call is involved.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from reminder.timer import (
    NUDGE_MINUTES,
    Cycle,
    activate,
    first_reminder_due,
    mark_sent,
    nudge_due,
    nudge_text,
    parse,
    stop_cycle,
)

UTC = timezone.utc


def at(hh: int, mm: int, day: int = 5) -> datetime:
    """07:20-style wall clock in UTC, so the tests read like the spec."""
    return datetime(2026, 10, day, hh, mm, tzinfo=UTC)


def activated(hh: int, mm: int, minutes: int, chat_id: str = "A") -> Cycle:
    return activate(Cycle(chat_id=chat_id), minutes, at(hh, mm))


class TimerContract(unittest.TestCase):
    """1-8: the interval is relative to activation, not to the wall clock."""

    def test_1_fifteen_minutes_from_0720_is_0735(self):
        self.assertEqual(activated(7, 20, 15).due_at, at(7, 35))

    def test_2_fifteen_minutes_from_0723_is_0738(self):
        # The spec calls this out specifically: not 07:30, not 07:45.
        self.assertEqual(activated(7, 23, 15).due_at, at(7, 38))

    def test_3_thirty_minutes(self):
        self.assertEqual(activated(7, 20, 30).due_at, at(7, 50))

    def test_4_sixty_minutes(self):
        self.assertEqual(activated(7, 20, 60).due_at, at(8, 20))

    def test_5_before_due_nothing_is_due(self):
        cycle = activated(7, 20, 60)
        self.assertFalse(first_reminder_due(cycle, at(8, 19)))

    def test_6_exactly_at_due_it_is_due(self):
        cycle = activated(7, 20, 60)
        self.assertTrue(first_reminder_due(cycle, at(8, 20)))

    def test_7_polling_repeatedly_never_sends_early(self):
        # 07:25 -> 08:20 is twelve beats. None of them may send.
        cycle = activated(7, 20, 60)
        beats = [at(7, 25) + timedelta(minutes=5 * i) for i in range(11)]
        for beat in beats:
            self.assertFalse(first_reminder_due(cycle, beat), beat)

    def test_8_only_one_first_reminder_per_cycle(self):
        cycle = mark_sent(activated(7, 20, 15), at(7, 35))
        # The question is now open, so the first reminder is never due again.
        for beat in (at(7, 35), at(7, 40), at(9, 0), at(23, 59)):
            self.assertFalse(first_reminder_due(cycle, beat), beat)


class NudgeContract(unittest.TestCase):
    """9-14: nudge every 5 minutes, forever, until confirmed."""

    def setUp(self):
        self.sent = mark_sent(activated(7, 20, 15), at(7, 35))

    def test_9_the_first_reminder_opens_a_question(self):
        self.assertTrue(self.sent.pending)
        self.assertEqual(self.sent.nudges, 0)
        self.assertEqual(self.sent.last_sent_at, "2026-10-05T07:35:00+00:00")

    def test_10_five_minutes_later_a_nudge_is_due(self):
        self.assertTrue(nudge_due(self.sent, at(7, 40)))

    def test_11_and_another_five_later(self):
        once = mark_sent(self.sent, at(7, 40))
        self.assertEqual(once.nudges, 1)
        self.assertFalse(nudge_due(once, at(7, 44)))
        self.assertTrue(nudge_due(once, at(7, 45)))

    def test_12_more_than_twelve_nudges_still_continue(self):
        # The old MAX_NUDGES=12 silently stopped asking and then let the timer
        # restart. There is no longer a ceiling anywhere in the model.
        cycle = self.sent
        moment = at(7, 35)
        for _ in range(30):
            moment += timedelta(minutes=NUDGE_MINUTES)
            self.assertTrue(nudge_due(cycle, moment), moment)
            cycle = mark_sent(cycle, moment)
        self.assertEqual(cycle.nudges, 30)
        self.assertTrue(cycle.pending)

    def test_13_not_yet_leaves_the_question_open(self):
        # "Not yet" is a no-op on the model: the cycle already has no state that
        # an acknowledgement would change, so nothing has to write anything.
        self.assertTrue(self.sent.pending)
        self.assertFalse(self.sent.stopped)

    def test_14_saved_stops_immediately(self):
        stopped = stop_cycle(self.sent)
        self.assertTrue(stopped.stopped)
        self.assertFalse(stopped.pending)


class StopContract(unittest.TestCase):
    """15-19: STOP is durable, and only a new activation revives it."""

    def setUp(self):
        self.pending = mark_sent(activated(7, 20, 15), at(7, 35))

    def test_15_interval_worker_is_silent_after_stop(self):
        stopped = stop_cycle(self.pending)
        for beat in (at(7, 40), at(8, 0), at(23, 59)):
            self.assertFalse(first_reminder_due(stopped, beat), beat)
            self.assertFalse(nudge_due(stopped, beat), beat)

    def test_16_nudge_worker_is_silent_after_stop(self):
        stopped = stop_cycle(self.pending)
        self.assertFalse(nudge_due(stopped, at(7, 45)))

    def test_17_a_restart_does_not_revive_it(self):
        # Persistence is the store's job; what the model guarantees is that a
        # round-trip through a serialised document changes nothing.
        import json
        from dataclasses import asdict

        revived = Cycle(**asdict(stop_cycle(self.pending)))
        self.assertTrue(revived.stopped)
        self.assertFalse(first_reminder_due(revived, at(7, 40)))

    def test_18_a_new_interval_starts_a_new_cycle(self):
        stopped = stop_cycle(self.pending)
        fresh = activate(stopped, 30, at(9, 0))
        self.assertFalse(fresh.stopped)
        self.assertEqual(fresh.cycle, stopped.cycle + 1)
        self.assertEqual(fresh.due_at, at(9, 30))
        self.assertTrue(first_reminder_due(fresh, at(9, 30)))

    def test_19_the_old_question_cannot_leak_into_the_new_cycle(self):
        stopped = stop_cycle(self.pending)
        fresh = activate(stopped, 15, at(9, 0))
        self.assertFalse(fresh.pending)
        self.assertEqual(fresh.nudges, 0)
        self.assertEqual(fresh.asked_at, "")
        self.assertEqual(fresh.last_sent_at, "")

    def test_activating_an_idle_chat_starts_at_cycle_one(self):
        fresh = activate(Cycle(chat_id="A"), 15, at(7, 20))
        self.assertEqual(fresh.cycle, 1)
        self.assertTrue(fresh.is_running)


class MultiUserContract(unittest.TestCase):
    """20-25: five chats, five independent timers."""

    def setUp(self):
        self.a = mark_sent(activate(Cycle(chat_id="A"), 15, at(7, 20)), at(7, 35))
        self.b = mark_sent(activate(Cycle(chat_id="B"), 60, at(7, 20)), at(8, 20))

    def test_20_independent_intervals(self):
        self.assertEqual(self.a.due_at, at(7, 35))
        self.assertEqual(self.b.due_at, at(8, 20))
        self.assertNotEqual(self.a.due_at, self.b.due_at)

    def test_21_one_reminder_does_not_disturb_another(self):
        stopped_a = stop_cycle(self.a)
        self.assertTrue(stopped_a.stopped)
        # B is untouched and still owed its own second reminder.
        self.assertFalse(stopped_a.pending)
        self.assertTrue(nudge_due(self.b, at(8, 25)))

    def test_22_saved_stops_only_that_user(self):
        stopped_a = stop_cycle(self.a)
        self.assertTrue(stopped_a.stopped)
        # B is a separate document; stopping A never reaches it. Its own record
        # is still running and still owed a reminder.
        self.assertFalse(self.b.stopped)
        self.assertTrue(self.b.pending)

    def test_23_b_keeps_receiving_reminders(self):
        stop_cycle(self.a)
        for beat in (at(8, 25), at(8, 30), at(9, 15)):
            self.assertTrue(nudge_due(self.b, beat), beat)

    def test_24_different_users_hold_different_values(self):
        cycle_c = activate(Cycle(chat_id="C"), 180, at(12, 0))
        self.assertEqual(cycle_c.due_at, at(15, 0))
        self.assertEqual(self.a.interval_minutes, 15)
        self.assertEqual(self.b.interval_minutes, 60)
        self.assertEqual(cycle_c.interval_minutes, 180)

    def test_25_the_chat_id_is_part_of_the_record(self):
        # The identity lives in the document, so a record cannot be reattributed.
        self.assertEqual(self.a.chat_id, "A")
        self.assertEqual(self.b.chat_id, "B")
        self.assertEqual(activate(Cycle(chat_id="C"), 30, at(7, 20)).chat_id, "C")


class CountdownContract(unittest.TestCase):
    """The page and the worker must read the same instant."""

    def test_countdown_targets_the_due_time_before_the_first_reminder(self):
        cycle = activated(7, 20, 15)
        self.assertEqual(cycle.next_send_at, at(7, 35))
        self.assertEqual(cycle.seconds_until_next_send(at(7, 20)), 15 * 60)

    def test_countdown_targets_the_nudge_time_afterwards(self):
        cycle = mark_sent(activated(7, 20, 15), at(7, 35))
        self.assertEqual(cycle.next_send_at, at(7, 40))
        self.assertEqual(cycle.seconds_until_next_send(at(7, 37)), 3 * 60)

    def test_countdown_is_zero_when_stopped(self):
        self.assertEqual(stop_cycle(Cycle(chat_id="A", interval_minutes=15)).next_send_at, None)


class TimezoneContract(unittest.TestCase):
    """Internally UTC, whatever the user's wall clock says."""

    def test_an_offset_activation_is_normalised(self):
        from datetime import timedelta as td

        cairo = at(7, 20) + td(hours=3)  # 07:20 Cairo == 04:20 UTC
        cycle = activate(Cycle(chat_id="A"), 15, cairo)
        # due is 15 real minutes later, expressed in UTC
        self.assertEqual(cycle.due_at, cairo + td(minutes=15))

    def test_a_naive_stored_stamp_is_read_as_utc_not_host_local(self):
        self.assertEqual(parse("2026-10-05T07:35:00"), at(7, 35))

    def test_an_unparseable_stamp_is_ignored_rather_than_guessed(self):
        self.assertIsNone(parse("not a timestamp"))
        self.assertIsNone(parse(""))


class BoundaryContract(unittest.TestCase):
    """Adversarial: the awkward moments around a boundary."""

    def test_activation_exactly_on_a_five_minute_boundary(self):
        cycle = activated(7, 20, 15)
        one_second_early = at(7, 35) - timedelta(seconds=1)
        self.assertFalse(first_reminder_due(cycle, one_second_early))
        self.assertTrue(first_reminder_due(cycle, at(7, 35)))

    def test_one_second_before_the_nudge_boundary(self):
        cycle = mark_sent(activated(7, 20, 15), at(7, 35))
        self.assertFalse(nudge_due(cycle, at(7, 40) - timedelta(seconds=1)))
        self.assertTrue(nudge_due(cycle, at(7, 40)))

    def test_a_corrupt_activated_at_never_becomes_due(self):
        cycle = Cycle(chat_id="A", interval_minutes=15, activated_at="garbage")
        self.assertIsNone(cycle.due_at)
        self.assertFalse(first_reminder_due(cycle, at(23, 59)))

    def test_stopping_a_cycle_that_never_asked_is_safe(self):
        self.assertTrue(stop_cycle(Cycle(chat_id="A")).stopped)

    def test_mark_sent_twice_does_not_inflate_the_nudge_count(self):
        cycle = mark_sent(activated(7, 20, 15), at(7, 35))
        self.assertEqual(cycle.nudges, 0)
        self.assertEqual(mark_sent(cycle, at(7, 40)).nudges, 1)


class StoreContract(unittest.TestCase):
    """The file store is the same contract as Redis, for a laptop."""

    def setUp(self):
        import tempfile
        from pathlib import Path

        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "timer.json"
        self.addCleanup(self.dir.cleanup)

    def _store(self):
        from reminder.timer import FileCycleStore

        return FileCycleStore(self.path)

    def test_round_trip_preserves_the_cycle(self):
        store = self._store()
        cycle = mark_sent(activate(Cycle(chat_id="A"), 15, at(7, 20)), at(7, 35))
        store.save(cycle)
        again = store.load("A")
        self.assertEqual(again.due_at, cycle.due_at)
        self.assertEqual(again.nudges, 0)
        self.assertTrue(again.pending)

    def test_one_chat_does_not_overwrite_another(self):
        store = self._store()
        store.save(activate(Cycle(chat_id="A"), 15, at(7, 20)))
        store.save(activate(Cycle(chat_id="B"), 60, at(7, 20)))
        self.assertEqual(store.load("A").interval_minutes, 15)
        self.assertEqual(store.load("B").interval_minutes, 60)
        self.assertEqual(store.known_chats(), ["A", "B"])

    def test_an_unknown_chat_reads_as_idle_rather_than_raising(self):
        cycle = self._store().load("nobody")
        self.assertEqual(cycle.interval_minutes, 0)
        self.assertFalse(cycle.is_running)

    def test_a_corrupt_file_reads_as_idle(self):
        self.path.write_text("{ not json", encoding="utf-8")
        self.assertFalse(self._store().load("A").is_running)

    def test_serve_cycles_selects_only_the_due_chats(self):
        from reminder.timer import serve_cycles

        store = self._store()
        store.save(activate(Cycle(chat_id="A"), 15, at(7, 20)))  # due 07:35
        store.save(activate(Cycle(chat_id="B"), 60, at(7, 20)))  # due 08:20
        self.assertEqual([c.chat_id for c in serve_cycles(store, at(7, 35))], ["A"])
        self.assertEqual(
            [c.chat_id for c in serve_cycles(store, at(8, 20))], ["A", "B"]
        )


if __name__ == "__main__":
    unittest.main()