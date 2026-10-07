from __future__ import annotations

import io
import json
import os
import unittest
from unittest import mock

from api import index
from reminder.storage import RedisError

SCHEDULER_SECRET = "unit-test-secret-" + ("x" * 40)


class TelegramRecorder:
    def __init__(self):
        self.sent = []
        self.answers = []

    def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text, reply_markup))
        return {"ok": True}

    def answer_callback(self, callback_id, text="", alert=False):
        self.answers.append((callback_id, text, alert))
        return {"ok": True}


class ApiTest(unittest.TestCase):
    def request(self, path, method="POST", body=b"{}", headers=None):
        status_line = []
        response_headers = []
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "CONTENT_LENGTH": str(len(body)),
            "wsgi.input": io.BytesIO(body),
        }
        for name, value in (headers or {}).items():
            environ[name] = value

        def start_response(status, values):
            status_line.append(status)
            response_headers.extend(values)

        with mock.patch.dict(
            os.environ,
            {
                "WEBHOOK_SECRET": "telegram-secret",
                "TIMER_SCHEDULER_SECRET": SCHEDULER_SECRET,
                "UPSTASH_REDIS_REST_URL": "https://redis.invalid",
                "UPSTASH_REDIS_REST_TOKEN": "redis-token",
                "TELEGRAM_BOT_TOKEN": "bot-token",
            },
        ):
            payload = b"".join(index.application(environ, start_response))
        return int(status_line[0].split()[0]), json.loads(payload), dict(response_headers)

    def test_webhook_rejects_missing_and_invalid_secret_before_storage(self):
        with mock.patch.object(index, "_dependencies", side_effect=AssertionError("not reached")):
            status, _, _ = self.request("/telegram/webhook")
            self.assertEqual(status, 403)
            status, _, _ = self.request(
                "/telegram/webhook",
                headers={"HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN": "wrong"},
            )
            self.assertEqual(status, 403)

    def test_valid_start_is_routed_to_telegram_bot(self):
        telegram = TelegramRecorder()
        store = object()
        with mock.patch.object(index, "_dependencies", return_value=(store, telegram)):
            status, body, headers = self.request(
                "/telegram/webhook",
                body=json.dumps(
                    {
                        "message": {
                            "chat": {"id": 123, "type": "private"},
                            "text": "/start",
                        }
                    }
                ).encode(),
                headers={"HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN": "telegram-secret"},
            )
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertIn("/timer 30", telegram.sent[0][1])
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_redis_failure_returns_visible_service_error_for_callback_path(self):
        with mock.patch.object(index, "_dependencies", side_effect=RedisError("offline")):
            status, body, _ = self.request(
                "/telegram/webhook",
                body=b'{"message":{"chat":{"id":123,"type":"private"},"text":"/timer 30"}}',
                headers={"HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN": "telegram-secret"},
            )
        self.assertEqual(status, 503)
        self.assertIn("storage unavailable", body["error"])

    def test_tick_accepts_valid_scheduler_secret_and_uses_one_worker(self):
        store, telegram = object(), object()
        with (
            mock.patch.object(index, "_dependencies", return_value=(store, telegram)),
            mock.patch.object(
                index, "run_tick",
                return_value={"due": 2, "claimed": 1, "delivered": 1},
            ) as tick,
        ):
            status, body, _ = self.request(
                "/internal/tick",
                headers={"HTTP_AUTHORIZATION": "Bearer " + SCHEDULER_SECRET},
            )
        self.assertEqual(status, 200)
        self.assertEqual(body["delivered"], 1)
        tick.assert_called_once_with(store, telegram)

    def test_tick_accepts_qstash_forwarded_authorization_header(self):
        store, telegram = object(), object()
        with (
            mock.patch.object(index, "_dependencies", return_value=(store, telegram)),
            mock.patch.object(index, "run_tick", return_value={"due": 0, "claimed": 0, "delivered": 0}) as tick,
        ):
            status, body, _ = self.request(
                "/internal/tick",
                headers={
                    "HTTP_UPSTASH_FORWARD_AUTHORIZATION": "Bearer " + SCHEDULER_SECRET,
                },
            )
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        tick.assert_called_once_with(store, telegram)

    def test_tick_rejects_missing_or_invalid_scheduler_auth_before_worker(self):
        with (
            mock.patch.object(index, "_dependencies", side_effect=AssertionError("not reached")),
            mock.patch.object(index, "run_tick", side_effect=AssertionError("not reached")),
        ):
            status, body, _ = self.request("/internal/tick")
            self.assertEqual(status, 403)
            self.assertEqual(body, {"error": "forbidden"})
            status, _, _ = self.request(
                "/internal/tick",
                headers={"HTTP_AUTHORIZATION": "Bearer wrong"},
            )
            self.assertEqual(status, 403)

    def test_tick_redis_failure_is_not_reported_as_empty_success(self):
        with mock.patch.object(index, "_dependencies", side_effect=RedisError("offline")):
            status, body, _ = self.request(
                "/internal/tick",
                headers={"HTTP_AUTHORIZATION": "Bearer " + SCHEDULER_SECRET},
            )
        self.assertEqual(status, 503)
        self.assertIn("error", body)
        self.assertNotIn("due", body)

    def test_health_check_does_not_require_secrets_or_redis(self):
        status, body, _ = self.request("/healthz", method="GET")
        self.assertEqual((status, body), (200, {"ok": True}))


if __name__ == "__main__":
    unittest.main()
