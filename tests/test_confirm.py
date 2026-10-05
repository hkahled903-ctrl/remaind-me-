"""Confirmation flow: "did you save it?", the re-ask, and the stop.

The promise this suite defends, in the user's words: set the time once; the
reminder asks; if nobody answers it asks again every few minutes; press "I saved"
and everything stops until a new time is set.

Each test names the promise it protects, because "test_nudge" would not tell a
future reader what breaks if it were deleted.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from reminder.timer import Cycle, FileCycleStore, activate  # noqa: E402
from reminder.commands import (  # noqa: E402
    cmd_interval,
    cmd_nudge,
    set_interval,
    stop_timer,
)
from reminder.confirm import (  # noqa: E402
    MAX_NUDGES,
    NOT_YET,
    SAVED,
    FileConfirm,
    Pending,
    UpstashConfirm,
    ask,
    confirm_store_from_env,
    is_due,
    record_nudge,
    reminder_keyboard,
    resolve,
    seconds_until_next_ask,
)
from reminder.settings import (  # noqa: E402
    FileSettings,
    Settings,
    UpstashSettings,
    settings_store_from_env,
)
from reminder.webapp import ConnectApp  # noqa: E402

CAIRO = timezone(timedelta(hours=2))
START = datetime(2026, 10, 5, 10, 0, tzinfo=CAIRO)
CONFIG = {"telegram_chat_id": "555", "message": "Report ready.", "reminder_time": "09:00"}


class Bot:
    """Records what the user would see."""

    def __init__(self):
        self.sent: list[tuple[str, bool]] = []
        self.answered: list[tuple[str, str]] = []

    def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((text, bool(reply_markup)))

    def answer_callback(self, callback_id, text="", alert=False):
        self.answered.append((callback_id, text))


class ConfirmCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.confirm = FileConfirm(root / "confirm.json")
        self.settings = FileSettings(root / "settings.json")
        self.bot = Bot()
        self.settings.save(Settings(interval_minutes=15))


class DurableDiskCase(ConfirmCase):
    """These tests drive `cmd_interval` against a temp file that outlives the call.

    `interval_mode_is_safe` refuses a plain file store whenever `CI` is set, which
    is right for a GitHub runner and wrong here. The tests declare the exception
    they mean rather than the guard being loosened: it has to stay strict on a
    real runner or the spam protection is gone.
    """

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"REMINDER_ALLOW_FILE_INTERVAL": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)


class NudgeTimingTest(unittest.TestCase):
    """The nudge cadence, measured against the engine the cron actually runs.

    These replace tests that drove a confirm store directly: the question now
    lives inside the chat's cycle, so a "pending" flag and a "last asked" stamp
    in two global records are no longer how the answer is found.
    """

    def setUp(self):
        import tempfile
        from pathlib import Path as _P

        from reminder.timer import Cycle, FileCycleStore, activate

        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = FileCycleStore(_P(self.dir.name) / "timer.json")
        self.store.save(activate(Cycle(chat_id="555"), 15, START))
        self.bot = Bot()

    def beat(self, now):
        cmd_nudge(self.bot, self.store, now=now)
        return [text for text, buttons in self.bot.sent if text.startswith("Still open")]

    def test_f1_a_reminder_carries_two_buttons_and_opens_the_question(self):
        cmd_interval(CONFIG, CAIRO, self.bot, self.store, now=START + timedelta(minutes=15))
        self.assertEqual(len(self.bot.sent), 1)
        self.assertTrue(self.bot.sent[0][1], "the reminder must carry the buttons")
        self.assertTrue(self.store.load("555").pending)

    def test_f2_a_silent_chat_is_asked_again_every_five_minutes(self):
        cmd_interval(CONFIG, CAIRO, self.bot, self.store, now=START + timedelta(minutes=15))
        for minute in range(20, 45, 5):
            self.beat(START + timedelta(minutes=minute))
        self.assertEqual(len([t for t, _ in self.bot.sent if t.startswith("Still open")]), 5)

    def test_f3_a_nudge_before_the_five_minutes_are_up_says_nothing(self):
        cmd_interval(CONFIG, CAIRO, self.bot, self.store, now=START + timedelta(minutes=15))
        self.beat(START + timedelta(minutes=17))
        self.assertEqual(len(self.bot.sent), 1, "only the original reminder")

    def test_f4_the_nagging_has_no_ceiling(self):
        """The old build stopped after 12 nudges and then let the timer restart,
        which the product explicitly forbids. Asking continues until answered."""
        cmd_interval(CONFIG, CAIRO, self.bot, self.store, now=START + timedelta(minutes=15))
        for minute in range(20, 20 + 5 * 40, 5):
            self.beat(START + timedelta(minutes=minute))
        nudges = len([t for t, _ in self.bot.sent if t.startswith("Still open")])
        self.assertGreater(nudges, 12, "a limit here silently restarts the cycle")

    def test_f5_nothing_is_sent_when_no_question_is_open(self):
        self.beat(START)
        self.assertEqual(self.bot.sent, [], "a resting timer is silent")

    def test_f6_every_nudge_carries_the_buttons_too(self):
        cmd_interval(CONFIG, CAIRO, self.bot, self.store, now=START + timedelta(minutes=15))
        self.beat(START + timedelta(minutes=20))
        self.assertTrue(self.bot.sent[-1][1], "a nudge with no buttons is unusable")


class ButtonOwnershipTest(unittest.TestCase):
    def _cycles(self):
        """A cycle store holding one chat with an open question, so the callback
        has something real to authorise against."""
        import tempfile
        from pathlib import Path as _P

        from reminder.timer import Cycle, FileCycleStore, mark_sent

        store = FileCycleStore(_P(tempfile.mkdtemp()) / "timer.json")
        store.save(mark_sent(Cycle(chat_id="555"), START))
        return store

    """The webhook secret proves the update came from Telegram. It does not prove
    it came from the chat that was asked."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.confirm = FileConfirm(Path(tmp.name) / "c.json")
        self.bot = Bot()
        self.saved: list[str] = []
        self.settings = FileSettings(Path(tmp.name) / "s.json")
        self.settings.save(Settings(interval_minutes=15))
        self.app = ConnectApp(
            None,
            secret="s",
            bot_username="b",
            answer=self.bot.answer_callback,
            confirm_store=self.confirm,
            cycle_store=self._cycles(),
            on_saved=self.saved.append,
        )
        self.confirm.save(ask("555", START))

    def press(self, data: str, chat_id: int) -> tuple[int, dict]:
        body = json.dumps(
            {
                "callback_query": {
                    "id": "cb1",
                    "data": data,
                    "message": {"chat": {"id": chat_id}},
                }
            }
        ).encode("utf-8")
        return self.app.webhook("s", body)

    def test_f7_saved_closes_the_question_and_stops_the_timer(self):
        status, result = self.press(SAVED, 555)
        self.assertEqual(status, 200)
        self.assertTrue(result["resolved"])
        self.assertEqual(self.saved, ["555"], "and the schedule is asked to stop")

    def test_f8_not_yet_keeps_the_question_open(self):
        _, result = self.press(NOT_YET, 555)
        self.assertFalse(result["resolved"])
        self.assertEqual(self.saved, [], "nothing was stopped")
        # "Not yet" writes nothing at all: the cycle is already open, and the
        # nudge heartbeat re-asks five minutes after the last message.
        self.assertTrue(self.app.cycle_store.load("555").pending, "still waiting")

    def test_f9_another_chat_cannot_stop_your_timer(self):
        """Reproduced the risk while building: the endpoint is authenticated, so
        it is tempting to trust every press. It is not the same chat."""
        _, result = self.press(NOT_YET, 999)
        self.assertFalse(result["resolved"])
        self.assertEqual(self.saved, [], "a stranger must not stop the timer")
        self.assertIsNotNone(self.confirm.load(), "the real question survives")

    def test_f10_a_press_with_nothing_open_changes_nothing(self):
        from reminder.timer import Cycle

        self.app.cycle_store.save(Cycle(chat_id="555"))
        _, result = self.press(SAVED, 555)
        self.assertFalse(result["resolved"])
        self.assertEqual(self.saved, [])

    def test_f11_the_button_gets_a_visible_answer(self):
        """Telegram spins a clock on the chat until answerCallbackQuery runs, so a
        silent success looks like a frozen button."""
        self.press(SAVED, 555)
        self.assertTrue(self.bot.answered, "the spinner must be closed")

    def test_f12_a_failing_spinner_answer_does_not_undo_the_stop(self):
        class Angry(Bot):
            def answer_callback(self, callback_id, text="", alert=False):
                raise RuntimeError("Telegram is down")

        app = ConnectApp(
            None, secret="s", bot_username="b",
            answer=Angry().answer_callback,
            confirm_store=self.confirm, cycle_store=self._cycles(), on_saved=self.saved.append,
        )
        body = json.dumps(
            {"callback_query": {"id": "cb", "data": SAVED, "message": {"chat": {"id": 555}}}}
        ).encode("utf-8")
        _, result = app.webhook("s", body)
        self.assertTrue(result["resolved"], "a cosmetic failure is not a failed press")
        self.assertEqual(self.saved, ["555"])

    def test_f13_only_the_two_known_actions_stop_anything(self):
        self.assertTrue(resolve(SAVED))
        for action in (NOT_YET, "nonsense", "", "SAVED"):
            with self.subTest(action=action):
                self.assertFalse(resolve(action))


class StoppedTimerTest(unittest.TestCase):
    """STOP is a write to one chat's cycle, and only that chat's."""

    def setUp(self):
        import tempfile
        from pathlib import Path as _P

        from reminder.timer import Cycle, FileCycleStore, activate

        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = FileCycleStore(_P(self.dir.name) / "timer.json")
        self.bot = Bot()
        self.store.save(activate(Cycle(chat_id="555"), 15, START))
        cmd_interval(CONFIG, CAIRO, self.bot, self.store, now=START + timedelta(minutes=15))

    def test_f14_saving_stops_every_later_beat(self):
        stop_timer(self.store, self.bot, "555")
        self.assertTrue(self.store.load("555").stopped)
        for minute in range(20, 200, 5):
            cmd_interval(CONFIG, CAIRO, self.bot, self.store, now=START + timedelta(minutes=minute))
            cmd_nudge(self.bot, self.store, now=START + timedelta(minutes=minute))
        after_stop = self.bot.sent[1:]  # index 0 is the reminder sent before STOP
        self.assertEqual(
            [t for t, _ in after_stop if not t.startswith("Timer is off")],
            [],
            "nothing may be sent after STOP",
        )

    def test_f15_setting_a_new_time_starts_it_again(self):
        stop_timer(self.store, None, "555")
        self.assertTrue(self.store.load("555").stopped)
        set_interval(self.store, 30, "555", now=START + timedelta(hours=2))
        after = self.store.load("555")
        self.assertFalse(after.stopped, "a stopped timer must be resumable")
        self.assertEqual(after.interval_minutes, 30)

    def test_f16_the_stop_is_told_to_the_user_not_done_silently(self):
        stop_timer(self.store, self.bot, "555")
        self.assertTrue(any("Timer is off" in text for text, _ in self.bot.sent))

    def test_f17_one_users_stop_does_not_touch_another(self):
        from reminder.timer import Cycle, activate as act

        self.store.save(act(Cycle(chat_id="777"), 60, START))
        stop_timer(self.store, self.bot, "555")
        self.assertTrue(self.store.load("555").stopped)
        self.assertFalse(self.store.load("777").stopped)


class OpenQuestionBlocksNewRemindersTest(unittest.TestCase):
    def test_f18_an_unanswered_question_becomes_nudges_not_a_second_reminder(self):
        """The user was asked and has not answered. Another "report ready" on top
        of that is nagging twice over, so the follow-ups are nudges."""
        import tempfile
        from pathlib import Path as _P

        from reminder.timer import Cycle, FileCycleStore, activate

        with tempfile.TemporaryDirectory() as d:
            store = FileCycleStore(_P(d) / "timer.json")
            store.save(activate(Cycle(chat_id="555"), 15, START))
            bot = Bot()
            cmd_interval(CONFIG, CAIRO, bot, store, now=START + timedelta(minutes=15))
            cmd_interval(CONFIG, CAIRO, bot, store, now=START + timedelta(minutes=30))
            reminders = [t for t, _ in bot.sent if not t.startswith("Still open")]
            self.assertEqual(len(reminders), 1, "one reminder, then only nudges")


class StoreTest(ConfirmCase):
    def test_f19_a_corrupt_file_reads_as_no_question_rather_than_crash(self):
        import tempfile
        from pathlib import Path as _P

        from reminder.timer import Cycle, FileCycleStore, activate

        with tempfile.TemporaryDirectory() as d:
            store = FileCycleStore(_P(d) / "timer.json")
            store.save(activate(Cycle(chat_id="555"), 15, START))
            store._path.write_text("{not json", encoding="utf-8")
            bot = Bot()
            cmd_nudge(bot, store, now=START + timedelta(hours=1))
            self.assertEqual(bot.sent, [], "unreadable state must not become a message")

    def test_f20_a_pending_without_a_chat_id_is_not_a_question(self):
        self.confirm.save(Pending(chat_id="   "))
        self.assertIsNone(self.confirm.load())

    def test_f21_the_round_trip_keeps_every_field(self):
        original = Pending(chat_id="555", asked_at=START.isoformat(), last_asked_at=START.isoformat(), nudges=4)
        self.confirm.save(original)
        loaded = self.confirm.load()
        self.assertEqual(loaded.chat_id, "555")
        self.assertEqual(loaded.nudges, 4)

    def test_f22_re_asking_does_not_restart_the_original_clock(self):
        pending = ask("555", START)
        nudged = record_nudge(pending, START + timedelta(minutes=5))
        # Stamps are normalised to UTC on the way in, so compare the instants.
        self.assertEqual(
            datetime.fromisoformat(nudged.asked_at),
            datetime.fromisoformat(START.astimezone(timezone.utc).isoformat()),
        )
        self.assertGreater(nudged.nudges, pending.nudges, "the re-ask is counted")
        self.assertNotEqual(
            nudged.last_asked_at, nudged.asked_at, "but the original ask is kept"
        )


class KeyboardTest(unittest.TestCase):
    def test_f23_the_two_buttons_carry_callback_data_telegram_accepts(self):
        """Telegram caps callback_data at 64 bytes; a long one is rejected
        silently and the buttons do nothing."""
        keyboard = reminder_keyboard()
        row = keyboard["inline_keyboard"][0]
        self.assertEqual([b["text"] for b in row], ["I saved it", "Not yet"])
        for button in row:
            self.assertLessEqual(len(button["callback_data"].encode()), 64)


class SecondsUntilAskTest(unittest.TestCase):
    def test_f24_no_open_question_means_no_wait(self):
        self.assertEqual(seconds_until_next_ask(None, START, 5), 0)

    def test_f25_the_countdown_counts_down_to_zero_then_stops(self):
        pending = ask("555", START)
        self.assertEqual(seconds_until_next_ask(pending, START, 5), 300)
        self.assertEqual(seconds_until_next_ask(pending, START + timedelta(minutes=5), 5), 0)


class UpstashSelectionTest(unittest.TestCase):
    """The Upstash branch of each factory: the branch nothing else ever ran.

    Found the hard way. The first GitHub Actions run that had real credentials
    arrived from Infisical died with `object.__init__() takes exactly one
    argument`. Both factories return an Upstash implementation only when *both*
    `UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN` are set, so on a
    laptop, and in this suite until now, that code path was never constructed:
    `UpstashConfirm` and `UpstashSettings` were declared with no base class while
    still calling `super().__init__(url, token, key=key)`, which reached
    `object` and raised before a single request was sent.

    Calling the factory is therefore the whole guard. Asserting the returned
    *type* would not have caught it, because the factory returns that type either
    way -- it is the construction inside the factory call that fails. So these
    tests also check the inherited REST helper is really there.
    """

    CREDENTIALS = {
        "UPSTASH_REDIS_REST_URL": "https://example.invalid",
        "UPSTASH_REDIS_REST_TOKEN": "dummy-token",
    }

    def test_f26_the_confirm_store_is_upstash_when_both_credentials_are_set(self):
        store = confirm_store_from_env(dict(self.CREDENTIALS))
        self.assertIsInstance(store, UpstashConfirm)
        self.assertNotIsInstance(store, FileConfirm, "it must not fall back to a file")
        self.assertEqual(store._url, self.CREDENTIALS["UPSTASH_REDIS_REST_URL"])
        self.assertTrue(
            hasattr(store, "_command"), "the REST helper must be inherited, not reimplemented"
        )

    def test_f27_the_settings_store_is_upstash_when_both_credentials_are_set(self):
        store = settings_store_from_env(dict(self.CREDENTIALS))
        self.assertIsInstance(store, UpstashSettings)
        self.assertNotIsInstance(store, FileSettings, "it must not fall back to a file")
        self.assertEqual(store._url, self.CREDENTIALS["UPSTASH_REDIS_REST_URL"])
        self.assertTrue(
            hasattr(store, "_command"), "the REST helper must be inherited, not reimplemented"
        )


if __name__ == "__main__":
    unittest.main()