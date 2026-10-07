"""Vercel WSGI entry point for Telegram updates and the QStash tick."""

from __future__ import annotations

import hmac
import json
import logging
import os
from urllib.parse import urlsplit

from reminder.bot import TimerBot
from reminder.scheduler import run_tick
from reminder.storage import RedisError, TimerStore
from reminder.transport import Telegram

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())
log = logging.getLogger("timerbot")
TELEGRAM_SECRET_HEADER = "HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN"
AUTHORIZATION_HEADER = "HTTP_AUTHORIZATION"
QSTASH_FORWARD_AUTHORIZATION_HEADER = "HTTP_UPSTASH_FORWARD_AUTHORIZATION"
MIN_SCHEDULER_SECRET_LENGTH = 32


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _dependencies() -> tuple[TimerStore, Telegram]:
    store = TimerStore(
        _env("UPSTASH_REDIS_REST_URL"),
        _env("UPSTASH_REDIS_REST_TOKEN"),
    )
    telegram = Telegram(_env("TELEGRAM_BOT_TOKEN"))
    return store, telegram


def _response(start_response, status: int, payload: dict):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    reason = {
        200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found",
        405: "Method Not Allowed", 413: "Payload Too Large", 500: "Internal Server Error",
        502: "Bad Gateway", 503: "Service Unavailable",
    }.get(status, "Error")
    start_response(
        f"{status} {reason}",
        [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"),
        ],
    )
    return [body]


def application(environ, start_response):
    method = str(environ.get("REQUEST_METHOD", "GET")).upper()
    path = urlsplit(environ.get("PATH_INFO", "/")).path
    if method == "GET" and path == "/healthz":
        return _response(start_response, 200, {"ok": True})
    if path not in ("/telegram/webhook", "/internal/tick"):
        return _response(start_response, 404, {"error": "not found"})
    if method != "POST":
        return _response(start_response, 405, {"error": "POST required"})

    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except (TypeError, ValueError):
        return _response(start_response, 400, {"error": "invalid content length"})
    if length < 0 or length > 1_000_000:
        return _response(start_response, 413, {"error": "request body too large"})
    body = environ.get("wsgi.input").read(length) if length else b"{}"

    try:
        if path == "/telegram/webhook":
            secret = _env("WEBHOOK_SECRET")
            if not secret:
                return _response(start_response, 503, {"error": "webhook secret is not configured"})
            provided = str(environ.get(TELEGRAM_SECRET_HEADER, ""))
            if not hmac.compare_digest(provided, secret):
                return _response(start_response, 403, {"error": "forbidden"})
            try:
                update = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                return _response(start_response, 400, {"error": "body must be JSON"})
            if not isinstance(update, dict):
                return _response(start_response, 400, {"error": "update must be a JSON object"})
            store, telegram = _dependencies()
            status, result = TimerBot(store, telegram).handle_update(update)
            return _response(start_response, status, result)

        scheduler_secret = _env("TIMER_SCHEDULER_SECRET")
        if len(scheduler_secret) < MIN_SCHEDULER_SECRET_LENGTH:
            return _response(start_response, 503, {"error": "scheduler secret is not configured"})
        expected_authorization = f"Bearer {scheduler_secret}"
        authorization = str(environ.get(AUTHORIZATION_HEADER, ""))
        forwarded_authorization = str(environ.get(QSTASH_FORWARD_AUTHORIZATION_HEADER, ""))
        authorized = hmac.compare_digest(authorization, expected_authorization) | hmac.compare_digest(
            forwarded_authorization, expected_authorization
        )
        if not authorized:
            return _response(start_response, 403, {"error": "forbidden"})
        store, telegram = _dependencies()
        result = run_tick(store, telegram)
        return _response(start_response, 200, {"ok": True, **result})
    except RedisError:
        log.exception("Redis operation failed")
        return _response(start_response, 503, {"error": "persistent timer storage unavailable"})
    except Exception:
        log.exception("request failed")
        if path == "/internal/tick":
            return _response(start_response, 502, {"error": "timer worker failed; due work remains retryable"})
        return _response(start_response, 502, {"error": "Telegram request failed"})


app = application
