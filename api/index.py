"""Vercel WSGI entry point for Telegram updates and the QStash tick."""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import secrets
from pathlib import Path
from urllib.parse import quote, urlsplit

from reminder.bot import TimerBot
from reminder.scheduler import run_tick
from reminder.storage import RedisError, TimerStore
from reminder.timer import now_ms
from reminder.transport import Telegram

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())
log = logging.getLogger("timerbot")
TELEGRAM_SECRET_HEADER = "HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN"
AUTHORIZATION_HEADER = "HTTP_AUTHORIZATION"
QSTASH_FORWARD_AUTHORIZATION_HEADER = "HTTP_UPSTASH_FORWARD_AUTHORIZATION"
WEBSITE_SESSION_HEADER = "HTTP_X_WEBSITE_SESSION"
MIN_SCHEDULER_SECRET_LENGTH = 32
WEBSITE_SESSION_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
WEBSITE_DURATIONS_MINUTES = (15, 30, 60)


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _dependencies() -> tuple[TimerStore, Telegram]:
    store = _store()
    telegram = Telegram(_env("TELEGRAM_BOT_TOKEN"))
    return store, telegram


def _store() -> TimerStore:
    return TimerStore(
        _env("UPSTASH_REDIS_REST_URL"),
        _env("UPSTASH_REDIS_REST_TOKEN"),
    )


def _response(start_response, status: int, payload: dict):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    reason = {
        200: "OK", 201: "Created", 400: "Bad Request", 401: "Unauthorized",
        403: "Forbidden", 404: "Not Found", 409: "Conflict",
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


def _website_response(start_response):
    try:
        body = Path(__file__).with_name("website.html").read_bytes()
    except OSError:
        log.exception("Website asset unavailable")
        return _response(start_response, 500, {"error": "website interface unavailable"})
    start_response(
        "200 OK",
        [
            ("Content-Type", "text/html; charset=utf-8"),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-cache"),
            ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "no-referrer"),
            ("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'"),
        ],
    )
    return [body]


def _website_session(environ) -> str | None:
    session_id = str(environ.get(WEBSITE_SESSION_HEADER, ""))
    return session_id if WEBSITE_SESSION_PATTERN.fullmatch(session_id) else None


def _website_link_create(start_response):
    username = _env("TELEGRAM_BOT_USERNAME").lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
        return _response(start_response, 503, {"error": "Telegram bot username is not configured"})
    session_id = secrets.token_urlsafe(32)
    link_token = secrets.token_urlsafe(32)
    store = _store()
    store.create_website_link(session_id, link_token)
    return _response(
        start_response,
        201,
        {
            "session_id": session_id,
            "telegram_url": f"https://t.me/{username}?start={quote(link_token, safe='')}",
        },
    )


def _website_link_status(environ, start_response):
    session_id = _website_session(environ)
    if not session_id:
        return _response(start_response, 401, {"error": "website session required"})
    session = _store().get_website_session(session_id)
    if not session:
        return _response(start_response, 404, {"error": "link session expired; connect Telegram again"})
    return _response(start_response, 200, {"connected": session.get("state") == "LINKED"})


def _website_timer_status(environ, start_response):
    session_id = _website_session(environ)
    if not session_id:
        return _response(start_response, 401, {"error": "website session required"})
    store = _store()
    session = store.get_website_session(session_id)
    if not session or session.get("state") != "LINKED":
        return _response(start_response, 409, {"error": "connect Telegram before checking a timer"})
    timer = store.get(session["chat_id"])
    if not timer:
        return _response(start_response, 200, {"state": "IDLE"})
    result = {
        "state": timer.get("state", "IDLE"),
        "duration_seconds": int(timer.get("duration_seconds", "0")),
        "started_at": int(timer.get("started_at", "0")),
        "expires_at": int(timer.get("expires_at", "0")),
        "next_reminder_at": int(timer.get("next_reminder_at") or "0"),
    }
    result["server_now_ms"] = now_ms()
    result["remaining_seconds"] = max(0, (result["expires_at"] - result["server_now_ms"] + 999) // 1000)
    return _response(start_response, 200, result)


def _website_timer_start(environ, body, start_response):
    session_id = _website_session(environ)
    if not session_id:
        return _response(start_response, 401, {"error": "website session required"})
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return _response(start_response, 400, {"error": "body must be JSON"})
    if not isinstance(payload, dict):
        return _response(start_response, 400, {"error": "body must be a JSON object"})
    if "chat_id" in payload:
        return _response(start_response, 400, {"error": "chat_id is not accepted"})
    duration_minutes = payload.get("duration_minutes")
    if (
        isinstance(duration_minutes, bool)
        or not isinstance(duration_minutes, int)
        or duration_minutes not in WEBSITE_DURATIONS_MINUTES
    ):
        return _response(start_response, 400, {"error": "choose 15, 30, or 60 minutes"})

    store = _store()
    session = store.get_website_session(session_id)
    if not session or session.get("state") != "LINKED":
        return _response(start_response, 409, {"error": "connect Telegram before starting a timer"})
    outcome = TimerBot(store, Telegram(_env("TELEGRAM_BOT_TOKEN"))).start_timer(
        session["chat_id"], duration_minutes * 60, f"website:{secrets.token_urlsafe(16)}"
    )
    if outcome != "started":
        return _response(start_response, 409, {"error": "a timer is already active"})
    return _website_timer_status(
        {**environ, WEBSITE_SESSION_HEADER: session_id},
        start_response,
    )


def application(environ, start_response):
    method = str(environ.get("REQUEST_METHOD", "GET")).upper()
    path = urlsplit(environ.get("PATH_INFO", "/")).path
    if method == "GET" and path == "/healthz":
        return _response(start_response, 200, {"ok": True})
    if method == "GET" and path == "/":
        return _website_response(start_response)
    api_get_routes = {
        "/api/telegram/link-status": _website_link_status,
        "/api/timer/status": _website_timer_status,
    }
    if method == "GET" and path in api_get_routes:
        try:
            return api_get_routes[path](environ, start_response)
        except RedisError:
            log.exception("Redis operation failed")
            return _response(start_response, 503, {"error": "persistent timer storage unavailable"})
    if path not in (
        "/telegram/webhook",
        "/internal/tick",
        "/api/telegram/link",
        "/api/timer/start",
    ):
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
        if path == "/api/telegram/link":
            return _website_link_create(start_response)
        if path == "/api/timer/start":
            return _website_timer_start(environ, body, start_response)
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
