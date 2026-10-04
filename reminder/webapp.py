"""Web. The Connect page, the interval picker, and the webhook that turns a
Telegram "Start" into a Binding.

Why this exists
---------------
A bot cannot message a chat until the user has pressed Start in Telegram. That is a
Telegram rule, not a limitation of this code. So "press Connect and the reminder
arrives" always means: press Connect, press Start once in Telegram, and never think
about it again.

The flow, end to end:

    browser                this app                      Telegram
       |-- GET /connect/start -->|                              |
       |<-- {nonce, deep_link} --|                              |
       |------- open t.me/<bot>?start=<nonce> ------------------>|
       |                              |<---- POST /telegram/webhook (the Start)
       |                              |  (store.chat_id)
       |-- GET /connect/status ----->|                              |
       |<-- {"connected": true} -----|
       |                              |
       |                    09:00 GitHub Actions -> store.load() -> send

The browser never holds a token, the server never polls, and nothing has to stay
awake: Telegram pushes the update to this endpoint when it happens.

The page's primary figure is a countdown, not a clock face: the question a user
actually arrives with is "when is the next one?", and a ticking number answers it
directly where a dial only made them read it. The interval sits underneath in a
single segmented control, because the choices are magnitudes on one scale rather
than unrelated buttons.

Standard library only: `http.server` and `urllib`, the same constraints the rest of
the runtime already accepts.
"""

from __future__ import annotations

import hmac
import json
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .binding import BindingStore, binding_from_update
from .confirm import SAVED, ConfirmStore, resolve
from .logs import log
from .policy import next_occurrence, seconds_until_next_slot
from .settings import SettingsStore, validate_interval
from .transport import Telegram

# Nonces are held only long enough for a human to press a button. They are
# remembered in memory, not in the binding store, because they are a freshness
# marker and nothing else: if the process restarts and forgets one, the status
# endpoint falls back to "a binding exists" rather than failing the user.
NONCE_TTL_SECONDS = 15 * 60
MAX_NONCES = 500

SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"

# How the interval is written in the control. `None` means the original daily
# mode, which is not an interval at all and gets its own word.
INTERVAL_LABELS = {0: "Daily", 15: "15 min", 30: "30 min", 60: "1 hour", 180: "3 hours"}


from .page import PAGE



class ConnectApp:
    """Routing and state, with no HTTP framework attached, so it is testable."""

    def __init__(
        self,
        store: BindingStore,
        secret: str,
        bot_username: str,
        send=None,
        ack=None,
        answer=None,
        confirm_store: ConfirmStore | None = None,
        on_saved=None,
        settings_store: SettingsStore | None = None,
        reminder_time: str = "09:00",
        tz=None,
    ):
        self.store = store
        self._secret = secret or ""
        self.bot_username = bot_username
        self._send = send  # callable(str) -> None; injected so tests need no network
        # A distinct "you're connected" line, so the acknowledgement does not
        # pretend to be the reminder itself. Falls back to `send` when the caller
        # has only one message to offer.
        self._ack = ack or send
        # callable(callback_id, text) -> None, for closing the button spinner.
        self._answer = answer
        self.confirm_store = confirm_store
        # callable() -> None, run when the user presses "I saved". Owned by
        # `commands`, because stopping the schedule is business logic and not
        # routing; this module only knows when the button was pressed.
        self._on_saved = on_saved
        self.settings_store = settings_store
        self.reminder_time = reminder_time
        self.tz = tz
        self._nonces: dict[str, float] = {}
        self._lock = threading.Lock()

    # -- schedule ----------------------------------------------------------
    def schedule(self) -> tuple[int, dict]:
        """The current repeat setting and how long until the next reminder.

        The countdown is computed server-side and the browser ticks down from it,
        so a wrong client clock cannot make the page promise a time that was never
        calculated.
        """
        settings = self.settings_store.load() if self.settings_store else None
        interval = settings.interval_minutes if settings else 0
        if settings is not None and settings.is_silent:
            # A stopped timer has nothing to count down to, and showing the old
            # countdown would be a promise the server is not going to keep.
            return 200, {
                "interval_minutes": interval,
                "seconds_until": 0,
                "label": "stopped",
                "stopped": True,
                "options": [
                    {"value": value, "label": INTERVAL_LABELS[value]}
                    for value in sorted(INTERVAL_LABELS)
                ],
            }
        now = datetime_now(self.tz)

        if interval:
            seconds = seconds_until_next_slot(now, interval)
            sentence = interval
        else:
            target = next_occurrence(now, self.reminder_time)
            seconds = max(0, int((target - now).total_seconds()))
            sentence = "day"
        return 200, {
            "interval_minutes": interval,
            "seconds_until": seconds,
            "label": sentence,
            # A list, not a mapping: the browser iterates these directly, and a
            # JSON object is not iterable in JavaScript. Being explicit here is
            # cheaper than debugging `for...of` failing in a browser.
            "options": [
                {"value": value, "label": INTERVAL_LABELS[value]}
                for value in sorted(INTERVAL_LABELS)
            ],
        }

    def set_interval(self, body: bytes) -> tuple[int, dict]:
        """Change how often the reminder repeats. Rejects anything unlisted."""
        if self.settings_store is None:
            return 503, {"ok": False, "error": "Intervals are not available on this host."}
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return 400, {"ok": False, "error": "Send the interval as JSON, e.g. {\"minutes\": 30}."}
        try:
            minutes = validate_interval(payload.get("minutes"))
        except ValueError as exc:
            return 400, {"ok": False, "error": f"{str(exc).rstrip('.')}. Pick one of the listed intervals."}
        settings = self.settings_store.load()
        settings.interval_minutes = minutes
        settings.last_slot = ""  # a stale marker must not survive a change
        settings.stopped = False  # choosing a time is how a stopped timer starts
        self.settings_store.save(settings)
        log(f"repeat interval set from the page: every {minutes} minutes" if minutes
            else "repeat interval cleared; back to one reminder a day")
        now = datetime_now(self.tz)
        seconds = (
            seconds_until_next_slot(now, minutes)
            if minutes
            else max(0, int((next_occurrence(now, self.reminder_time) - now).total_seconds()))
        )
        return 200, {
            "ok": True,
            "interval_minutes": minutes,
            "seconds_until": seconds,
            "stopped": False,
        }

    # -- nonce bookkeeping -------------------------------------------------
    def new_nonce(self) -> tuple[str, float]:
        nonce = secrets.token_urlsafe(12)
        now = time.time()
        with self._lock:
            if len(self._nonces) >= MAX_NONCES:
                cutoff = now - NONCE_TTL_SECONDS
                self._nonces = {k: v for k, v in self._nonces.items() if v > cutoff}
            if len(self._nonces) >= MAX_NONCES:
                self._nonces.clear()  # drop everything rather than grow without bound
            self._nonces[nonce] = now
        return nonce, now

    def _issued(self, nonce: str) -> float | None:
        with self._lock:
            issued = self._nonces.get(nonce)
        if issued is not None:
            return issued
        # Unknown nonce: either it expired, or the process restarted after the
        # browser opened the page. Both mean the only safe reading is "check the
        # store", which the status handler does anyway.
        return None

    # -- routes -----------------------------------------------------------
    def page(self) -> tuple[int, str, str]:
        return 200, "text/html; charset=utf-8", PAGE

    def start(self) -> tuple[int, dict]:
        """Mint a nonce and the deep link the browser should open."""
        if not self.bot_username:
            return 503, {
                "error": "Bot username is unknown. Set BOT_USERNAME, or let --serve "
                "discover it from TELEGRAM_BOT_TOKEN."
            }
        nonce, issued = self.new_nonce()
        return 200, {
            "nonce": nonce,
            "issued": f"{issued:.3f}",
            "deep_link": f"https://t.me/{self.bot_username}?start={nonce}",
        }

    def status(self, query: dict) -> tuple[int, dict]:
        """Report whether a binding exists that postdates the page load."""
        binding = self.store.load()
        if binding is None:
            return 200, {"connected": False}
        issued = self._issued(query.get("nonce", ""))
        if issued is not None and binding.bound_at:
            # A binding older than the current page load predates this attempt, so
            # the user has not pressed Start yet. Compare as floats via ISO 8601,
            # both produced by this process in UTC with the same format.
            try:
                from datetime import datetime

                bound = datetime.fromisoformat(binding.bound_at).timestamp()
                if bound < issued - 1:  # one second of slack for clock formatting
                    return 200, {"connected": False}
            except ValueError:
                pass  # unparseable stamp: fall through and trust the binding
        return 200, {"connected": True, "label": binding.label, "chat_id": binding.chat_id}

    def webhook(self, secret_header: str, body: bytes) -> tuple[int, dict]:
        """Receive a Telegram update. A `/start` binds, with or without a nonce.

        The deep link from the Connect button produces `/start <nonce>`. A person
        who opens the bot in Telegram and taps Start produces a bare `/start`,
        which is the same intent expressed the other way. Binding only the
        two-part form made the button look broken for anyone who started in
        Telegram instead of on the page.
        """
        if not self._secret:
            log("ERROR: WEBHOOK_SECRET is not set; refusing webhook updates.")
            return 503, {"error": "webhook secret not configured"}
        if not hmac.compare_digest(secret_header or "", self._secret):
            log("WARNING: rejected a webhook update with a bad secret token.")
            return 403, {"error": "bad secret token"}

        try:
            update = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return 400, {"error": "body is not JSON"}

        message = update.get("message") or update.get("edited_message")
        if not message:
            if "callback_query" in update:
                return self.callback(update)
            return 200, {"ok": True, "bound": False}  # nothing to bind; not an error
        if "chat" not in message:
            return 200, {"ok": True, "bound": False}

        text = str(message.get("text", "")).strip()
        parts = text.split()
        if not parts or parts[0] != "/start":
            return 200, {"ok": True, "bound": False}

        if len(parts) > 1:
            with self._lock:
                known = parts[1] in self._nonces
            if not known:
                # A /start from someone who did not come from our page. Telegram
                # still permits the bot to message them, so binding them is not
                # harmful, but it is not what the button promised either. Accept
                # it and say so.
                log("note: /start from an unknown nonce; binding anyway.")

        binding = binding_from_update(message["chat"])
        self.store.save(binding)
        log(f"bound Telegram chat {binding.chat_id}" + (f" (@{binding.username})" if binding.username else ""))
        # Say something back. A bot that swallows the Start tap in silence is
        # indistinguishable from a broken one, and this tap is the one step the
        # user has to take on their own.
        if self._ack is not None:
            try:
                self._ack(binding.chat_id)
            except Exception as exc:  # the binding stands even if the reply fails
                log(f"could not acknowledge the binding: {exc}")
        return 200, {"ok": True, "bound": True, "chat_id": binding.chat_id}

    def callback(self, update: dict) -> tuple[int, dict]:
        """A button press. Only the chat that was asked may answer.

        The ownership check is the whole point: this endpoint is authenticated
        with the webhook secret, which proves the update came from Telegram, not
        that it came from *your* chat. Without comparing `chat_id`, anyone who
        ever pressed "Not yet" once could stop somebody else's timer.
        """
        query = update.get("callback_query") or {}
        query_id = str(query.get("id", ""))
        action = str(query.get("data", ""))
        chat_id = str(((query.get("message") or {}).get("chat") or {}).get("id", ""))

        pending = self.confirm_store.load() if self.confirm_store else None
        if pending is None:
            return 200, {"ok": True, "action": action, "resolved": False}

        if str(pending.chat_id) != chat_id:
            # Not the chat that was asked. Answer the spinner so the tap does not
            # hang, but change nothing.
            log(f"WARNING: ignoring '{action}' from chat {chat_id or '?'}; not the asked chat.")
            self._ack_callback(query_id, "This reminder belongs to another chat.", alert=True)
            return 200, {"ok": True, "action": action, "resolved": False}

        if resolve(action):
            self.confirm_store.clear()
            if self._on_saved is not None:
                try:
                    self._on_saved(chat_id)
                except Exception as exc:
                    # The question is already closed. If stopping the schedule
                    # failed, say so in the log rather than pretending it worked.
                    log(f"could not stop the timer: {exc}")
            log("user saved it; the timer stops until a new time is set.")
            self._ack_callback(query_id, "Saved. No more reminders until you set a new time.")
            return 200, {"ok": True, "action": action, "resolved": True}

        # "Not yet": keep the question open and let the nudge heartbeat re-ask.
        self._ack_callback(query_id, "Okay, I will ask again.")
        return 200, {"ok": True, "action": action, "resolved": False}

    def _ack_callback(self, callback_id: str, text: str, alert: bool = False) -> None:
        if self._answer is None or not callback_id:
            return
        try:
            self._answer(callback_id, text, alert)
        except Exception as exc:
            # The state change already happened; failing to close a spinner is
            # cosmetic and must not be reported as a failed press.
            log(f"could not close the button spinner: {exc}")

    def test_send(self) -> tuple[int, dict]:
        binding = self.store.load()
        if binding is None:
            return 409, {"ok": False, "error": "not connected yet"}
        if self._send is None:
            return 503, {"ok": False, "error": "sending is not configured on this host"}
        try:
            self._send(binding.chat_id)
        except Exception as exc:
            # The transport's wording ("Telegram rejected the request: HTTP 401:
            # {...json...}") is for the log, not for the person looking at the
            # page. Tell them what to check and keep the detail server-side.
            log(f"test send failed: {exc}")
            return 502, {
                "ok": False,
                "error": "Telegram would not accept the message. Check that the bot "
                "token is still valid and that this chat still exists.",
            }
        return 200, {"ok": True}

    def reset(self) -> tuple[int, dict]:
        self.store.clear()
        log("binding cleared.")
        return 200, {"ok": True}


def datetime_now(tz):
    """The current moment, always timezone-aware.

    A bare `datetime.now()` would fall back to the host's clock, and the whole
    countdown rests on knowing the user's timezone rather than the server's.
    """
    from datetime import datetime, timezone

    return datetime.now(tz or timezone.utc)


def _make_handler(app: ConnectApp):
    class Handler(BaseHTTPRequestHandler):
        server_version = "reminder-connect"

        def log_message(self, fmt, *args):  # quieter than the default; we log ourselves
            pass

        def _reply(self, status: int, payload, content_type: str = "application/json"):
            if content_type.startswith("application/json"):
                body = json.dumps(payload).encode("utf-8")
            else:
                body = payload if isinstance(payload, bytes) else payload.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            flat = {k: v[0] for k, v in query.items()}
            if parsed.path in ("/", "/index.html"):
                status, ctype, body = app.page()
                self._reply(status, body, ctype)
            elif parsed.path == "/connect/start":
                status, data = app.start()
                self._reply(status, data)
            elif parsed.path == "/connect/status":
                status, data = app.status(flat)
                self._reply(status, data)
            elif parsed.path == "/connect/interval":
                status, data = app.schedule()
                self._reply(status, data)
            elif parsed.path == "/healthz":
                self._reply(200, {"ok": True})
            else:
                self._reply(404, {"error": "not found"})

        def do_POST(self):
            parsed = urllib.parse.urlparse(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            if parsed.path == "/telegram/webhook":
                status, data = app.webhook(self.headers.get(SECRET_HEADER, ""), body)
            elif parsed.path == "/connect/interval":
                status, data = app.set_interval(body)
            elif parsed.path == "/connect/test":
                status, data = app.test_send()
            elif parsed.path == "/connect/reset":
                status, data = app.reset()
            else:
                status, data = 404, {"error": "not found"}
            self._reply(status, data)

    return Handler


def register_webhook(public_url: str, secret: str, client: Telegram) -> None:
    """Point Telegram at this host. Idempotent; safe to call on every boot."""
    url = f"{public_url.rstrip('/')}/telegram/webhook"
    client.call("setWebhook", {"url": url, "secret_token": secret, "allowed_updates": ["message"]})
    log(f"webhook registered at {url}")


def serve(app: ConnectApp, host: str = "0.0.0.0", port: int = 8000) -> ThreadingHTTPServer:
    """Start the server and block. Returns only if the server stops."""
    server = ThreadingHTTPServer((host, port), _make_handler(app))
    log(f"connect page on http://{host}:{port}")
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return server