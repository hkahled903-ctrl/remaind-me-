"""Vercel WSGI entry point for Telegram updates and the QStash tick."""

from __future__ import annotations

import hmac
import json
import logging
import os
from urllib.parse import urlsplit

from qstash import Receiver
from qstash.errors import SignatureError

from reminder.bot import TimerBot
from reminder.scheduler import run_tick
from reminder.storage import RedisError, TimerStore
from reminder.transport import Telegram

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())
log = logging.getLogger("timerbot")
TELEGRAM_SECRET_HEADER = "HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN"
QSTASH_SIGNATURE_HEADER = "HTTP_UPSTASH_SIGNATURE"
QSTASH_TICK_URL = "https://remaind-me-xi.vercel.app/internal/tick"


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

        current_key = _env("QSTASH_CURRENT_SIGNING_KEY")
        next_key = _env("QSTASH_NEXT_SIGNING_KEY")
        if not current_key or not next_key:
            return _response(start_response, 503, {"error": "QStash signing keys are not configured"})
        signature = str(environ.get(QSTASH_SIGNATURE_HEADER, ""))
        if not signature:
            return _response(start_response, 403, {"error": "forbidden"})
        try:
            Receiver(
                current_signing_key=current_key,
                next_signing_key=next_key,
            ).verify(
                signature=signature,
                body=body.decode("utf-8"),
                url=QSTASH_TICK_URL,
            )
        except (SignatureError, UnicodeDecodeError):
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
