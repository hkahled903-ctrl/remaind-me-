"""State. Owns the record of what has already been sent today.

Depends on nothing. Note the limitation this cannot solve: on an ephemeral CI
runner the file is written and discarded, so it does not survive a re-run
(ROADMAP.md gap G2, reproduced by test_s3).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .logs import log


def mark_sent(day: str, path: Path) -> None:
    """Record that `day` has been delivered."""
    try:
        path.write_text(
            json.dumps(
                {"last_sent_date": day, "last_sent_at": datetime.now().isoformat()},
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        log(f"could not write {path.name}: {exc}")


def already_sent(day: str, path: Path) -> bool:
    """True if `day` was already recorded. A corrupt file means False."""
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("last_sent_date") == day
    except (OSError, ValueError):
        return False
