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
    MAX_NUDGES,
    ConfirmStore,
    ask,
    confirm_store_from_env,
    is_due,
    nudge_text,
    record_nudge,
    reminder_keyboard,
)
from .logs import log
from .policy import next_occurrence, should_send, slot_for
from .settings import (
    Settings,
    interval_mode_is_safe,
    settings_store_from_env,
    validate_interval,
)
from .state import already_sent, mark_sent
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
    mark_sent(today, state_path)
    log(f"sent to chat_id={chat_id}")
    return True


def cmd_scheduled(
    config, tz, client, state_path, store=None, confirm_store=None, settings_store=None
) -> int:
    """The CI entry point. Skips cleanly when the window is not open."""
    now = datetime.now(tz)
    if settings_store is not None and settings_store.load().is_silent:
        log("the timer was stopped after 'I saved'; nothing to send.")
        return 0
    if not should_send(now, config["reminder_time"]):
        log(
            f"outside the reminder window (now {now.strftime('%H:%M')}, "
            f"reminder {config['reminder_time']}); skipping"
        )
        return 0
    deliver(config, tz, client, state_path, store=store, confirm_store=confirm_store)
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


def stop_timer(settings_store, client: Telegram | None = None, chat_id: str = "") -> int:
    """The 'I saved' handler: silence the schedule, not just the question.

    Both halves matter. Clearing the pending stops the nudges; setting `stopped`
    stops the *next period* from arriving as well. Without the second, the user
    presses 'I saved', feels finished, and gets another reminder fifteen minutes
    later -- which is the behaviour this feature exists to remove.
    """
    settings = settings_store.load()
    settings.stopped = True
    settings.last_slot = ""
    settings_store.save(settings)
    log("saved; the timer is off until a new time is set.")
    if client is not None and chat_id:
        try:
            client.send_message(chat_id, STOPPED_NOTICE)
        except Exception as exc:
            log(f"could not confirm the stop: {exc}")
    return 0


def ask_with_buttons(
    client: Telegram,
    confirm_store: ConfirmStore,
    chat_id: str,
    text: str,
    now: datetime,
) -> None:
    """Send the reminder with its two buttons, and open the question.

    A message with no buttons would leave the user nothing to press, and the
    nudge heartbeat would then nag someone who already finished. The buttons and
    the open question are therefore the same act.
    """
    client.send_message(chat_id, text, reply_markup=reminder_keyboard())
    confirm_store.save(ask(chat_id, now))


def cmd_nudge(client: Telegram, confirm_store: ConfirmStore, now=None, every_minutes: int = 5) -> int:
    """Ask again if -- and only if -- the question is open and due.

    This is the second half of the feature. It runs on its own short cron and
    sends nothing at all when there is no open question, which is what makes
    "I saved it" genuinely stop the reminders.
    """
    moment = now if now is not None else datetime.now(timezone.utc)
    pending = confirm_store.load()
    if pending is None:
        return 0
    if not is_due(pending, moment, every_minutes):
        return 0
    if pending.nudges >= MAX_NUDGES:
        log(f"giving up after {pending.nudges} nudges; nothing was sent.")
        confirm_store.clear()
        return 0
    updated = record_nudge(pending, moment)
    client.send_message(pending.chat_id, nudge_text(updated.nudges), reply_markup=reminder_keyboard())
    confirm_store.save(updated)
    log(f"nudge {updated.nudges} sent to chat_id={pending.chat_id}")
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


def cmd_interval(
    config: dict,
    tz,
    client: Telegram,
    settings_store,
    binding_store=None,
    now=None,
    confirm_store: ConfirmStore | None = None,
) -> int:
    """One heartbeat of the repeating schedule.

    The cron fires on a fixed fine-grained beat and this decides whether that
    particular beat belongs to a period that has not been served yet. Two things
    make it safe to run often:

    * the period is derived from the clock, not from process start, so a restart
      cannot open a fresh window and re-send;
    * the served marker is written to the durable store, so the next runner --
      a different machine with no memory of this one -- sees it.

    Exits 0 in every case, including "nothing to do", so a quiet heartbeat is not
    a red build. `now` is injectable so the period boundaries are testable without
    a fake clock, the same way `policy` takes the moment it is deciding about.
    """
    settings = settings_store.load()
    if settings.is_silent:
        log("the timer was stopped after 'I saved'; nothing to do on this heartbeat.")
        log("       Set a new time (--every, or the page) to start it again.")
        return 0
    if not settings.is_interval:
        log("interval reminders are off; nothing to do on this heartbeat.")
        return 0

    # The dangerous case is not "interval mode is off", it is "interval mode is on
    # and this host cannot remember what it already sent". On an ephemeral CI
    # runner that means every */5 beat sends, so the user gets 288 messages a day.
    # Refusing is louder than sending and hoping.
    if not interval_mode_is_safe(settings_store):
        log("ERROR: this runner's disk does not survive the run, so interval mode")
        log("       cannot tell a served period from a new one. Every heartbeat")
        log("       would send. Nothing was sent.")
        log("       Fix: set UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN,")
        log("       or REMINDER_ALLOW_FILE_INTERVAL=1 if this disk really is durable.")
        return 1

    now = now if now is not None else datetime.now(tz)
    if now.tzinfo is None:
        log("ERROR: the heartbeat timestamp has no timezone; refusing to guess.")
        return 1
    slot = slot_for(now, settings.interval_minutes)
    if settings.last_slot == slot:
        log(f"period {slot} already served; nothing to do.")
        return 0

    # An unanswered question outranks the schedule. The user has been asked
    # "did you save it?" and has not said; sending another reminder on top of that
    # is nagging twice over, and it is the exact case the buttons exist to stop.
    # `--nudge` keeps asking; this heartbeat stays quiet until they answer.
    if confirm_store is not None:
        pending = confirm_store.load()
        if pending is not None and pending.is_active:
            log("an open question is waiting for an answer; no new reminder sent.")
            log("       `--nudge` will keep asking until the user presses a button.")
            return 0

    chat_id = resolve_chat_id(config, binding_store)
    if confirm_store is not None:
        ask_with_buttons(client, confirm_store, chat_id, config["message"], now)
    else:
        client.send_message(chat_id, config["message"])
    settings.last_slot = slot
    settings_store.save(settings)
    log(
        f"sent to chat_id={chat_id} for period {slot} "
        f"(every {settings.interval_minutes} minutes)"
    )
    return 0


def set_interval(settings_store, minutes, confirm_store: ConfirmStore | None = None) -> int:
    """Store a new interval. Rejects anything not on the offered list.

    Setting a time is what resumes a stopped timer, so any open question is
    cleared here. Without that, a user who had pressed "I saved" would be asked
    again by the nudge heartbeat for the previous report.
    """
    try:
        value = validate_interval(minutes)
    except ValueError as exc:
        log(str(exc))
        return 1
    if confirm_store is not None:
        confirm_store.clear()
    settings = settings_store.load()
    # Changing the period must not inherit the old marker: the new slot numbering
    # differs, and carrying a stale value across would be harmless only by luck.
    settings.interval_minutes = value
    settings.last_slot = ""
    # Setting a time is how a stopped timer starts again.
    settings.stopped = False
    settings_store.save(settings)
    log("daily mode restored" if value == 0 else f"reminder interval set to every {value} minutes")
    return 0
