#!/usr/bin/env python3
"""Connect-flow tests: can a user go from "nothing set up" to "connected"?

The claim this file defends is the one the Connect button makes: press it, press
Start in Telegram, and the reminder now knows where to send. Every test drives the
real handlers over real sockets -- a real browser would take exactly this path --
so a route that is wired but unreachable, or reachable but wrong, fails here.

Telegram is faked at the HTTP boundary (as in test_scenarios.py) and the binding
store is faked at the filesystem boundary, because neither is what this feature is
about. The Upstash backend gets its own fake REST server, so its wire format is
checked rather than assumed.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from reminder.binding import Binding, FileStore, UpstashStore, store_from_env  # noqa: E402
from reminder.commands import deliver, resolve_chat_id  # noqa: E402
from reminder.config import ConfigError  # noqa: E402
from reminder.settings import FileSettings, Settings  # noqa: E402
from reminder.webapp import PAGE, SECRET_HEADER, ConnectApp, _make_handler  # noqa: E402

SECRET = "test-webhook-secret-token"
BOT = "my_report_bot"


def get(url: str) -> tuple[int, dict]:
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.status, json.loads(response.read().decode())


def get_text(url: str) -> tuple[int, str]:
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.status, response.read().decode()


def post(url: str, body: bytes, secret: str | None = None) -> tuple[int, dict]:
    request = urllib.request.Request(url, data=body, method="POST")
    if secret is not None:
        request.add_header(SECRET_HEADER, secret)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode())
        finally:
            exc.close()  # otherwise the socket waits on the garbage collector


class ConnectTestCase(unittest.TestCase):
    """A live server per test, so no state leaks between them."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = FileStore(Path(self.tmp.name) / "binding.json")
        self.sent: list[str] = []
        self.app = ConnectApp(
            self.store, secret=SECRET, bot_username=BOT, send=self.sent.append
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self.app))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def start_update(self, chat_id: int = 987654321, username: str = "hassan") -> bytes:
        """A real Telegram `/start` update, shaped as Telegram actually sends it.

        Mints a nonce first if the test has not already opened the page, so a
        caller that only cares about rejection does not have to think about it.
        """
        if not hasattr(self, "nonce"):
            _, data = get(f"{self.base}/connect/start")
            self.nonce = data["nonce"]
        return json.dumps(
            {
                "update_id": 1,
                "message": {
                    "message_id": 1,
                    "date": 1700000000,
                    "chat": {"id": chat_id, "type": "private", "username": username},
                    "text": f"/start {self.nonce}",
                },
            }
        ).encode("utf-8")


class ConnectPageTest(ConnectTestCase):
    def test_c1_the_page_offers_a_connect_button(self):
        status, body = get_text(f"{self.base}/")
        self.assertEqual(status, 200)
        self.assertIn("Connect Telegram", body)
        self.assertIn("/connect/start", body)

    def test_c2_start_hands_out_a_deep_link_carrying_a_fresh_nonce(self):
        status, first = get(f"{self.base}/connect/start")
        self.assertEqual(status, 200)
        self.assertTrue(first["deep_link"].startswith(f"https://t.me/{BOT}?start="))

        _, second = get(f"{self.base}/connect/start")
        self.assertNotEqual(
            first["nonce"],
            second["nonce"],
            "two page loads must not share a nonce, or one Start binds every open page",
        )

    def test_c3_status_is_false_until_start_is_pressed(self):
        _, data = get(f"{self.base}/connect/start")
        status, result = get(
            f"{self.base}/connect/status?nonce={data['nonce']}&issued={data['issued']}"
        )
        self.assertEqual(status, 200)
        self.assertFalse(result["connected"], "nothing has been bound yet")

    def test_c4_status_flips_to_true_after_start(self):
        """The whole feature, end to end, over two real requests."""
        _, data = get(f"{self.base}/connect/start")
        self.nonce = data["nonce"]
        status, result = post(
            f"{self.base}/telegram/webhook", self.start_update(), secret=SECRET
        )
        self.assertEqual(status, 200)
        self.assertTrue(result["bound"])

        _, result = get(f"{self.base}/connect/status?nonce={data['nonce']}")
        self.assertTrue(result["connected"])
        self.assertEqual(result["chat_id"], "987654321")
        self.assertEqual(result["label"], "hassan")

    def test_c5_a_binding_from_an_earlier_session_does_not_look_connected(self):
        """Otherwise a returning user sees 'Connected' the instant the page loads."""
        self.store.save(Binding(chat_id="111", username="old", bound_at="2000-01-01T00:00:00+00:00"))
        _, data = get(f"{self.base}/connect/start")
        _, result = get(f"{self.base}/connect/status?nonce={data['nonce']}")
        self.assertFalse(result["connected"])

        self.store.save(Binding(chat_id="222", username="new", bound_at="2999-01-01T00:00:00+00:00"))
        _, result = get(f"{self.base}/connect/status?nonce={data['nonce']}")
        self.assertTrue(result["connected"])


class WebhookSecurityTest(ConnectTestCase):
    def test_c6_a_webhook_with_the_wrong_secret_is_refused(self):
        _, data = get(f"{self.base}/connect/start")
        status, _ = post(
            f"{self.base}/telegram/webhook",
            self.start_update(),
            secret="not-the-secret",
        )
        self.assertEqual(status, 403)
        self.assertIsNone(self.store.load(), "a rejected update must bind nothing")

    def test_c7_a_webhook_with_no_secret_is_refused(self):
        status, _ = post(f"{self.base}/telegram/webhook", self.start_update())
        self.assertEqual(status, 403)
        self.assertIsNone(self.store.load())

    def test_c8_an_app_without_a_configured_secret_refuses_to_bind_at_all(self):
        """Defence in depth: even the correct secret must fail when none is set."""
        app = ConnectApp(self.store, secret="", bot_username=BOT)
        status, _ = app.webhook(SECRET, b'{"message":{"chat":{"id":1},"text":"/start x"}}')
        self.assertEqual(status, 503)
        self.assertIsNone(self.store.load())

    def test_c9_a_malformed_body_is_a_400_not_a_crash(self):
        status, _ = post(f"{self.base}/telegram/webhook", b"not json", secret=SECRET)
        self.assertEqual(status, 400)

    def test_c34_a_bare_start_from_the_telegram_app_binds_too(self):
        """Tapping Start in Telegram sends `/start` with no payload.

        Only accepting `/start <nonce>` made the button look broken to anyone who
        started in Telegram rather than on the Connect page.
        """
        body = json.dumps(
            {
                "update_id": 7,
                "message": {
                    "chat": {"id": 4242, "type": "private", "username": "hassan"},
                    "text": "/start",
                },
            }
        ).encode("utf-8")
        status, result = post(f"{self.base}/telegram/webhook", body, secret=SECRET)
        self.assertEqual(status, 200)
        self.assertTrue(result["bound"])
        self.assertEqual(self.store.load().chat_id, "4242")

    def test_c35_binding_replies_so_the_tap_is_not_silent(self):
        """A swallowed Start tap looks exactly like a broken bot."""
        sent: list[str] = []
        app = ConnectApp(self.store, secret=SECRET, bot_username=BOT, ack=sent.append)
        body = json.dumps(
            {"message": {"chat": {"id": 4242}, "text": "/start"}}
        ).encode("utf-8")
        status, _ = app.webhook(SECRET, body)
        self.assertEqual(status, 200)
        self.assertEqual(sent, ["4242"], "the new chat must be acknowledged")

    def test_c36_a_failed_reply_still_keeps_the_binding(self):
        """A network blip must not cost the user the connection they just made."""
        def explode(_chat_id: str) -> None:
            raise RuntimeError("network down")

        app = ConnectApp(self.store, secret=SECRET, bot_username=BOT, ack=explode)
        body = json.dumps(
            {"message": {"chat": {"id": 77}, "text": "/start"}}
        ).encode("utf-8")
        status, result = app.webhook(SECRET, body)
        self.assertEqual(status, 200)
        self.assertTrue(result["bound"])
        self.assertEqual(self.store.load().chat_id, "77")

    def test_c39_a_reload_keeps_the_connected_state(self):
        """Reloading after connecting is the most natural thing to do.

        The page only asked for the status after the button was pressed, so any
        fresh load showed "Not connected yet" while the binding sat in the store.
        """
        status, body = get_text(f"{self.base}/")
        self.assertEqual(status, 200)
        self.assertIn("restoreConnection", body)

    def test_c38_the_telegram_link_is_a_real_clickable_anchor(self):
        """A popup opened after an `await` is blocked by the browser.

        The page used to call `window.open(deep_link)` once the nonce came back
        from the network. That is no longer inside the click gesture, so Telegram
        never opened and the page waited forever for a Start that could not
        happen. The link must be an anchor with a real href.
        """
        status, body = get_text(f"{self.base}/")
        self.assertEqual(status, 200)
        self.assertIn('id="telegram-link"', body)
        self.assertNotIn("window.open", body)

    def test_c37_start_followed_by_other_words_is_not_a_start(self):
        """/startled is not /start."""
        body = json.dumps(
            {"message": {"chat": {"id": 5}, "text": "/startled"}}
        ).encode("utf-8")
        status, result = post(f"{self.base}/telegram/webhook", body, secret=SECRET)
        self.assertEqual(status, 200)
        self.assertFalse(result["bound"])
        self.assertIsNone(self.store.load())

    def test_c10_a_message_that_is_not_start_binds_nothing(self):
        """Someone chatting with the bot must not repoint the reminder."""
        body = json.dumps(
            {"message": {"chat": {"id": 5}, "text": "hello there"}}
        ).encode("utf-8")
        status, result = post(f"{self.base}/telegram/webhook", body, secret=SECRET)
        self.assertEqual(status, 200)
        self.assertFalse(result["bound"])
        self.assertIsNone(self.store.load())


class ConnectedReminderTest(ConnectTestCase):
    """Once connected, the reminder goes to the connected chat with no config."""

    def setUp(self):
        super().setUp()
        self.config = {"telegram_chat_id": "", "message": "Reminder: your daily report is due."}

    def test_c11_the_configured_id_wins_over_the_binding(self):
        """A hand-configured id must keep working; the binding is a fallback."""
        self.store.save(Binding(chat_id="999", username="connected"))
        self.assertEqual(
            resolve_chat_id({"telegram_chat_id": "123"}, self.store), "123"
        )

    def test_c12_the_binding_is_used_when_config_is_empty(self):
        self.store.save(Binding(chat_id="999", username="connected"))
        self.assertEqual(resolve_chat_id(self.config, self.store), "999")

    def test_c13_neither_configured_nor_bound_is_still_a_config_error(self):
        with self.assertRaises(ConfigError):
            resolve_chat_id(self.config, self.store)

    def test_c14_an_unreachable_store_does_not_send_to_nobody_silently(self):
        class Broken:
            def load(self):
                raise RuntimeError("store unreachable")

        with self.assertRaises(ConfigError):
            resolve_chat_id(self.config, Broken())

    def test_c15_reset_unbinds(self):
        self.store.save(Binding(chat_id="999"))
        status, _ = post(f"{self.base}/connect/reset", b"")
        self.assertEqual(status, 200)
        self.assertIsNone(self.store.load())

    def test_c16_the_test_send_button_uses_the_connected_chat(self):
        self.store.save(Binding(chat_id="555"))
        status, result = post(f"{self.base}/connect/test", b"")
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        self.assertEqual(self.sent, ["555"])

    def test_c17_the_test_send_button_refuses_when_not_connected(self):
        status, result = post(f"{self.base}/connect/test", b"")
        self.assertEqual(status, 409)
        self.assertFalse(self.sent)


class IntervalControlTest(unittest.TestCase):
    """The segmented control's two jobs: read the current setting, change it safely."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = FileSettings(Path(self.tmp.name) / "settings.json")
        self.tz = timezone(timedelta(hours=2))
        self.app = ConnectApp(
            FileStore(Path(self.tmp.name) / "binding.json"),
            secret=SECRET,
            bot_username=BOT,
            settings_store=self.settings,
            reminder_time="09:00",
            tz=self.tz,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_c26_the_page_uses_a_countdown_not_a_clock_face(self):
        body = PAGE
        self.assertIn('id="countdown"', body)
        self.assertNotIn("<svg", body, "the dial was replaced by a number")

    def test_c27_the_ticking_digits_are_hidden_from_assistive_tech(self):
        """A per-second live region would announce every second."""
        self.assertIn('id="countdown" aria-hidden="true"', PAGE)
        self.assertIn('id="countdown-sentence" aria-live="polite"', PAGE)

    def test_c28_the_interval_control_is_real_form_controls(self):
        self.assertIn('<legend class="sr-only">', PAGE)
        self.assertIn('input.type = "radio"', PAGE)
        self.assertIn('input.name = "interval"', PAGE)

    def test_c29_reading_the_schedule_offers_exactly_the_supported_intervals(self):
        status, data = self.app.schedule()
        self.assertEqual(status, 200)
        self.assertEqual(
            [stop["value"] for stop in data["options"]],
            [0, 15, 30, 60, 180],
            "options must be an ordered list; a JSON object is not iterable in JS",
        )
        self.assertTrue(all(stop["label"] for stop in data["options"]))

    def test_c30_the_countdown_is_a_plausible_number_of_seconds(self):
        _, data = self.app.schedule()
        self.assertGreater(data["seconds_until"], 0)
        self.assertLessEqual(data["seconds_until"], 24 * 3600)

    def test_c31_changing_the_interval_stores_it_and_reports_the_new_countdown(self):
        status, data = self.app.set_interval(b'{"minutes": 30}')
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["interval_minutes"], 30)
        self.assertEqual(self.settings.load().interval_minutes, 30)
        self.assertLessEqual(data["seconds_until"], 30 * 60)

    def test_c32_an_unlisted_interval_is_refused_and_changes_nothing(self):
        self.app.set_interval(b'{"minutes": 30}')
        status, data = self.app.set_interval(b'{"minutes": 45}')
        self.assertEqual(status, 400)
        self.assertIn("Pick one of the listed intervals", data["error"])
        self.assertEqual(self.settings.load().interval_minutes, 30)

    def test_c33_a_malformed_body_is_a_400_with_a_next_step(self):
        status, data = self.app.set_interval(b"nonsense")
        self.assertEqual(status, 400)
        self.assertIn("minutes", data["error"])

    def test_c34_changing_the_interval_clears_a_stale_served_marker(self):
        self.settings.save(Settings(interval_minutes=30, last_slot="999"))
        self.app.set_interval(b'{"minutes": 60}')
        self.assertEqual(self.settings.load().last_slot, "")

    def test_c35_daily_mode_is_reachable_again_from_the_control(self):
        self.app.set_interval(b'{"minutes": 60}')
        status, data = self.app.set_interval(b'{"minutes": 0}')
        self.assertEqual(status, 200)
        self.assertEqual(data["interval_minutes"], 0)
        self.assertEqual(self.settings.load().interval_minutes, 0)

    def test_c36_without_a_settings_store_the_control_says_so_instead_of_failing(self):
        app = ConnectApp(FileStore(Path(self.tmp.name) / "b.json"), secret=SECRET, bot_username=BOT)
        status, data = app.set_interval(b'{"minutes": 30}')
        self.assertEqual(status, 503)
        self.assertIn("not available", data["error"])


class UpstashBackendTest(unittest.TestCase):
    """The hosted backend, checked against a server that speaks its wire format."""

    def setUp(self):
        outer = self

        class FakeUpstash(BaseHTTPRequestHandler):
            data: dict[str, str] = {}
            seen_auth: list[str | None] = []
            failing = False

            def log_message(self, *args):
                pass

            def do_POST(self):
                FakeUpstash.seen_auth.append(self.headers.get("Authorization"))
                length = int(self.headers.get("Content-Length") or 0)
                command = json.loads(self.rfile.read(length).decode())
                name = command[0].upper()
                if FakeUpstash.failing:
                    self.send_response(500)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if name == "SET":
                    FakeUpstash.data[command[1]] = command[2]
                    result = "OK"
                elif name == "GET":
                    result = FakeUpstash.data.get(command[1])
                elif name == "DEL":
                    result = FakeUpstash.data.pop(command[1], None)
                else:
                    result = None
                body = json.dumps({"result": result}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        FakeUpstash.seen_auth = []
        FakeUpstash.failing = False
        self.handler = FakeUpstash
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstash)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.store = UpstashStore(self.url, "tok-abc", key="reminder:binding")
        outer.data = FakeUpstash.data

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def test_c18_a_binding_survives_a_round_trip(self):
        self.assertIsNone(self.store.load(), "empty at the start")
        self.store.save(Binding(chat_id="4242", username="hassan", bound_at="2026-01-01T00:00:00+00:00"))
        loaded = self.store.load()
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.chat_id, "4242")
        self.assertEqual(loaded.username, "hassan")
        self.assertEqual(loaded.label, "hassan")

    def test_c19_clear_removes_it(self):
        self.store.save(Binding(chat_id="4242"))
        self.store.clear()
        self.assertIsNone(self.store.load())

    def test_c20_the_token_travels_as_a_bearer_header(self):
        self.store.save(Binding(chat_id="1"))
        self.assertTrue(self.handler.seen_auth)
        self.assertTrue(all(a == "Bearer tok-abc" for a in self.handler.seen_auth))

    def test_c21_an_unreachable_store_raises_rather_than_reading_as_unbound(self):
        """The distinction that matters: 'cannot tell' is not 'not connected'."""
        self.handler.failing = True
        with self.assertRaises(RuntimeError):
            self.store.load()

    def test_c22_the_env_picker_chooses_upstash_only_when_both_values_are_set(self):
        both = {
            "UPSTASH_REDIS_REST_URL": self.url,
            "UPSTASH_REDIS_REST_TOKEN": "tok",
        }
        self.assertIsInstance(store_from_env(both), UpstashStore)

        half = {"UPSTASH_REDIS_REST_URL": self.url}
        self.assertIsInstance(store_from_env(half), FileStore)
        self.assertIsInstance(store_from_env({}), FileStore)


class CliSurfaceTest(unittest.TestCase):
    def test_c23_help_advertises_the_new_modes(self):
        import subprocess

        result = subprocess.run(
            [sys.executable, "-m", "reminder", "--help"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("--connect", result.stdout)
        self.assertIn("--serve", result.stdout)

    def test_c24_a_typo_is_still_rejected_rather_than_starting_something(self):
        import subprocess

        result = subprocess.run(
            [sys.executable, "-m", "reminder", "--conect"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 2, "a misspelled flag must not silently do something")


if __name__ == "__main__":
    unittest.main(verbosity=2)