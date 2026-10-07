from __future__ import annotations

import threading
import time
import unittest
from datetime import datetime, timezone
from unittest import mock

from reminder.bot import TimerBot
from reminder.scheduler import DeliveryError, run_tick
from reminder.storage import (
    CALLBACK_SCRIPT,
    CLAIM_SCRIPT,
    FINISH_SCRIPT,
    RELEASE_SCRIPT,
    START_SCRIPT,
    RedisError,
    TimerStore,
)
from reminder.timer import callback_data, new_id, parse_callback_data, parse_duration

from tests.redis_support import test_store

MINUTE = 60_000
DUE = int(datetime(2026, 10, 7, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
CHAT = "123456"


class HelpersTest(unittest.TestCase):
    def test_duration_is_whole_minutes_in_range(self):
        self.assertEqual(parse_duration("30"), 1800)
        for invalid in ("0", "1441", "1.5", "-2", "x"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                parse_duration(invalid)

    def test_callback_has_both_identities_and_fits_telegram_limit(self):
        timer_id, confirmation_id = new_id(), new_id()
        encoded = callback_data("yes", timer_id, confirmation_id)
        self.assertLessEqual(len(encoded.encode()), 64)
        self.assertEqual(
            parse_callback_data(encoded), ("yes", timer_id, confirmation_id)
        )
        self.assertIsNone(parse_callback_data("yes|stale"))

    def test_atomic_scripts_encode_the_required_guards(self):
        self.assertIn("state ~= 'IDLE'", START_SCRIPT)
        self.assertIn("expires_at > now", CLAIM_SCRIPT)
        self.assertIn("next_reminder_at > now", CLAIM_SCRIPT)
        self.assertIn("delivery_token", FINISH_SCRIPT)
        self.assertIn("delivery_token", RELEASE_SCRIPT)
        self.assertIn("confirmation_id') ~= ARGV[2]", CALLBACK_SCRIPT)
        self.assertIn("lease_until > tonumber(ARGV[3])", CALLBACK_SCRIPT)

    def test_redis_errors_do_not_become_empty_results(self):
        store = TimerStore("https://redis.invalid", "secret")
        with mock.patch.object(store, "command", side_effect=RedisError("unavailable")):
            with self.assertRaises(RedisError):
                store.due_chats(DUE)


class TelegramRecorder:
    def __init__(self, fail=False):
        self.sent: list[tuple[str, str, dict | None]] = []
        self.answers: list[tuple[str, str, bool]] = []
        self.fail = fail
        self.lock = threading.Lock()

    def send_message(self, chat_id, text, reply_markup=None):
        if self.fail:
            raise RuntimeError("Telegram unavailable")
        with self.lock:
            self.sent.append((chat_id, text, reply_markup))
        return {"ok": True}

    def answer_callback(self, callback_id, text="", alert=False):
        self.answers.append((callback_id, text, alert))
        return {"ok": True}


class TimerProductRedisTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = test_store()
        if cls.store is None:
            raise unittest.SkipTest("set TIMER_TEST_REDIS_URL to run Redis Lua integration tests")
        try:
            cls.store.command("PING")
        except Exception as exc:
            raise AssertionError(
                f"configured test Redis is not reachable: {exc}"
            ) from exc

    def setUp(self):
        self.store.command("FLUSHDB")
        self.bot = TelegramRecorder()
        self.app = TimerBot(self.store, self.bot)

    def start(self, duration_seconds=1800, chat_id=CHAT, at=DUE):
        timer_id = new_id()
        self.assertTrue(self.store.start(chat_id, duration_seconds, at, timer_id))
        return timer_id

    def callback_update(self, action, timer_id, confirmation_id, chat_id=CHAT, query_id="q1"):
        return {
            "callback_query": {
                "id": query_id,
                "from": {"id": int(chat_id)},
                "data": callback_data(action, timer_id, confirmation_id),
                "message": {
                    "message_id": 1,
                    "chat": {"id": int(chat_id), "type": "private"},
                },
            }
        }

    def test_timer_creation_persists_timestamps_and_only_one_active_timer(self):
        timer_id = self.start()
        timer = self.store.get(CHAT)
        self.assertEqual(timer["state"], "RUNNING")
        self.assertEqual(timer["duration_seconds"], "1800")
        self.assertEqual(timer["started_at"], str(DUE))
        self.assertEqual(timer["expires_at"], str(DUE + 30 * MINUTE))
        self.assertEqual(timer["timer_id"], timer_id)
        self.assertEqual(timer["next_reminder_at"], "")
        self.assertFalse(self.store.start(CHAT, 900, DUE, new_id()))
        self.assertEqual(self.store.due_chats(DUE + 30 * MINUTE), [CHAT])

    def test_website_link_is_pending_then_telegram_start_links_server_side_chat(self):
        session_id, link_token = new_id(), new_id()
        self.store.create_website_link(session_id, link_token)
        self.assertEqual(self.store.get_website_session(session_id)["state"], "PENDING")

        result = self.app.handle_update(
            {
                "message": {
                    "chat": {"id": int(CHAT), "type": "private"},
                    "text": f"/start {link_token}",
                }
            }
        )
        self.assertEqual(result[0], 200)
        session = self.store.get_website_session(session_id)
        self.assertEqual(session["state"], "LINKED")
        self.assertEqual(session["chat_id"], CHAT)

    def test_invalid_or_expired_website_link_token_is_rejected(self):
        self.assertEqual(self.store.complete_website_link(new_id(), CHAT), "invalid")
        session_id, link_token = new_id(), new_id()
        self.store.create_website_link(session_id, link_token, ttl_seconds=1)
        time.sleep(1.1)
        self.assertEqual(self.store.complete_website_link(link_token, CHAT), "invalid")
        self.assertIsNone(self.store.get_website_session(session_id))

    def test_website_link_duplicate_is_safe_and_token_cannot_link_another_chat(self):
        session_id, link_token = new_id(), new_id()
        self.store.create_website_link(session_id, link_token)
        update = {
            "message": {
                "chat": {"id": int(CHAT), "type": "private"},
                "text": f"/start {link_token}",
            }
        }
        self.assertEqual(self.app.handle_update(update)[0], 200)
        self.assertEqual(self.app.handle_update(update)[0], 200)
        self.assertEqual(self.store.complete_website_link(link_token, "654321"), "invalid")
        self.assertEqual(self.store.get_website_session(session_id)["chat_id"], CHAT)

    def test_concurrent_website_link_attempts_can_bind_only_one_chat(self):
        session_id, link_token = new_id(), new_id()
        self.store.create_website_link(session_id, link_token)
        barrier = threading.Barrier(3)
        results = []

        def complete(chat_id):
            barrier.wait()
            results.append(self.store.complete_website_link(link_token, chat_id))

        threads = [
            threading.Thread(target=complete, args=(CHAT,)),
            threading.Thread(target=complete, args=("654321",)),
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)
        self.assertCountEqual(results, ["linked", "invalid"])
        self.assertIn(
            self.store.get_website_session(session_id)["chat_id"],
            {CHAT, "654321"},
        )

    def test_simultaneous_starts_create_only_one_active_timer(self):
        barrier = threading.Barrier(3)
        outcomes = []

        def start(duration):
            barrier.wait()
            outcomes.append(self.store.start(CHAT, duration, DUE, new_id()))

        threads = [
            threading.Thread(target=start, args=(30 * 60,)),
            threading.Thread(target=start, args=(60 * 60,)),
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)
        self.assertCountEqual(outcomes, [0, 1])
        self.assertIn(self.store.get(CHAT)["duration_seconds"], {"1800", "3600"})

    def test_expiration_transitions_once_and_sends_confirmation_once(self):
        timer_id = self.start()
        result = run_tick(self.store, self.bot, DUE + 30 * MINUTE)
        timer = self.store.get(CHAT)
        self.assertEqual(result["delivered"], 1)
        self.assertEqual(timer["state"], "WAITING_CONFIRMATION")
        self.assertEqual(timer["timer_id"], timer_id)
        self.assertEqual(int(timer["next_reminder_at"]), DUE + 35 * MINUTE)
        self.assertEqual(len(self.bot.sent), 1)
        self.assertEqual(self.bot.sent[0][1], "⏰ Time's up!\n\nDid you save it?")
        self.assertEqual(run_tick(self.store, self.bot, DUE + 30 * MINUTE)["delivered"], 0)
        self.assertEqual(len(self.bot.sent), 1)

    def test_yes_stops_everything_and_duplicate_yes_is_harmless(self):
        timer_id = self.start()
        run_tick(self.store, self.bot, DUE + 30 * MINUTE)
        timer = self.store.get(CHAT)
        confirmation_id = timer["confirmation_id"]
        update = self.callback_update("yes", timer_id, confirmation_id)
        with mock.patch("reminder.bot.now_ms", return_value=DUE + 31 * MINUTE):
            first = self.app.handle_update(update)
            duplicate = self.app.handle_update(update)
        self.assertTrue(first[1]["changed"])
        self.assertFalse(duplicate[1]["changed"])
        self.assertEqual(self.store.get(CHAT)["state"], "IDLE")
        self.assertEqual(self.store.get(CHAT)["next_reminder_at"], "")
        self.assertEqual(self.store.due_chats(DUE + 5 * 60 * MINUTE), [])
        self.assertEqual(len(self.bot.answers), 2)

    def test_more_time_restarts_duration_and_duplicate_click_does_not_restart_again(self):
        timer_id = self.start(45 * 60)
        run_tick(self.store, self.bot, DUE + 45 * MINUTE)
        confirmation_id = self.store.get(CHAT)["confirmation_id"]
        update = self.callback_update("more", timer_id, confirmation_id)
        with mock.patch("reminder.bot.now_ms", return_value=DUE + 46 * MINUTE):
            first = self.app.handle_update(update)
            duplicate = self.app.handle_update(update)
        timer = self.store.get(CHAT)
        self.assertTrue(first[1]["changed"])
        self.assertFalse(duplicate[1]["changed"])
        self.assertEqual(timer["state"], "RUNNING")
        self.assertEqual(timer["duration_seconds"], str(45 * 60))
        self.assertEqual(timer["started_at"], str(DUE + 46 * MINUTE))
        self.assertEqual(timer["expires_at"], str(DUE + 91 * MINUTE))
        self.assertNotEqual(timer["timer_id"], timer_id)
        self.assertEqual(timer["next_reminder_at"], "")

    def test_simultaneous_more_time_clicks_create_one_new_timer(self):
        timer_id = self.start()
        run_tick(self.store, self.bot, DUE + 30 * MINUTE)
        confirmation_id = self.store.get(CHAT)["confirmation_id"]
        update = self.callback_update("more", timer_id, confirmation_id)
        barrier = threading.Barrier(3)
        results = []

        def press():
            barrier.wait()
            results.append(self.app.handle_update(update)[1]["changed"])

        threads = [threading.Thread(target=press) for _ in range(2)]
        with mock.patch("reminder.bot.now_ms", return_value=DUE + 31 * MINUTE):
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join(timeout=5)
        self.assertCountEqual(results, [True, False])
        self.assertEqual(self.store.get(CHAT)["state"], "RUNNING")
        self.assertEqual(self.store.get(CHAT)["started_at"], str(DUE + 31 * MINUTE))

    def test_duplicate_start_webhook_cannot_restart_a_completed_timer(self):
        update = {
            "update_id": 987,
            "message": {
                "chat": {"id": int(CHAT), "type": "private"},
                "text": "/timer 30",
            },
        }
        with mock.patch("reminder.bot.now_ms", return_value=DUE):
            first = self.app.handle_update(update)
        timer_id = self.store.get(CHAT)["timer_id"]
        run_tick(self.store, self.bot, DUE + 30 * MINUTE)
        confirmation_id = self.store.get(CHAT)["confirmation_id"]
        with mock.patch("reminder.bot.now_ms", return_value=DUE + 31 * MINUTE):
            self.app.handle_update(self.callback_update("yes", timer_id, confirmation_id))
            retry = self.app.handle_update(update)
        self.assertTrue(first[1]["started"])
        self.assertTrue(retry[1]["duplicate"])
        self.assertEqual(self.store.get(CHAT)["state"], "IDLE")

    def test_unanswered_timer_repeats_every_five_minutes_until_yes(self):
        timer_id = self.start()
        expiration_at = DUE + 30 * MINUTE
        run_tick(self.store, self.bot, expiration_at)
        timer = self.store.get(CHAT)
        confirmation_id = timer["confirmation_id"]
        run_tick(self.store, self.bot, expiration_at + 5 * MINUTE)
        run_tick(self.store, self.bot, expiration_at + 10 * MINUTE)
        self.assertEqual(len(self.bot.sent), 3)
        self.assertEqual(
            int(self.store.get(CHAT)["next_reminder_at"]),
            expiration_at + 15 * MINUTE,
        )
        yes = self.callback_update("yes", timer_id, confirmation_id)
        with mock.patch("reminder.bot.now_ms", return_value=expiration_at + 11 * MINUTE):
            self.app.handle_update(yes)
        self.assertEqual(run_tick(self.store, self.bot, expiration_at + 15 * MINUTE)["delivered"], 0)
        self.assertEqual(len(self.bot.sent), 3)

    def test_concurrent_workers_claim_a_five_minute_reminder_once(self):
        self.start()
        expiration_at = DUE + 30 * MINUTE
        run_tick(self.store, self.bot, expiration_at)
        reminder_at = expiration_at + 5 * MINUTE
        barrier = threading.Barrier(3)
        errors: list[Exception] = []

        def worker():
            try:
                barrier.wait()
                run_tick(self.store, self.bot, reminder_at)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(errors, [])
        self.assertEqual(len(self.bot.sent), 2)
        self.assertEqual(
            int(self.store.get(CHAT)["next_reminder_at"]),
            reminder_at + 5 * MINUTE,
        )

    def test_stale_callback_cannot_change_a_new_timer(self):
        old_timer = self.start()
        run_tick(self.store, self.bot, DUE + 30 * MINUTE)
        old_confirmation = self.store.get(CHAT)["confirmation_id"]
        with mock.patch("reminder.bot.now_ms", return_value=DUE + 31 * MINUTE):
            self.app.handle_update(self.callback_update("yes", old_timer, old_confirmation))
        new_timer = self.start(900, at=DUE + 32 * MINUTE)
        stale = self.callback_update("more", old_timer, old_confirmation)
        with mock.patch("reminder.bot.now_ms", return_value=DUE + 33 * MINUTE):
            result = self.app.handle_update(stale)
        self.assertFalse(result[1]["changed"])
        self.assertEqual(self.store.get(CHAT)["state"], "RUNNING")
        self.assertEqual(self.store.get(CHAT)["timer_id"], new_timer)

    def test_concurrent_workers_claim_an_expiration_once(self):
        self.start()
        barrier = threading.Barrier(3)
        errors: list[Exception] = []

        def worker():
            try:
                barrier.wait()
                run_tick(self.store, self.bot, DUE + 30 * MINUTE)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(errors, [])
        self.assertEqual(len(self.bot.sent), 1)

    def test_telegram_failure_keeps_notification_retryable(self):
        self.start()
        failing = TelegramRecorder(fail=True)
        with self.assertRaises(DeliveryError):
            run_tick(self.store, failing, DUE + 30 * MINUTE)
        timer = self.store.get(CHAT)
        self.assertEqual(timer["state"], "WAITING_CONFIRMATION")
        self.assertNotEqual(timer["pending_delivery_id"], "")
        self.assertEqual(timer["delivery_token"], "")
        self.assertEqual(
            run_tick(self.store, self.bot, DUE + 30 * MINUTE + 30_000)["delivered"], 1
        )
        self.assertEqual(len(self.bot.sent), 1)

    def test_restart_uses_persisted_timer_state(self):
        timer_id = self.start(60 * 60)
        restarted_store = test_store()
        timer = restarted_store.get(CHAT)
        self.assertEqual(timer["timer_id"], timer_id)
        self.assertEqual(timer["state"], "RUNNING")
        self.assertEqual(timer["expires_at"], str(DUE + 60 * MINUTE))

    def test_callback_is_deferred_while_scheduler_delivery_holds_lease(self):
        timer_id = self.start()
        claim = self.store.claim_due(
            CHAT, DUE + 30 * MINUTE, new_id(), new_id(), new_id(),
            DUE + 30 * MINUTE + 120_000,
        )
        self.assertIsNotNone(claim)
        result = self.store.callback(
            CHAT, "yes", timer_id, claim["confirmation_id"],
            DUE + 30 * MINUTE + 1, new_id(),
        )
        self.assertEqual(result, "busy")
        self.assertEqual(self.store.get(CHAT)["state"], "WAITING_CONFIRMATION")

    def test_delivery_finalization_is_once_only_and_rejects_a_stale_claim(self):
        timer_id = self.start()
        expiration_at = DUE + 30 * MINUTE
        claim = self.store.claim_due(
            CHAT, expiration_at, new_id(), new_id(), new_id(),
            expiration_at + 120_000,
        )
        self.assertIsNotNone(claim)
        self.assertTrue(
            self.store.finish_delivery(CHAT, claim, expiration_at, 5 * MINUTE)
        )
        self.assertFalse(
            self.store.finish_delivery(CHAT, claim, expiration_at, 5 * MINUTE)
        )
        callback_result = self.store.callback(
            CHAT, "more", timer_id, claim["confirmation_id"],
            expiration_at + 1, new_id(),
        )
        self.assertEqual(callback_result, "restarted")
        self.assertFalse(
            self.store.finish_delivery(
                CHAT, claim, expiration_at + 2, 5 * MINUTE
            )
        )
        timer = self.store.get(CHAT)
        self.assertEqual(timer["state"], "RUNNING")
        self.assertEqual(timer["next_reminder_at"], "")

    def test_redis_failure_from_worker_is_visible(self):
        broken = TimerStore("https://redis.invalid", "token")
        with mock.patch.object(broken, "command", side_effect=RedisError("offline")):
            with self.assertRaises(RedisError):
                run_tick(broken, self.bot, DUE)


if __name__ == "__main__":
    unittest.main()
