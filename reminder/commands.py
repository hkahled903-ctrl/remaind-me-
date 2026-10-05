"""Commands. One function per mode, composed from the layers below.

Each command answers with an exit code and does not touch sys.exit, so every
mode is callable from a test without spawning a process.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import chat_id_from, parse_hhmm
from .confirm import (
    ConfirmStore,
    confirm_store_from_env,
    nudge_text,
    reminder_keyboard,
)
from .logs import diagnostic, diagnostic_exception, log
from .policy import next_occurrence, should_send, slot_for
from .settings import (
    Settings,
    interval_mode_is_safe,
    settings_store_from_env,
    validate_interval,
)
from .state import already_sent, mark_sent as mark_sent_today
from .timer import (
    activate,
    cycle_store_from_env,
    mark_sent,
    serve_cycles,
    stop_cycle,
    utcnow,
)
from .transport import Telegram
from .webapp import ConnectApp, register_webhook, serve


def resolve_chat_id(config: dict, store=None) -> str:
    """The chat to remind: the configured id, else the one the user connected.

    Precedence matters. An explicit `telegram_chat_id` always wins, so anyone who
    configured a number by hand keeps exactly the behaviour they had. The binding
    is consulted only when that field is empty -- the first-run state.

    When nothing is bound this delegates to `chat_id_from`, so the failure a user
    sees is still the one this project has always raised.
    """
    configured = str(config.get("telegram_chat_id", "")).strip()
    if configured:
        return configured
    if store is not None:
        try:
            binding = store.load()
        except Exception as exc:
            # An unreachable store must not be mistaken for an unbound user, and
            # must not be mistaken for a valid id either. Say so, then fall back.
            log(f"could not read the connected chat ({exc}); using config.json only.")
            binding = None
        if binding is not None and binding.chat_id.strip():
            return binding.chat_id
    return chat_id_from(config)


def deliver(
    config: dict,
    tz,
    client: Telegram,
    state_path: Path,
    force: bool = False,
    store=None,
    confirm_store: ConfirmStore | None = None,
    now=None,
) -> bool:
    """Send today's reminder unless it is already recorded. True if sent.

    When a confirmation store is available the message carries the two buttons
    and the question is marked open. Without one the reminder still sends, so a
    host that has not configured it degrades to the old behaviour rather than
    going quiet.
    """
    chat_id = resolve_chat_id(config, store)
    moment = now if now is not None else datetime.now(tz)
    today = moment.date().isoformat()

    if not force and already_sent(today, state_path):
        log(f"already recorded for {today}; skipping (use --force to send anyway)")
        return False

    if confirm_store is not None:
        ask_with_buttons(client, confirm_store, chat_id, config["message"], moment)
    else:
        client.send_message(chat_id, config["message"])
    mark_sent_today(today, state_path)
    log(f"sent to chat_id={chat_id}")
    return True


def cmd_scheduled(
    config: dict, tz, client: Telegram, state_path: Path, store=None, confirm_store=None,
    settings_store=None, now=None,
) -> int:
    """The old once-a-day sender. Retired: it now refuses to send.

    It used to fire on its own schedule from `config.json`'s `reminder_time`,
    independently of the chat's cycle -- so it could message somebody who had
    pressed "I saved it", and it could fire at 09:00 for a user who asked for
    every 30 minutes. Two senders with two ideas of what "due" means is exactly
    the bug this rewrite removed. The interval engine in `serve_due` is the only
    thing that sends.

    The flag is kept so an old workflow or a muscle-memory command exits 0 with
    an explanation rather than "unrecognised argument" and a red build.
    """
    log("the daily reminder has been retired. The interval engine sends reminders;")
    log("set an interval on the Connect page and a cycle will run on its own.")
    return 0


def cmd_once(config, tz, client, state_path, force: bool = False, store=None, confirm_store=None) -> int:
    deliver(
        config, tz, client, state_path, force=force, store=store, confirm_store=confirm_store
    )
    return 0


def cmd_dry_run(config, tz) -> int:
    """Print what would be sent, and whether right now is the window."""
    hour, minute = parse_hhmm(config["reminder_time"])
    now = datetime.now(tz)
    log(f"time: {hour:02d}:{minute:02d} {config['timezone']} (now {now:%Y-%m-%d %H:%M})")
    log(f"in window right now: {should_send(now, config['reminder_time'])}")
    log(f"message:\n{config['message']}")
    return 0


def cmd_whoami(client: Telegram) -> int:
    """Print the chat ids that have messaged the bot.

    Refuses when a webhook is registered. `getUpdates` and a webhook cannot both
    receive updates, so reading them here would fail with a confusing 409 -- and
    worse, on a bot that is *not* on a webhook, the call would consume the queue
    and eat the very `/start` the user just sent.
    """
    me = client.call("getMe", {})
    log(f"bot is live as @{me.get('result', {}).get('username', '?')}")

    try:
        hook = client.call("getWebhookInfo", {}).get("result", {})
    except Exception as exc:
        log(f"could not read the webhook state ({exc}); continuing.")
        hook = {}
    if hook.get("url"):
        log("ERROR: a webhook is registered, so Telegram holds the updates here.")
        log(f"       {hook['url']}")
        log("The connected chat is already known to the app; check /connect/status.")
        return 1

    chats: dict[str, str] = {}
    for update in client.call("getUpdates", {"limit": 100}).get("result", []):
        message = update.get("message") or update.get("channel_post")
        if message and "chat" in message:
            chat = message["chat"]
            label = (
                chat.get("title")
                or chat.get("username")
                or chat.get("first_name")
                or "?"
            )
            chats[str(chat["id"])] = label

    if not chats:
        log("the bot has no messages yet. Send it any message in Telegram, then retry.")
        return 1
    log("chats that have messaged this bot:")
    for chat_id, label in chats.items():
        log(f"  {chat_id}   <- {label}")
    log("copy your id into config.json as telegram_chat_id")
    return 0


def cmd_loop(config, tz, client, state_path, store=None) -> int:
    """Stay up and send every day. Needs a machine that stays on."""
    hour, minute = parse_hhmm(config["reminder_time"])
    log(f"running; daily at {hour:02d}:{minute:02d} {config['timezone']}")
    while True:
        now = datetime.now(tz)
        target = next_occurrence(now, config["reminder_time"])
        hours = (target - now).total_seconds() / 3600
        log(f"next send {target:%Y-%m-%d %H:%M} (in {hours:.1f}h)")
        time.sleep(max(0.0, (target - now).total_seconds()) + 1)
        try:
            deliver(config, tz, client, state_path, store=store)
        except Exception as exc:
            log(f"send failed: {exc}")


STOPPED_NOTICE = (
    "Timer is off. No reminders will be sent until you set a new time."
)


def stop_timer(cycle_store, client: Telegram | None = None, chat_id: str = "") -> int:
    """The 'I saved' handler: end this chat's cycle, and only this chat's.

    Scoping the write to `chat_id` is the whole multi-user guarantee here. The
    old version mutated one global record, so one person finishing their report
    silently cancelled everybody else's timer.
    """
    if not chat_id:
        log("ERROR: 'I saved it' arrived with no chat id; nothing was stopped.")
        return 1
    cycle_store.save(stop_cycle(cycle_store.load(chat_id)))
    log(f"chat {chat_id} saved; its timer is off until a new time is set.")
    if client is not None and chat_id:
        try:
            client.send_message(chat_id, STOPPED_NOTICE)
        except Exception as exc:
            log(f"could not confirm the stop: {exc}")
    return 0


def serve_due(
    client: Telegram, store, message: str, now=None, diagnostic_id: str | None = None
) -> int:
    """One heartbeat: message every chat whose next send time has arrived.

    Both crons call this. There is one engine, not two, so the "first reminder"
    and the "nudge" are the same act distinguished only by which timestamp was
    crossed -- and the two five-minute workflows cannot disagree about it.

    Duplicate protection is two independent guards, because the two crons can
    overlap and GitHub can retry:

    1. `claim` is a Redis SET NX. Whichever worker creates the key owns this
       beat; the other sees False and sends nothing. The key expires, so a worker
       that dies mid-send cannot lock a chat out of the next one.
    2. The state is written *before* the send, so a crash afterwards costs one
       missed beat rather than a duplicate message.

    That yields at-most-once delivery, not exactly-once. Telegram's sendMessage
    is not transactional with Redis and cannot be made so; when the two
    disagree, this system chooses to miss rather than to double-message.
    """
    moment = now if now is not None else utcnow()
    sent = 0
    for cycle in serve_cycles(store, moment, diagnostic_id=diagnostic_id):
        if diagnostic_id:
            diagnostic(
                "CLAIM_ATTEMPTED",
                diagnostic_id,
                chat_id=cycle.chat_id,
                cycle=cycle.cycle,
            )
        try:
            claimed = store.claim(cycle.chat_id, cycle.cycle)
        except Exception as exc:
            if diagnostic_id:
                diagnostic_exception(
                    "CLAIM_ERROR",
                    diagnostic_id,
                    exc,
                    redactions=(
                        getattr(store, "_url", ""),
                        getattr(store, "_token", ""),
                    ),
                    stage="CLAIM",
                    chat_id=cycle.chat_id,
                    cycle=cycle.cycle,
                )
            raise
        if diagnostic_id:
            diagnostic(
                "CLAIM_RESULT",
                diagnostic_id,
                chat_id=cycle.chat_id,
                cycle=cycle.cycle,
                claimed=claimed,
            )
        if not claimed:
            log(f"chat {cycle.chat_id}: another worker owns this beat; skipping.")
            continue
        first = not cycle.pending
        text = message if first else nudge_text(cycle.nudges + 1)
        updated_cycle = mark_sent(cycle, moment)
        try:
            store.save(updated_cycle)
        except Exception as exc:
            if diagnostic_id:
                diagnostic_exception(
                    "STATE_UPDATE_ERROR",
                    diagnostic_id,
                    exc,
                    redactions=(
                        getattr(store, "_url", ""),
                        getattr(store, "_token", ""),
                    ),
                    stage="STATE_UPDATE",
                    chat_id=cycle.chat_id,
                )
            raise
        if diagnostic_id:
            diagnostic(
                "STATE_UPDATE",
                diagnostic_id,
                chat_id=cycle.chat_id,
                status="saved_before_send",
                marked_sent=updated_cycle.pending,
                stopped=updated_cycle.stopped,
                asked_at=updated_cycle.asked_at,
                last_sent_at=updated_cycle.last_sent_at,
            )
            diagnostic(
                "TELEGRAM_SEND_ATTEMPTED",
                diagnostic_id,
                chat_id=cycle.chat_id,
                message_kind="reminder" if first else "nudge",
            )
        try:
            response = client.send_message(
                cycle.chat_id, text, reply_markup=reminder_keyboard()
            )
        except Exception as exc:
            # The record already says this beat was served. Releasing the claim
            # would let a retry send it again; leaving it is what makes this
            # at-most-once.
            log(f"chat {cycle.chat_id}: Telegram rejected the message; this beat is lost.")
            if diagnostic_id:
                diagnostic_exception(
                    "TELEGRAM_SEND_ERROR",
                    diagnostic_id,
                    exc,
                    redactions=(getattr(client, "_token", ""),),
                    stage="TELEGRAM_SEND",
                    chat_id=cycle.chat_id,
                    state_marked_sent=True,
                )
            raise
        if diagnostic_id:
            result = response.get("result") if isinstance(response, dict) else None
            diagnostic(
                "TELEGRAM_SEND_RESULT",
                diagnostic_id,
                chat_id=cycle.chat_id,
                returned=True,
                telegram_ok=response.get("ok") if isinstance(response, dict) else None,
                message_id=result.get("message_id") if isinstance(result, dict) else None,
            )
            diagnostic(
                "STATE_AFTER_SEND",
                diagnostic_id,
                chat_id=cycle.chat_id,
                state_transition="none",
                persisted_before_send=True,
                marked_sent=updated_cycle.pending,
                stopped=updated_cycle.stopped,
                asked_at=updated_cycle.asked_at,
                last_sent_at=updated_cycle.last_sent_at,
            )
        log(
            f"{'reminder' if first else f'nudge {cycle.nudges + 1}'} sent to "
            f"chat {cycle.chat_id}"
        )
        sent += 1
    if not sent:
        log("nothing was due on this beat.")
    if diagnostic_id:
        diagnostic(
            "SERVE_DUE_COMPLETE",
            diagnostic_id,
            serve_due_return_value=sent,
        )
    return sent


def cmd_interval(
    config: dict,
    tz,
    client: Telegram,
    store,
    now=None,
    diagnostic_id: str | None = None,
    **kwargs,
) -> int:
    # Exit code, not a message count: a quiet beat must be 0 so the cron is not
    # red every five minutes. `serve_due` raises if Telegram refuses.
    serve_due(
        client,
        store,
        config.get("message", ""),
        now,
        diagnostic_id=diagnostic_id,
    )
    return 0


def cmd_nudge(client: Telegram, store, now=None, **kwargs) -> int:
    serve_due(client, store, "", now)
    return 0


CONFIRMATION = (
    "Connected. This chat will get the daily report reminder.\n\n"
    "Nothing else to do here -- close Telegram and carry on."
)


def port_from_env(default: int = 8000) -> int:
    """The port to listen on, preferring the host's `PORT`.

    Every PaaS -- Render, Koyeb, Fly.io, Railway, Cloud Run -- assigns a port
    through the `PORT` environment variable and routes its public hostname to
    exactly that port. A server that hardcodes 8000 looks healthy locally and then
    gets 502s in production, so the platform's value wins when it is sane.
    """
    raw = os.environ.get("PORT", "").strip()
    if not raw:
        return default
    try:
        port = int(raw)
    except ValueError:
        log(f"WARNING: PORT={raw!r} is not a number; using {default}.")
        return default
    if not 1 <= port <= 65535:
        log(f"WARNING: PORT={port} is out of range; using {default}.")
        return default
    return port


def bot_username(client: Telegram) -> str:
    """The @name a deep link needs. One extra API call, so it is cached per run."""
    cached = os.environ.get("BOT_USERNAME", "").strip()
    if cached:
        return cached.lstrip("@")
    return str(client.call("getMe", {}).get("result", {}).get("username", "") or "")


def cmd_connect(client: Telegram, store, timeout: float = 180.0) -> int:
    """Connect a Telegram chat without a web page: print a link, then wait.

    Same outcome as the hosted Connect button, for someone who would rather not
    deploy anything yet. Exits 0 the moment Telegram reports the chat, and 1 on
    timeout having changed nothing.
    """
    username = bot_username(client)
    if not username:
        log("Could not read the bot username from TELEGRAM_BOT_TOKEN.")
        return 1
    # The secret is irrelevant here: nothing in this path accepts a webhook, so a
    # placeholder keeps ConnectApp's refuse-when-unset behaviour off our backs.
    app = ConnectApp(store, secret="not-used-in-terminal-mode", bot_username=username)
    status, data = app.start()
    if status != 200:
        log(str(data.get("error", "could not start a connection")))
        return 1
    log("Open this link in Telegram and press Start:")
    log(f"  {data['deep_link']}")
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(2)
        _, result = app.status({"nonce": data["nonce"]})
        if result.get("connected"):
            label = result.get("label", "")
            log(f"Connected as {label}. Nothing else to do." if label else "Connected.")
            return 0
    log("No Start received before the timeout. Nothing was changed; try again.")
    return 1


def cmd_serve(
    config: dict,
    tz,
    client: Telegram,
    store,
    host: str = "0.0.0.0",
    port: int = 8000,
    confirm_store: ConfirmStore | None = None,
) -> int:
    """Run the hosted Connect page and the webhook that backs it.

    Refuses to start without WEBHOOK_SECRET: without it the webhook endpoint would
    accept any `/start` from any stranger and could repoint the reminder at
    someone else's chat. Failing here is better than failing quietly in public.
    """
    # Check the secret before anything that touches the network. A host with a
    # missing secret is misconfigured, and the useful answer is to say so
    # immediately rather than after a round trip to Telegram.
    secret = os.environ.get("WEBHOOK_SECRET", "").strip()
    if not secret:
        log("ERROR: WEBHOOK_SECRET is not set. Generate one and set it, then retry.")
        log("       e.g. python -c \"import secrets;print(secrets.token_urlsafe(32))\"")
        return 1
    username = bot_username(client)
    if not username:
        log("Could not read the bot username from TELEGRAM_BOT_TOKEN.")
        return 1
    public_url = os.environ.get("PUBLIC_URL", "").strip()
    if not public_url:
        log("WARNING: PUBLIC_URL is not set, so the Telegram webhook was not registered.")
        log("         Set it to this host's public https URL, or Telegram cannot reach it.")

    message = config["message"]
    settings = settings_store_from_env()
    app = ConnectApp(
        store,
        secret,
        username,
        send=lambda chat_id: client.send_message(chat_id, message),
        ack=lambda chat_id: client.send_message(chat_id, CONFIRMATION),
        answer=lambda callback_id, text, alert: client.answer_callback(callback_id, text, alert),
        confirm_store=confirm_store,
        # Pressing "I saved" stops the schedule, which lives in the settings
        # store. Wiring it here keeps that rule in one place.
        on_saved=lambda chat_id: stop_timer(settings, client, chat_id),
        settings_store=settings,
        reminder_time=config["reminder_time"],
        tz=tz,
    )
    if public_url:
        register_webhook(public_url, secret, client)
    serve(app, host, port_from_env(port))
    return 0


def set_interval(store, minutes, chat_id: str, now=None) -> int:
    """Activate a new interval for exactly one chat. The only way out of STOPPED.

    The previous version wrote a global settings record, so five people shared
    one timer and whoever pressed the button last owned it. Now the chat id is
    part of the write, and `activate` discards the previous cycle's open
    question so a new interval can never inherit a stale one.
    """
    try:
        value = validate_interval(minutes)
    except ValueError as exc:
        log(str(exc))
        return 1
    if not chat_id:
        log("ERROR: no chat id to activate a timer for.")
        return 1
    moment = now if now is not None else utcnow()
    cycle = store.load(chat_id)
    store.save(activate(cycle, value, moment))
    store.remember(chat_id)
    due = store.load(chat_id).due_at
    log(
        f"chat {chat_id}: every {value} minutes, first reminder at "
        f"{due.strftime('%H:%M UTC') if due else 'unknown'}."
    )
    return 0
