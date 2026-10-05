"""Output. Owns how a line reaches a human.

Depends on nothing, so everything else may log without creating a cycle.
"""

from __future__ import annotations

import json
import os
import re
import sys
import traceback
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


def diagnostic(event: str, invocation_id: str, **fields) -> None:
    """Emit one structured interval-worker event without request credentials."""
    payload = {
        "component": "interval_cron",
        "event": event,
        "invocation_id": invocation_id,
        **fields,
    }
    log(f"INTERVAL_DIAGNOSTIC {json.dumps(payload, sort_keys=True, default=str)}")


def diagnostic_exception(
    event: str,
    invocation_id: str,
    exc: Exception,
    redactions=(),
    **fields,
) -> None:
    """Log exception context and a traceback after removing credential material."""
    message = _redact_sensitive_text(str(exc), redactions)
    trace = _redact_sensitive_text(
        "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        redactions,
    )
    diagnostic(
        event,
        invocation_id,
        exception_type=type(exc).__name__,
        exception_message=message,
        traceback=trace,
        **fields,
    )


def _redact_sensitive_text(value: str, redactions=()) -> str:
    """Remove configured credentials and URL/header values from diagnostic text."""
    for secret in redactions:
        if secret:
            value = value.replace(str(secret), "[REDACTED]")
    for name, secret in os.environ.items():
        if secret and re.search(r"secret|token|password|credential|authorization|cookie|url", name, re.I):
            value = value.replace(secret, "[REDACTED]")
    value = re.sub(r"https?://[^\s'\"<>]+", "[URL REDACTED]", value, flags=re.I)
    value = re.sub(
        r"(?i)\b(authorization|cookie|token|secret|password)(\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+",
        r"\1\2[REDACTED]",
        value,
    )
    return value
