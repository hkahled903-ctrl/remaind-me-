"""WSGI entry point, so the same app runs on a serverless host.

Why this file exists
--------------------
`reminder.webapp` runs on `ThreadingHTTPServer`: a long-lived process that binds a
port. Vercel does not run processes -- it runs a WSGI callable once per request
and may freeze or discard the container between requests. So the routing logic is
reused here behind the WSGI interface, and nothing else in the project changes.

Reused, not duplicated: every decision (nonce freshness, the secret check, the
owner check on a button press) lives in `ConnectApp`. This file only translates
the WSGI environ into the `(method, path, query, headers, body)` shape that
`ConnectApp` already understands, so a rule fixed there is fixed here too.

What serverless changes, honestly
---------------------------------
* `self._nonces` lives in memory. A cold start loses it. The status handler
  already treats an unknown nonce as "check the store", so the Connect flow
  degrades to "you look connected" rather than breaking -- see
  `ConnectApp._issued`.
* `binding.json` and `settings.json` are per-invocation unless a durable store is
  configured. Set Upstash (which the interval and nudge modes require anyway) and
  this file is stateless and correct.

Depends on `reminder`, and on nothing outside it.
"""

from __future__ import annotations

import hmac
import json
import os
import uuid
import urllib.parse
from datetime import datetime

from reminder.commands import CONFIRMATION, cmd_interval, port_from_env, stop_timer
from reminder.binding import store_from_env
from reminder.commands import bot_username
from reminder.confirm import confirm_store_from_env
from reminder.config import load_config, resolve_timezone
from reminder.logs import diagnostic, diagnostic_exception
from reminder.settings import settings_store_from_env
from reminder.timer import cycle_store_from_env
from reminder.transport import Telegram
from reminder.webapp import SECRET_HEADER, ConnectApp

_CONFIG = load_config()
_TZ = resolve_timezone(_CONFIG["timezone"])
_CLIENT = Telegram(os.environ.get("TELEGRAM_BOT_TOKEN", "").strip())
_SETTINGS = settings_store_from_env()
_CYCLES = cycle_store_from_env()
_CONFIRM = confirm_store_from_env()
_BINDING = store_from_env()


def _username() -> str:
    """Cached per container. A cold start pays one getMe call."""
    cached = os.environ.get("BOT_USERNAME", "").strip()
    if cached:
        return cached.lstrip("@")
    try:
        return bot_username(_CLIENT)
    except Exception:
        # A missing username must not take the whole page down: the page still
        # renders, and `/connect/start` reports the problem in plain words.
        return ""


APP = ConnectApp(
    _BINDING,
    secret=os.environ.get("WEBHOOK_SECRET", "").strip(),
    bot_username=_username(),
    send=lambda chat_id: _CLIENT.send_message(chat_id, _CONFIG["message"]),
    ack=lambda chat_id: _CLIENT.send_message(chat_id, CONFIRMATION),
    answer=lambda cb, text, alert: _CLIENT.answer_callback(cb, text, alert),
    confirm_store=_CONFIRM,
    on_saved=lambda chat_id: stop_timer(_CYCLES, _CLIENT, chat_id),
    settings_store=_SETTINGS,
    cycle_store=_CYCLES,
    reminder_time=_CONFIG["reminder_time"],
    tz=_TZ,
)


def _json_response(start_response, status: int, payload, content_type="application/json"):
    body = (
        json.dumps(payload).encode("utf-8")
        if content_type.startswith("application/json")
        else (payload.encode("utf-8") if isinstance(payload, str) else payload)
    )
    start_response(
        f"{status} {_reason(status)}",
        [
            ("Content-Type", content_type),
            ("Content-Length", str(len(body))),
            # The page polls itself; a cached copy would show a stale countdown.
            ("Cache-Control", "no-store"),
        ],
    )
    return [body]


def _reason(status: int) -> str:
    return {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found",
            409: "Conflict", 500: "Internal Server Error", 502: "Bad Gateway",
            503: "Service Unavailable"}.get(status, "OK")


def _dispatch(method, path, query, headers, body) -> tuple[int, object, str]:
    """One routing table, shared with `webapp` by construction."""
    flat = {k: v[0] for k, v in query.items()}
    if method == "GET":
        if path == "/internal/interval-cron":
            cron_secret = os.environ.get("CRON_SECRET", "").strip()
            if not cron_secret:
                return 503, {"error": "cron secret is not configured"}, "application/json"
            authorization = headers.get("authorization", "")
            if not hmac.compare_digest(authorization, f"Bearer {cron_secret}"):
                return 403, {"error": "forbidden"}, "application/json"
            diagnostic_id = (
                uuid.uuid4().hex
                if headers.get("x-interval-diagnostic") == "1"
                else None
            )
            if diagnostic_id:
                diagnostic(
                    "ENDPOINT_INVOKED",
                    diagnostic_id,
                    method="GET",
                    path="/internal/interval-cron",
                )
            try:
                cmd_interval(
                    _CONFIG,
                    _TZ,
                    _CLIENT,
                    _CYCLES,
                    diagnostic_id=diagnostic_id,
                )
            except Exception as exc:
                if diagnostic_id:
                    diagnostic_exception(
                        "ENDPOINT_FAILED",
                        diagnostic_id,
                        exc,
                        redactions=(
                            os.environ.get("UPSTASH_REDIS_REST_URL", ""),
                            os.environ.get("UPSTASH_REDIS_REST_TOKEN", ""),
                            os.environ.get("TELEGRAM_BOT_TOKEN", ""),
                            cron_secret,
                            authorization,
                        ),
                        stage="INTERVAL_WORKER",
                    )
                else:
                    raise
                return 500, {"error": "internal error"}, "application/json"
            if diagnostic_id:
                diagnostic("ENDPOINT_COMPLETE", diagnostic_id, status=200)
            return 200, {"ok": True}, "application/json"
        if path in ("/", "/index.html"):
            status, content_type, page = APP.page()
            return status, page, content_type
        if path == "/connect/start":
            return APP.start() + ("application/json",)
        if path == "/connect/status":
            return APP.status(flat) + ("application/json",)
        if path == "/connect/interval":
            return APP.schedule() + ("application/json",)
        if path == "/healthz":
            return 200, {"ok": True}, "application/json"
        return 404, {"error": "not found"}, "application/json"

    if path == "/telegram/webhook":
        return APP.webhook(headers.get(SECRET_HEADER.lower(), ""), body) + ("application/json",)
    if path == "/connect/interval":
        return APP.set_interval(body) + ("application/json",)
    if path == "/connect/test":
        return APP.test_send() + ("application/json",)
    if path == "/connect/reset":
        return APP.reset() + ("application/json",)
    return 404, {"error": "not found"}, "application/json"


def application(environ, start_response):
    """The WSGI callable Vercel invokes.

    A bad request must not become a 500 from the platform: a reminder host that
    answers every error with a blank page is impossible to debug from Telegram's
    side.
    """
    try:
        method = environ.get("REQUEST_METHOD", "GET").upper()
        parsed = urllib.parse.urlparse(environ.get("PATH_INFO", "/"))
        query = urllib.parse.parse_qs(parsed.query)
        # WSGI hands headers over as HTTP_X_FOO_BAR, so the header name has to be
        # rebuilt before anything can look it up: strip the prefix and put the
        # hyphens back. Lowercasing alone leaves underscores in the key, and a
        # lookup by the real header name then finds nothing -- which is how every
        # Telegram update came back 403 whatever the webhook secret was set to.
        headers = {
            k[5:].replace("_", "-").lower(): v
            for k, v in environ.items()
            if k.startswith("HTTP_")
        }
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            length = 0
        body = environ["wsgi.input"].read(length) if length else b""

        status, payload, content_type = _dispatch(method, parsed.path, query, headers, body)
    except Exception as exc:  # noqa: BLE001 -- a handler must not fall over
        from reminder.logs import log

        log(f"unhandled request error: {exc!r}")
        status, payload, content_type = 500, {"error": "internal error"}, "application/json"

    return _json_response(start_response, status, payload, content_type)


# Vercel's Python runtime looks for either of these names.
app = application