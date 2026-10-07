from __future__ import annotations

import io
import json
import os
import re
import unittest
from unittest import mock

from api import index
from reminder.timer import now_ms

WEBSITE_SESSION = "test-session-" + ("x" * 30)
LINKED_SESSION = {"state": "LINKED", "chat_id": "private-chat-from-redis"}


class FakeWebsiteStore:
    def __init__(self):
        self.sessions = {}
        self.timers = {}
        self.created_links = []
        self.link_tokens = {}
        self.started = []

    def create_website_link(self, session_id, link_token):
        self.created_links.append((session_id, link_token))
        self.sessions[session_id] = {"state": "PENDING"}
        self.link_tokens[link_token] = session_id

    def complete_website_link(self, link_token, chat_id):
        session_id = self.link_tokens.pop(link_token, None)
        if session_id is None:
            return "invalid"
        self.sessions[session_id] = {"state": "LINKED", "chat_id": chat_id}
        return "linked"

    def get_website_session(self, session_id):
        return self.sessions.get(session_id)

    def get(self, chat_id):
        return self.timers.get(chat_id)

    def start(self, chat_id, duration_seconds, started_at, timer_id, update_id):
        self.started.append((chat_id, duration_seconds))
        self.timers[chat_id] = {
            "state": "RUNNING",
            "duration_seconds": str(duration_seconds),
            "started_at": str(started_at),
            "expires_at": str(started_at + duration_seconds * 1000),
            "next_reminder_at": "",
            "timer_id": timer_id,
        }
        return 1


class TelegramRecorder:
    def __init__(self, *_):
        self.sent = []

    def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text, reply_markup))
        return {"ok": True}


class WebsiteApiTest(unittest.TestCase):
    def setUp(self):
        self.store = FakeWebsiteStore()
        self.environ_patcher = mock.patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_USERNAME": "daymark_test_bot",
                "TELEGRAM_BOT_TOKEN": "test-bot-token",
                "WEBHOOK_SECRET": "test-webhook-secret",
                "UPSTASH_REDIS_REST_URL": "https://redis.invalid",
                "UPSTASH_REDIS_REST_TOKEN": "test-redis-token",
            },
        )
        self.environ_patcher.start()
        self.addCleanup(self.environ_patcher.stop)

    def request(self, path, method="GET", body=b"", session=None, headers=None):
        status_line = []
        response_headers = []
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "CONTENT_LENGTH": str(len(body)),
            "wsgi.input": io.BytesIO(body),
        }
        if session:
            environ[index.WEBSITE_SESSION_HEADER] = session
        environ.update(headers or {})

        def start_response(status, headers):
            status_line.append(status)
            response_headers.extend(headers)

        with (
            mock.patch.object(index, "_store", return_value=self.store),
            mock.patch.object(index, "_dependencies", return_value=(self.store, TelegramRecorder())),
            mock.patch.object(index, "Telegram", TelegramRecorder),
        ):
            payload = b"".join(index.application(environ, start_response))
        content_type = dict(response_headers).get("Content-Type", "")
        if content_type.startswith("application/json"):
            payload = json.loads(payload)
        return int(status_line[0].split()[0]), payload, dict(response_headers)

    def test_website_is_served_by_existing_wsgi_entrypoint(self):
        status, html, headers = self.request("/", method="GET")
        self.assertEqual(status, 200)
        self.assertIn(b"Connect Telegram", html)
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_link_creation_returns_deep_link_and_opaque_session(self):
        status, body, _ = self.request("/api/telegram/link", method="POST", body=b"{}")
        self.assertEqual(status, 201)
        self.assertRegex(body["session_id"], r"^[A-Za-z0-9_-]{43}$")
        self.assertTrue(body["telegram_url"].startswith("https://t.me/daymark_test_bot?start="))
        link_token = body["telegram_url"].rsplit("=", 1)[1]
        self.assertRegex(link_token, r"^[A-Za-z0-9_-]{43}$")
        self.assertEqual(self.store.created_links, [(body["session_id"], link_token)])
        self.assertNotIn("chat_id", body)

    def test_link_status_reports_pending_then_connected(self):
        self.store.sessions[WEBSITE_SESSION] = {"state": "PENDING"}
        status, body, _ = self.request(
            "/api/telegram/link-status", session=WEBSITE_SESSION
        )
        self.assertEqual((status, body), (200, {"connected": False}))
        self.store.sessions[WEBSITE_SESSION] = LINKED_SESSION
        status, body, _ = self.request(
            "/api/telegram/link-status", session=WEBSITE_SESSION
        )
        self.assertEqual((status, body), (200, {"connected": True}))
        self.assertNotIn("chat_id", body)

    def test_telegram_webhook_consumes_link_command_using_webhook_secret(self):
        self.store.sessions[WEBSITE_SESSION] = {"state": "PENDING"}
        self.store.link_tokens["one-time-link-token"] = WEBSITE_SESSION
        update = {
            "message": {
                "chat": {"id": 54321, "type": "private"},
                "text": "/start one-time-link-token",
            }
        }
        status, body, _ = self.request(
            "/telegram/webhook",
            "POST",
            json.dumps(update).encode(),
            headers={"HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN": "test-webhook-secret"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(self.store.sessions[WEBSITE_SESSION]["chat_id"], "54321")

    def test_timer_cannot_start_before_link_and_client_chat_id_is_rejected(self):
        body = json.dumps({"duration_minutes": 15}).encode()
        status, _, _ = self.request(
            "/api/timer/start", "POST", body, session=WEBSITE_SESSION
        )
        self.assertEqual(status, 409)
        self.assertEqual(self.store.started, [])

        self.store.sessions[WEBSITE_SESSION] = LINKED_SESSION
        body = json.dumps({"duration_minutes": 15, "chat_id": "attacker"}).encode()
        status, _, _ = self.request(
            "/api/timer/start", "POST", body, session=WEBSITE_SESSION
        )
        self.assertEqual(status, 400)
        self.assertEqual(self.store.started, [])

    def test_timer_starts_for_linked_chat_and_refresh_reads_persisted_expiry(self):
        self.store.sessions[WEBSITE_SESSION] = LINKED_SESSION
        status, timer, _ = self.request(
            "/api/timer/start",
            "POST",
            b'{"duration_minutes":15}',
            session=WEBSITE_SESSION,
        )
        self.assertEqual(status, 200)
        self.assertEqual(timer["state"], "RUNNING")
        self.assertEqual(timer["duration_seconds"], 900)
        self.assertEqual(self.store.started, [("private-chat-from-redis", 900)])
        self.assertGreater(timer["server_now_ms"], 0)
        self.assertGreater(timer["expires_at"], now_ms())

        status, refreshed, _ = self.request(
            "/api/timer/status", session=WEBSITE_SESSION
        )
        self.assertEqual(status, 200)
        self.assertEqual(refreshed["expires_at"], timer["expires_at"])
        self.assertEqual(refreshed["state"], "RUNNING")
        self.assertNotIn("chat_id", refreshed)

    def test_link_session_header_must_be_a_valid_capability(self):
        status, body, _ = self.request(
            "/api/telegram/link-status", session="not-a-session"
        )
        self.assertEqual(status, 401)
        self.assertIn("session", body["error"])


if __name__ == "__main__":
    unittest.main()
