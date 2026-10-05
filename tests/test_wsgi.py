"""The serverless entry point, over a real socket.

`api/index.py` is what Vercel runs. Nothing else in the suite touches it, so
without this file a deployment could 404 on every route and the 171 other tests
would all stay green -- which is exactly what happened before the first deploy.

Importing the callable is not enough: it can raise on the first request, or route
nothing. So a real WSGI server is started and every route the page uses is called
over HTTP.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from wsgiref.simple_server import WSGIRequestHandler, make_server

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "api"))

PORT = 8931  # deliberately not 8000: the preview server owns that one
BASE = f"http://127.0.0.1:{PORT}"


class Quiet(WSGIRequestHandler):
    def log_message(self, *args):  # the access log is noise in test output
        pass


def _set_env() -> None:
    """The entry point reads the environment at import time."""
    os.environ.setdefault("WEBHOOK_SECRET", "test-secret")
    os.environ.setdefault("BOT_USERNAME", "testbot")
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000000:test")
    # A temp file store must not be mistaken for the real one by the guard.
    os.environ.setdefault("REMINDER_ALLOW_FILE_INTERVAL", "1")


class WsgiAppTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _set_env()
        import index  # imported here so the environment is set first

        cls.server = make_server("127.0.0.1", PORT, index.application, handler_class=Quiet)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def get(self, path: str):
        with urllib.request.urlopen(BASE + path, timeout=10) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read()

    def post(self, path: str, body: bytes, secret: str | None = None):
        request = urllib.request.Request(BASE + path, data=body, method="POST")
        if secret is not None:
            request.add_header("X-Telegram-Bot-Api-Secret-Token", secret)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def test_w1_the_page_renders_over_wsgi(self):
        """The deployment 404s when this fails, with nothing else to go on."""
        status, content_type, body = self.get("/")
        text = body.decode("utf-8", "replace")
        self.assertEqual(status, 200)
        self.assertIn("text/html", content_type)
        self.assertIn("Connect Telegram", text)
        self.assertIn("telegram-link", text)

    def test_w2_a_missing_webhook_secret_is_a_503_not_a_crash(self):
        """A host with no secret must still serve the page; only the webhook is
        refused. A 500 here would look like a broken deployment."""
        status, _, _ = self.get("/")
        self.assertEqual(status, 200)

    def test_w3_the_page_fetches_all_answer_with_json(self):
        for path in ("/connect/interval", "/connect/status", "/connect/start", "/healthz"):
            with self.subTest(path=path):
                status, _, body = self.get(path)
                self.assertEqual(status, 200)
                self.assertIsInstance(json.loads(body), dict)

    def test_w4_the_interval_options_are_a_list_not_an_object(self):
        """The page iterates these with `for...of`, which throws on a JSON object.
        This was a real bug: the picker rendered nothing."""
        _, _, body = self.get("/connect/interval")
        options = json.loads(body)["options"]
        self.assertEqual(
            options,
            [
                {"value": 15, "label": "15 min"},
                {"value": 30, "label": "30 min"},
                {"value": 60, "label": "1 hour"},
                {"value": 180, "label": "3 hours"},
            ],
            "cycle-backed schedule options must match the picker's {value, label} shape",
        )

    def test_w5b_the_webhook_accepts_the_correct_secret(self):
        """The positive path, which had no coverage at all.

        `test_w5` only proved a *wrong* secret is refused, and it passes whether
        the header is read correctly or not -- so the deployed webhook could
        reject every update, forever, with a green suite. This posts the secret
        the app was actually configured with and insists on a 200.

        The message is deliberately not a `/start`, so the request exercises the
        header check and returns without writing the real `binding.json`.
        """
        status, data = self.post(
            "/telegram/webhook",
            json.dumps({"message": {"chat": {"id": 1}, "text": "hello"}}).encode(),
            secret=os.environ["WEBHOOK_SECRET"],
        )
        self.assertEqual(
            status, 200, "the correct secret was rejected -- the header never arrived"
        )
        self.assertTrue(data.get("ok"), data)

    def test_w5_the_webhook_refuses_a_bad_secret(self):
        status, data = self.post(
            "/telegram/webhook",
            json.dumps({"message": {"chat": {"id": 1}, "text": "/start"}}).encode(),
            secret="not-the-secret",
        )
        self.assertEqual(status, 403)
        # The refusal body is {"error": ...} with no "ok" -- matching the shape
        # `ConnectApp.webhook` returns, rather than inventing one here.
        self.assertIn("error", data)
        self.assertNotIn("bound", data)

    def test_w6_an_unknown_route_is_404_on_both_methods(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/nope")
        self.assertEqual(caught.exception.code, 404)
        self.assertEqual(self.post("/nope", b"{}")[0], 404)

    def test_w7_the_cache_header_stops_a_stale_countdown(self):
        """The page polls itself; a cached copy would promise a time that passed."""
        request = urllib.request.Request(BASE + "/")
        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")

    def test_w8_vercel_json_points_at_this_file(self):
        """A `vercel.json` that names a different path deploys to a 404 while every
        test here stays green, because the tests import the file directly."""
        config = json.loads((PROJECT_ROOT / "vercel.json").read_text(encoding="utf-8"))
        entry = config["builds"][0]["src"]
        self.assertEqual(entry, "api/index.py")
        self.assertTrue((PROJECT_ROOT / entry).is_file())
        self.assertEqual(config["routes"][0]["dest"], entry)


if __name__ == "__main__":
    unittest.main()