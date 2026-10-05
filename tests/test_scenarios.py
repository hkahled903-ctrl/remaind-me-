#!/usr/bin/env python3
"""Scenario tests: situations the reminder can actually be in.

Telegram is faked with a real local HTTP server, so status codes, retry timing and
request bodies cross an actual socket rather than being mocked away. The CLI tests
spawn the real `python -m reminder` process and assert on exit codes.

Run: python -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from reminder.commands import deliver  # noqa: E402
from reminder.config import ConfigError, load_config, parse_hhmm  # noqa: E402
from reminder.policy import next_occurrence, should_send  # noqa: E402
from reminder.state import already_sent, mark_sent  # noqa: E402
from reminder.transport import Telegram  # noqa: E402
from reminder.webapp import register_webhook  # noqa: E402

CAIRO = timezone(timedelta(hours=2))  # Egypt abolished DST in 2023 -> fixed UTC+2


class FakeTelegram:
    """A real HTTP server speaking enough of the Bot API to drive the client."""

    def __init__(self):
        self.calls = []
        self.queue = []  # statuses returned one per call, before falling through
        self.default_status = 200
        self._server = HTTPServer(("127.0.0.1", 0), self._build_handler())
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def _build_handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 (name fixed by BaseHTTPRequestHandler)
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length).decode()
                payload = dict(urllib.parse.parse_qsl(raw)) if raw else {}
                fake.calls.append({"path": self.path, "payload": payload})
                status = fake.queue.pop(0) if fake.queue else fake.default_status
                body = json.dumps({"ok": status == 200, "description": "fake"}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass  # keep the test output clean

        return Handler

    @property
    def api_base(self) -> str:
        return f"http://127.0.0.1:{self.port}/bot{{token}}/{{method}}"

    @property
    def messages(self):
        return [c for c in self.calls if c["path"].endswith("sendMessage")]

    def client(self, token="TOKEN", sleep=None) -> Telegram:
        return Telegram(token, api_base=self.api_base, sleep=sleep or (lambda _: None))

    def close(self):
        self._server.shutdown()
        self._server.server_close()


class ScenarioTest(unittest.TestCase):
    """The reminder in real situations."""

    def setUp(self):
        self.fake = FakeTelegram()
        self.client = self.fake.client()
        self.tmp = TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state.json"
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.fake.close)
        self.config = {
            "telegram_chat_id": "12345",
            "reminder_time": "09:00",
            "timezone": "Africa/Cairo",
            "message": "Reminder: your daily report is due.",
        }

    # ------------------------------------------------- the normal day
    def test_registration_sends_callback_query_as_a_json_array(self):
        """Exercise the real registration call and its encoded Bot API request."""
        register_webhook("https://example.test", " test-secret \n", self.client)
        call = next(c for c in self.fake.calls if c["path"].endswith("setWebhook"))
        payload = call["payload"]
        self.assertEqual(
            json.loads(payload["allowed_updates"]),
            ["message", "callback_query"],
        )
        self.assertEqual(payload["url"], "https://example.test/telegram/webhook")
        self.assertEqual(payload["secret_token"], "test-secret")

    def test_s1_cron_fires_inside_window_user_gets_one_message(self):
        """Real morning: cron runs at 09:00 Cairo, exactly one message lands."""
        self.assertTrue(
            should_send(datetime(2026, 10, 4, 9, 0, tzinfo=CAIRO), "09:00")
        )
        deliver(self.config, CAIRO, self.client, self.state)
        self.assertEqual(len(self.fake.messages), 1)
        self.assertEqual(self.fake.messages[0]["payload"]["chat_id"], "12345")
        self.assertEqual(
            self.fake.messages[0]["payload"]["text"], self.config["message"]
        )

    def test_s2_state_stops_a_second_send_on_a_persistent_host(self):
        """Re-running on a machine that kept its state must not double-send."""
        deliver(self.config, CAIRO, self.client, self.state)
        self.assertFalse(deliver(self.config, CAIRO, self.client, self.state))
        self.assertEqual(len(self.fake.messages), 1)

    def test_s3_ci_reruns_have_no_memory_and_actually_double_send(self):
        """GAP (roadmap P2): GitHub runners are ephemeral, so state is gone.

        Re-running a failed job -- or pressing Run workflow by hand -- sends a
        second identical reminder. Reproduced with a fresh state path, which is
        exactly what a new runner has. Flipped by P2; do not rewrite.
        """
        deliver(self.config, CAIRO, self.client, self.state)
        deliver(self.config, CAIRO, self.client, Path(self.tmp.name) / "fresh.json")
        self.assertEqual(len(self.fake.messages), 2)

    # ------------------------------------------------- clock changes
    def test_s4_a_one_hour_late_utc_shift_still_sends(self):
        """Egypt reinstating DST makes Cairo UTC+3, so `0 7 UTC` lands at 10:00."""
        self.assertTrue(
            should_send(datetime(2026, 10, 4, 10, 0, tzinfo=CAIRO), "09:00")
        )

    def test_s4b_a_one_hour_early_utc_shift_silently_drops_the_reminder(self):
        """GAP (roadmap P1): the other direction loses the day, with no error.

        If Cairo ever runs at UTC+1 the same cron fires at 08:00 local, before the
        window opens, and nothing is sent. A late shift is noisy; an early shift
        is silent. Flipped by P1; do not rewrite.
        """
        self.assertFalse(
            should_send(datetime(2026, 10, 4, 8, 0, tzinfo=CAIRO), "09:00")
        )

    def test_s5_window_closes_exactly_two_hours_late(self):
        """Measured boundary: open at +2h, shut at +2h1m."""
        base = datetime(2026, 10, 4, 9, 0, tzinfo=CAIRO)
        self.assertTrue(should_send(base + timedelta(hours=2), "09:00"))
        self.assertFalse(should_send(base + timedelta(hours=2, minutes=1), "09:00"))

    def test_s6_cron_firing_early_does_not_send_yesterdays_reminder(self):
        """A cron mis-set to 04:00 UTC must not fire a 09:00 reminder at 06:00."""
        self.assertFalse(
            should_send(datetime(2026, 10, 4, 6, 0, tzinfo=CAIRO), "09:00")
        )

    def test_s17_window_does_not_wrap_across_midnight(self):
        """A 23:30 reminder must not treat 00:10 the next day as in-window."""
        self.assertTrue(
            should_send(datetime(2026, 10, 4, 23, 45, tzinfo=CAIRO), "23:30")
        )
        self.assertFalse(
            should_send(datetime(2026, 10, 5, 0, 10, tzinfo=CAIRO), "23:30")
        )

    def test_s16_next_occurrence_rolls_over_to_tomorrow(self):
        """At 09:30 the loop must plan tomorrow, not re-fire in 30 seconds."""
        self.assertEqual(
            next_occurrence(datetime(2026, 10, 4, 9, 30, tzinfo=CAIRO), "09:00"),
            datetime(2026, 10, 5, 9, 0, tzinfo=CAIRO),
        )

    def test_s18_next_occurrence_crosses_a_month_boundary(self):
        """On the last evening of the month the next send is in the new month."""
        self.assertEqual(
            next_occurrence(datetime(2026, 10, 31, 22, 0, tzinfo=CAIRO), "09:00"),
            datetime(2026, 11, 1, 9, 0, tzinfo=CAIRO),
        )

    def test_s19_exactly_on_the_minute_the_loop_does_not_refire(self):
        """At exactly reminder_time, next_occurrence must be tomorrow."""
        now = datetime(2026, 10, 4, 9, 0, tzinfo=CAIRO)
        self.assertEqual(
            next_occurrence(now, "09:00"), datetime(2026, 10, 5, 9, 0, tzinfo=CAIRO)
        )

    # ------------------------------------------------- Telegram failures
    def test_s7_rate_limited_then_recovered(self):
        """Telegram answers 429 twice, then accepts. The reminder must still land."""
        self.fake.queue = [429, 429]
        slept = []
        self.fake.client(sleep=slept.append).send_message("12345", "hello")
        self.assertEqual(len(self.fake.messages), 3)
        self.assertEqual(slept, [1, 2], "no sleep after the successful final attempt")

    def test_s8_telegram_outage_fails_after_three_attempts(self):
        """Telegram is down all day. Fail loudly instead of pretending it worked."""
        self.fake.default_status = 500
        slept = []
        with self.assertRaises(RuntimeError) as caught:
            self.fake.client(sleep=slept.append).send_message("12345", "hello")
        self.assertEqual(len(self.fake.calls), 3)
        self.assertEqual(slept, [1, 2], "must not sleep after the final attempt")
        self.assertIn("3 attempts", str(caught.exception))

    def test_s9_revoked_token_fails_fast_without_pointless_retries(self):
        """Token revoked in BotFather. Retrying cannot fix it, so stop at once."""
        self.fake.default_status = 401
        with self.assertRaises(RuntimeError) as caught:
            self.fake.client().send_message("12345", "hello")
        self.assertEqual(len(self.fake.calls), 1, "must not retry a 401")
        self.assertIn("401", str(caught.exception))

    def test_s10_wrong_chat_id_fails_fast(self):
        """Typo in telegram_chat_id -> 400, and one wasted request, not three."""
        self.fake.default_status = 400
        with self.assertRaises(RuntimeError):
            self.fake.client().send_message("not-a-chat", "hello")
        self.assertEqual(len(self.fake.calls), 1)

    def test_s20_connection_refused_is_retried_then_reported(self):
        """Nothing listening at all: the client must retry and then say so."""
        slept = []
        dead = Telegram("TOKEN", api_base="http://127.0.0.1:1/bot{token}/{method}",
                        sleep=slept.append)
        with self.assertRaises(RuntimeError) as caught:
            dead.send_message("12345", "hello")
        self.assertEqual(slept, [1, 2])
        self.assertIn("3 attempts", str(caught.exception))

    # ------------------------------------------------- configuration
    def test_s11_missing_chat_id_is_a_configuration_error(self):
        self.config["telegram_chat_id"] = ""
        with self.assertRaises(ConfigError):
            deliver(self.config, CAIRO, self.client, self.state)
        self.assertEqual(self.fake.calls, [], "must not hit the network at all")

    def test_s12_whitespace_chat_id_is_rejected_too(self):
        self.config["telegram_chat_id"] = "   "
        with self.assertRaises(ConfigError):
            deliver(self.config, CAIRO, self.client, self.state)

    def test_s13_corrupt_state_file_never_blocks_a_reminder(self):
        """A half-written state.json must not silently swallow today's reminder."""
        self.state.write_text("{not json", encoding="utf-8")
        self.assertFalse(already_sent("2026-10-04", self.state))
        deliver(self.config, CAIRO, self.client, self.state)
        self.assertEqual(len(self.fake.messages), 1)

    def test_s21_shipped_config_is_valid_and_has_an_empty_chat_id(self):
        """The config.json that ships in the repo must parse and be safe to hand out."""
        shipped = load_config(PROJECT_ROOT / "config.json")
        self.assertEqual(shipped["reminder_time"], "09:00")
        self.assertEqual(shipped["timezone"], "Africa/Cairo")
        self.assertEqual(shipped["telegram_chat_id"], "", "no real chat id in git")
        parse_hhmm(shipped["reminder_time"])
        self.assertTrue(shipped["message"].strip())

    def test_s22_missing_config_keys_fall_back_to_defaults(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            path.write_text(json.dumps({"reminder_time": "07:15"}), encoding="utf-8")
            config = load_config(path)
        self.assertEqual(config["reminder_time"], "07:15")
        self.assertEqual(config["timezone"], "Africa/Cairo")
        self.assertEqual(config["telegram_chat_id"], "")

    def test_s23_corrupt_config_raises_instead_of_silently_using_defaults(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            path.write_text("{ this is not json", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_s24_config_that_is_a_list_is_rejected(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            path.write_text('["09:00"]', encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_s14_malformed_reminder_time_is_rejected(self):
        for bad in ("25:99", "9am", "", "09:60", "09", "09:00:00"):
            with self.subTest(value=bad), self.assertRaises(ConfigError):
                parse_hhmm(bad)


class CommandLineTest(unittest.TestCase):
    """The real process, spawned for real, asserting real exit codes."""

    def run_cli(self, *args, env_overrides=None, config_text=None):
        """Spawn the real CLI in a throwaway copy of the project.

        Copying the tree (rather than running in the repo) means the test sees
        exactly what a fresh clone would, including any config.json damage.
        """
        with TemporaryDirectory() as d:
            workdir = Path(d)
            shutil.copytree(
                PROJECT_ROOT / "reminder",
                workdir / "reminder",
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            if config_text is not None:
                (workdir / "config.json").write_text(config_text, encoding="utf-8")
            else:
                shutil.copy(PROJECT_ROOT / "config.json", workdir / "config.json")
            env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
            env.pop("TELEGRAM_BOT_TOKEN", None)
            env.update(env_overrides or {})
            return subprocess.run(
                [sys.executable, "-m", "reminder", *args],
                cwd=workdir,
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )

    def test_s22a_help_works_without_a_token(self):
        """Regression guard: --help must never demand credentials."""
        result = self.run_cli("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage:", result.stdout)

    def test_s22b_dry_run_works_without_a_token(self):
        result = self.run_cli("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("message:", result.stdout)

    def test_s22c_scheduled_without_a_token_fails_clearly(self):
        """--scheduled needs the token even when the window is shut.

        A missing secret is a misconfiguration worth reporting every run, not
        only when a send was about to happen.
        """
        result = self.run_cli("--scheduled")
        self.assertEqual(result.returncode, 1)
        self.assertIn("TELEGRAM_BOT_TOKEN", result.stdout)

    def test_s23a_once_without_a_token_fails_with_a_clear_message(self):
        result = self.run_cli("--once")
        self.assertEqual(result.returncode, 1)
        self.assertIn("TELEGRAM_BOT_TOKEN", result.stdout)

    def test_s23b_whoami_without_a_token_fails_cleanly(self):
        result = self.run_cli("--whoami")
        self.assertEqual(result.returncode, 1)
        self.assertIn("TELEGRAM_BOT_TOKEN", result.stdout)

    def test_s24a_corrupt_config_in_the_repo_exits_one(self):
        result = self.run_cli("--dry-run", config_text="{ broken")
        self.assertEqual(result.returncode, 1)
        self.assertIn("configuration error", result.stdout)

    def test_s24b_unknown_flag_is_rejected_instead_of_starting_the_loop(self):
        """A typo must not fall through to the mode that never exits."""
        result = self.run_cli("--not-a-flag")
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage:", result.stdout)
        self.assertIn("--not-a-flag", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
