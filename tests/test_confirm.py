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

from reminder.commands import (  # noqa: E402
    ask_with_buttons,
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


class NudgeTimingTest(ConfirmCase):
    def test_f1_a_reminder_carries_two_buttons_and_opens_the_question(self):
        ask_with_buttons(self.bot, self.confirm, "555", "Report ready.", START)
        self.assertEqual(len(self.bot.sent), 1)
        self.assertTrue(self.bot.sent[0][1], "the reminder must carry the buttons")
        self.assertEqual(self.confirm.load().chat_id, "555")

    def test_f2_a_silent_chat_is_asked_again_every_five_minutes(self):
        ask_with_buttons(self.bot, self.confirm, "555", "Report ready.", START)
        for minute in range(5, 26, 5):
            now = START + timedelta(minutes=minute)
            cmd_nudge(self.bot, self.confirm, now=now, every_minutes=5)
        nudges = [m for m in self.bot.sent if m[0].startswith("Still open")]
        self.assertEqual(len(nudges), 5, "one re-ask per five minutes")

    def test_f3_a_nudge_before_the_five_minutes_are_up_says_nothing(self):
        ask_with_buttons(self.bot, self.confirm, "555", "Report ready.", START)
        cmd_nudge(self.bot, self.confirm, now=START + timedelta(minutes=2))
        self.assertEqual(len(self.bot.sent), 1, "only the original reminder")

    def test_f4_the_asking_ceiling_stops_the_nagging(self):
        """Otherwise a chat that never answers gets 288 messages a day, which is
        the exact failure the reminder was built to avoid."""
        ask_with_buttons(self.bot, self.confirm, "555", "Report ready.", START)
        for minute in range(5, 5 * (MAX_NUDGES + 6), 5):
            cmd_nudge(self.bot, self.confirm, now=START + timedelta(minutes=minute))
        nudges = [m for m in self.bot.sent if m[0].startswith("Still open")]
        self.assertLessEqual(len(nudges), MAX_NUDGES)

    def test_f5_nothing_is_sent_when_no_question_is_open(self):
        cmd_nudge(self.bot, self.confirm, now=START)
        self.assertEqual(self.bot.sent, [], "a resting timer is silent")

    def test_f6_every_nudge_carries_the_buttons_too(self):
        ask_with_buttons(self.bot, self.confirm, "555", "Report ready.", START)
        cmd_nudge(self.bot, self.confirm, now=START + timedelta(minutes=5))
        self.assertTrue(self.bot.sent[-1][1], "a nudge with no buttons is unusable")


class ButtonOwnershipTest(unittest.TestCase):
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
        self.assertIsNone(self.confirm.load(), "the question is closed")
        self.assertEqual(self.saved, ["555"], "and the schedule is asked to stop")

    def test_f8_not_yet_keeps_the_question_open(self):
        _, result = self.press(NOT_YET, 555)
        self.assertFalse(result["resolved"])
        self.assertIsNotNone(self.confirm.load(), "still waiting for an answer")
        self.assertEqual(self.saved, [], "nothing was stopped")

    def test_f9_another_chat_cannot_stop_your_timer(self):
        """Reproduced the risk while building: the endpoint is authenticated, so
        it is tempting to trust every press. It is not the same chat."""
        _, result = self.press(NOT_YET, 999)
        self.assertFalse(result["resolved"])
        self.assertEqual(self.saved, [], "a stranger must not stop the timer")
        self.assertIsNotNone(self.confirm.load(), "the real question survives")

    def test_f10_a_press_with_nothing_open_changes_nothing(self):
        self.confirm.clear()
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
            confirm_store=self.confirm, on_saved=self.saved.append,
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


class StoppedTimerTest(DurableDiskCase):
    def test_f14_saved_stops_the_next_period_too_not_just_the_nudge(self):
        """Reproduced while building this: clearing the question alone left
        interval_minutes at 15, so the next period fired as if nothing happened.
        The whole feature is that this does not happen."""
        stop_timer(self.settings, self.bot, "555")
        self.assertTrue(self.settings.load().stopped)
        for period in range(1, 5):
            now = START + timedelta(minutes=15 * (period + 5))
            settings = self.settings.load()
            settings.last_slot = str(int(now.timestamp()) // 900 - 1)
            self.settings.save(settings)
            cmd_interval(CONFIG, CAIRO, self.bot, self.settings, None, now=now, confirm_store=self.confirm)
        self.assertEqual(self.bot.sent, [("Timer is off. No reminders will be sent until you set a new time.", False)])

    def test_f15_setting_a_new_time_starts_it_again(self):
        stop_timer(self.settings)
        self.assertTrue(self.settings.load().stopped)
        set_interval(self.settings, "15", self.confirm)
        after = self.settings.load()
        self.assertFalse(after.stopped, "a stopped timer must be resumable")
        self.assertEqual(after.interval_minutes, 15)

    def test_f16_the_stop_is_told_to_the_user_not_done_silently(self):
        stop_timer(self.settings, self.bot, "555")
        self.assertTrue(any("Timer is off" in text for text, _ in self.bot.sent))

    def test_f17_a_missing_confirmation_store_does_not_break_the_stop(self):
        stop_timer(self.settings, self.bot, "555")
        self.assertTrue(self.settings.load().stopped)


class OpenQuestionBlocksNewRemindersTest(DurableDiskCase):
    def test_f18_an_unanswered_question_stops_the_next_period_too(self):
        """The user was asked and has not answered. Another reminder on top of
        that is nagging twice over."""
        cmd_interval(CONFIG, CAIRO, self.bot, self.settings, None, now=START, confirm_store=self.confirm)
        self.assertEqual(len(self.bot.sent), 1, "the question went out")
        later = START + timedelta(minutes=15)
        settings = self.settings.load()
        settings.last_slot = str(int(later.timestamp()) // 900 - 1)
        self.settings.save(settings)
        cmd_interval(CONFIG, CAIRO, self.bot, self.settings, None, now=later, confirm_store=self.confirm)
        self.assertEqual(len(self.bot.sent), 1, "no second reminder while a question is open")


class StoreTest(ConfirmCase):
    def test_f19_a_corrupt_file_reads_as_no_question_rather_than_crash(self):
        self.confirm.save(ask("555", START))
        self.confirm._path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(self.confirm.load())
        cmd_nudge(self.bot, self.confirm, now=START)
        self.assertEqual(self.bot.sent, [], "unreadable state must not become a message")

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