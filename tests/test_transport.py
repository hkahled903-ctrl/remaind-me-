from __future__ import annotations

import json
import unittest
import urllib.parse
from unittest import mock

from reminder.transport import Telegram, TelegramError


class _Response:
    status = 200

    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


class TelegramTransportTest(unittest.TestCase):
    def test_answer_callback_encodes_boolean_for_bot_api(self):
        telegram = Telegram("test-token", "https://telegram.invalid/bot{token}/{method}")
        with mock.patch(
            "reminder.transport.urllib.request.urlopen",
            return_value=_Response(b'{"ok":true,"result":true}'),
        ) as request:
            telegram.answer_callback("cb-1", "Invalid", alert=True)
        body = request.call_args.args[0].data.decode()
        self.assertEqual(
            urllib.parse.parse_qs(body),
            {
                "callback_query_id": ["cb-1"],
                "text": ["Invalid"],
                "show_alert": ["true"],
            },
        )

    def test_non_ok_json_is_a_delivery_failure(self):
        telegram = Telegram("test-token", "https://telegram.invalid/bot{token}/{method}")
        with (
            mock.patch(
                "reminder.transport.urllib.request.urlopen",
                return_value=_Response(b'{"ok":false,"description":"chat not found"}'),
            ),
            self.assertRaisesRegex(TelegramError, "chat not found"),
        ):
            telegram.send_message("1", "hello")

    def test_successful_response_is_returned(self):
        telegram = Telegram("test-token", "https://telegram.invalid/bot{token}/{method}")
        expected = {"ok": True, "result": {"message_id": 1}}
        with mock.patch(
            "reminder.transport.urllib.request.urlopen",
            return_value=_Response(json.dumps(expected).encode()),
        ):
            self.assertEqual(telegram.send_message("1", "hello"), expected)


if __name__ == "__main__":
    unittest.main()
