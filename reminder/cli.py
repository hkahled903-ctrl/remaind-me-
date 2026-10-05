"""CLI. Owns argument parsing and exit codes, and nothing else.

Every mode is listed in MODES. A mode that needs the network gets a client built
first, so a missing token fails fast with a clear message instead of half-working.
"""

from __future__ import annotations

import sys

from .binding import store_from_env
from .commands import (
    cmd_connect,
    cmd_dry_run,
    cmd_interval,
    cmd_loop,
    cmd_nudge,
    cmd_once,
    cmd_scheduled,
    cmd_serve,
    cmd_whoami,
    set_interval,
)
from .confirm import confirm_store_from_env
from .config import (
    STATE_PATH,
    ConfigError,
    load_config,
    resolve_timezone,
    token_from_env,
)
from .logs import configure_console, log
from .settings import settings_store_from_env
from .timer import cycle_store_from_env
from .transport import Telegram

USAGE = (
    "usage: python -m reminder [--once [--force] | --scheduled | --dry-run | --whoami\n"
    "                          | --connect | --serve | --interval | --nudge | --every N]"
)

# Modes that never touch the network, and so need no token. `--every` only writes
# a setting, so demanding a bot token to change the repeat interval would be wrong.
# `--every` only writes a setting, so demanding a bot token to change the repeat
# interval would be wrong. `--nudge` is not here because it sends a message.
OFFLINE_MODES = {"--dry-run", "--every"}
HELP_FLAGS = {"--help", "-h"}
KNOWN_FLAGS = {
    "--once",
    "--force",
    "--scheduled",
    "--dry-run",
    "--whoami",
    "--connect",
    "--serve",
    "--interval",
    "--every",
    "--nudge",
} | HELP_FLAGS

EXIT_USAGE = 2


def main(argv: list[str]) -> int:
    configure_console()  # before any logging, so non-ASCII output survives Windows
    argv = list(argv)
    args = set(argv)

    # `--every 30` carries a value, and the unknown-flag guard below would read
    # "30" as a misspelled flag. Pull the value out before checking, so the guard
    # keeps rejecting genuine typos without eating this one legitimate argument.
    every: str | None = None
    if "--every" in args:
        index = argv.index("--every")
        if index + 1 < len(argv):
            every = argv[index + 1]
            args.discard(every)

    # Reject typos before anything else. Otherwise an unknown flag falls through
    # to the bare "run forever" mode, which looks like the tool just went quiet.
    unknown = args - KNOWN_FLAGS
    if unknown:
        log(USAGE)
        log(f"unknown option(s): {', '.join(sorted(unknown))}")
        return EXIT_USAGE

    # Help is answered before anything else, so it never needs a token.
    if args & HELP_FLAGS:
        log(USAGE)
        return 0

    config = load_config()
    tz = resolve_timezone(config["timezone"])

    client = None
    if not (args & OFFLINE_MODES):
        # Resolve the token before dispatch so a missing secret is reported
        # clearly rather than surfacing as an obscure failure inside a command.
        client = Telegram(token_from_env())

    # Where the chat came from: config.json, or the chat the user connected on the
    # web page. Resolved once, so every sending mode sees the same answer.
    store = store_from_env()

    confirm_store = confirm_store_from_env()

    if "--every" in args:
        if every is None:
            log("--every needs a number of minutes, e.g. --every 30")
            return EXIT_USAGE
        return set_interval(settings_store_from_env(), every, confirm_store)
    if "--serve" in args:
        return cmd_serve(config, tz, client, store, confirm_store=confirm_store)
    if "--interval" in args:
        return cmd_interval(config, tz, client, cycle_store_from_env())
    if "--nudge" in args:
        return cmd_nudge(client, cycle_store_from_env())
    if "--connect" in args:
        return cmd_connect(client, store)
    if "--whoami" in args:
        return cmd_whoami(client)
    if "--dry-run" in args:
        return cmd_dry_run(config, tz)
    if "--scheduled" in args:
        return cmd_scheduled(
            config, tz, client, STATE_PATH, store=store, confirm_store=confirm_store
        )
    if "--once" in args:
        return cmd_once(
            config,
            tz,
            client,
            STATE_PATH,
            force="--force" in args,
            store=store,
            confirm_store=confirm_store,
        )

    # No mode flag at all: stay up and send every day.
    return cmd_loop(config, tz, client, STATE_PATH, store=store)


def run() -> int:
    """Entry point wrapper. Owns the mapping from exceptions to exit codes."""
    try:
        return main(sys.argv[1:])
    except KeyboardInterrupt:
        log("stopped.")
        return 130
    except ConfigError as exc:
        log(f"configuration error: {exc}")
        return 1
    except Exception as exc:
        log(f"error: {exc}")
        return 1
