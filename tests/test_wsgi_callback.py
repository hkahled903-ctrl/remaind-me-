"""The button path, end to end through WSGI.

`tests/test_confirm.py` calls `app.callback(...)` directly, which proves the
handler is right but skips everything between Telegram and it. This file drives
the real `/telegram/webhook` endpoint over a real socket with `wsgiref`, exactly
as Vercel does, because that is where the bug that made every button inert
lived: the WSGI adapter mangled the header name, the handler was never reached,
and the whole confirmation feature was unreachable in production while its unit
tests stayed green.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from wsgiref.simple_server import make_server

from reminder.timer import Cycle, FileCycleStore, activate, mark_sent
from reminder.webapp import ConnectApp, SECRET_HEADER

UTC = timezone.utc
SECRET = "test-secret"


class _Binding:
    chat_id = "555"


class _BindingStore:
    def load(self):
        return _Binding()

    def save(self, binding):
        return None


class _Recorder:
    """Captures the answerCallbackQuery calls without a network."""

    def __init__(self):
        self.answers: list[tuple[str, str]] = []

    def __call__(self, callback_id: str, text: str, alert: bool = False) -> None:
        self.answers.append((callback_id, text))


class WebhookCallbackTest(unittest.TestCase):
    """One server for the class; the app is rebuilt per test with fresh state."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        cls.cycles = FileCycleStore(Path(cls.dir.name) / "timer.json")

        cls.answers = _Recorder()
        cls.saved: list[str] = []

        # Wire `on_saved` the way `api/index.py` does, so this exercises the
        # production effect of a button press rather than a stub that only
        # records that it happened.
        from reminder.commands import stop_timer

        def on_saved(chat_id: str) -> None:
            cls.saved.append(chat_id)
            stop_timer(cls.cycles, None, chat_id)

        cls.app = ConnectApp(
            store=_BindingStore(),
            secret=SECRET,
            bot_username="testbot",
            cycle_store=cls.cycles,
            answer=cls.answers,
            on_saved=on_saved,
            tz=UTC,
        )

        # Serve the real WSGI callable with the real adapter around it: swap the
        # module-level APP for one wired to a temp store, and keep every line of
        # `api/index.py` -- header normalisation, routing, error handling --
        # exactly as Vercel runs it.
        from api import index

        cls.index = index
        cls._real_app = index.APP
        index.APP = cls.app
        cls.httpd = make_server("127.0.0.1", 0, index.application)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)
        cls.httpd.server_close()
        cls.index.APP = cls._real_app
        cls.dir.cleanup()

    def setUp(self):
        self.answers.answers.clear()
        self.saved.clear()
        self.cycles._path.write_text("{}", encoding="utf-8")

    def open_question(self, chat_id: str = "555", at=datetime(2026, 10, 5, 7, 35, tzinfo=UTC)):
        cycle = mark_sent(activate(Cycle(chat_id=chat_id), 15, at - timedelta(minutes=15)), at)
        self.cycles.save(cycle)
        self.cycles.remember(chat_id)

    def post(self, payload: dict, secret: str | None = SECRET):
        url = f"http://127.0.0.1:{self.port}/telegram/webhook"
        headers = {"Content-Type": "application/json"}
        if secret is not None:
            headers[SECRET_HEADER] = secret
        request = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"), method="POST", headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # The 403 tests deliberately provoke an error response; close it so
            # the run does not leak a socket just because a case passed.
            with exc:
                return exc.code, json.loads(exc.read().decode("utf-8"))

    def tap(self, chat_id: str, data: str, query_id: str = "q1", secret: str | None = SECRET):
        return self.post(
            {
                "update_id": 1,
                "callback_query": {
                    "id": query_id,
                    "from": {"id": int(chat_id or 0)},
                    "data": data,
                    "message": {"chat": {"id": int(chat_id)}, "text": "Report ready."},
                },
            },
            secret=secret,
        )

    # -- the positive path -------------------------------------------------

    def test_a_correct_secret_reaches_the_callback_handler(self):
        self.open_question()
        status, body = self.tap("555", "saved")
        self.assertEqual(status, 200, "the WSGI adapter must deliver the header")
        self.assertTrue(body["ok"])
        self.assertTrue(body["resolved"], "'I saved it' must resolve the question")

    def test_saved_stops_that_chats_cycle(self):
        self.open_question()
        self.tap("555", "saved")
        self.assertEqual(self.saved, ["555"], "the schedule must be asked to stop")
        self.assertTrue(self.cycles.load("555").stopped)
        self.assertFalse(self.cycles.load("555").pending)

    def test_the_button_gets_a_telegram_acknowledgement(self):
        self.open_question()
        self.tap("555", "saved", query_id="q-abc")
        self.assertEqual(len(self.answers.answers), 1, "the spinner must be answered")
        query_id, text = self.answers.answers[0]
        self.assertEqual(query_id, "q-abc")
        self.assertIn("Saved", text)

    def test_not_yet_leaves_the_cycle_open_and_does_not_stop_it(self):
        self.open_question()
        status, body = self.tap("555", "not_yet")
        self.assertEqual(status, 200)
        self.assertFalse(body["resolved"])
        self.assertEqual(self.saved, [], "nothing was stopped")
        cycle = self.cycles.load("555")
        self.assertTrue(cycle.pending, "still waiting for an answer")
        self.assertFalse(cycle.stopped, "the interval must keep running")

    # -- the negative paths ------------------------------------------------

    def test_a_wrong_secret_is_rejected(self):
        self.open_question()
        status, _ = self.tap("555", "saved", secret="not-the-secret")
        self.assertEqual(status, 403)
        self.assertEqual(self.saved, [], "nothing may be stopped")
        self.assertFalse(self.cycles.load("555").stopped)

    def test_a_missing_secret_header_is_rejected(self):
        self.open_question()
        status, _ = self.tap("555", "saved", secret=None)
        self.assertEqual(status, 403)
        self.assertEqual(self.saved, [])

    def test_another_chat_cannot_stop_your_timer(self):
        """The security property: a stranger's tap finds nothing of yours to touch."""
        self.open_question("555")
        status, body = self.tap("999", "saved")
        self.assertEqual(status, 200)
        self.assertFalse(body["resolved"], "chat 999 has no question of its own")
        self.assertEqual(self.saved, [], "chat 555 must be untouched")
        self.assertFalse(self.cycles.load("555").stopped)

    def test_a_tap_from_a_chat_with_no_cycle_at_all_changes_nothing(self):
        status, body = self.tap("12345", "saved")
        self.assertEqual(status, 200)
        self.assertFalse(body["resolved"])
        self.assertEqual(self.saved, [])

    def test_a_start_message_still_binds_through_wsgi(self):
        """The same endpoint must keep doing what it did before callbacks."""
        status, body = self.post(
            {
                "update_id": 2,
                "message": {
                    "message_id": 1,
                    "chat": {"id": 777, "type": "private"},
                    "text": "/start",
                    "from": {"id": 777},
                },
            }
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["bound"])
        self.assertEqual(str(body["chat_id"]), "777")

    def test_callback_query_is_reachable_only_when_it_is_allowed(self):
        """Guards the registration that made this whole path dead.

        `allowed_updates: ["message"]` tells Telegram to discard every button
        press before it is ever sent, so a correct handler behind it receives
        nothing. This pins the value that registration must keep.
        """
        from reminder.webapp import register_webhook

        calls: list[dict] = []

        class FakeTelegram:
            def call(self, method, payload):
                calls.append({"method": method, "payload": payload})
                return {"ok": True}

        register_webhook("https://example.test", SECRET, FakeTelegram())
        payload = calls[0]["payload"]
        self.assertIn("callback_query", payload["allowed_updates"])
        self.assertIn("message", payload["allowed_updates"])

    def test_a_message_that_is_not_a_command_is_ignored(self):
        status, body = self.post(
            {
                "update_id": 3,
                "message": {"message_id": 2, "chat": {"id": 1}, "text": "hello"},
            }
        )
        self.assertEqual(status, 200)
        self.assertFalse(body.get("bound"))


if __name__ == "__main__":
    unittest.main()