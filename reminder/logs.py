"""Output. Owns how a line reaches a human.

Depends on nothing, so everything else may log without creating a cycle.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

LOG_PATH = Path(__file__).resolve().parent.parent / "reminder.log"


def configure_console() -> None:
    """Force UTF-8 on stdout/stderr.

    A Windows console defaults to cp1252 and raises UnicodeEncodeError on any
    non-ASCII output, which would kill every mode. Safe to call more than once.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # not a TextIOWrapper
            pass


def log(message: str) -> None:
    """Write one timestamped line to stdout and to reminder.log. Never raises."""
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    try:
        print(line, flush=True)
    except (OSError, UnicodeError):
        try:
            print(repr(line), flush=True)
        except Exception:
            pass
    try:
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass  # an unwritable log must never stop a reminder
