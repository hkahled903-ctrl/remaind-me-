"""One-minute due-timer worker invoked by the configured QStash schedule."""

from __future__ import annotations

import logging

from .bot import confirmation_keyboard
from .storage import TimerStore
from .timer import REMINDER_DELAY_SECONDS, new_id, now_ms
from .transport import Telegram

log = logging.getLogger("timerbot")
DELIVERY_LEASE_MS = 120_000
DELIVERY_RETRY_MS = 30_000
REMINDER_DELAY_MS = REMINDER_DELAY_SECONDS * 1000
BATCH_SIZE = 10
QUESTION = "⏰ Time's up!\n\nDid you save it?"


class DeliveryError(RuntimeError):
    """One or more due Telegram notifications failed and remain retryable."""


def run_tick(store: TimerStore, telegram: Telegram, moment_ms: int | None = None) -> dict[str, int]:
    moment = now_ms() if moment_ms is None else moment_ms
    chats = store.due_chats(moment, BATCH_SIZE)
    claimed_count = 0
    delivered_count = 0
    failures: list[str] = []

    for chat_id in chats:
        claim = store.claim_due(
            chat_id,
            moment,
            new_id(),
            new_id(),
            new_id(),
            moment + DELIVERY_LEASE_MS,
        )
        if claim is None:
            continue
        claimed_count += 1
        kind = claim["kind"]
        log.info(
            "timer expired chat_id=%s timer_id=%s"
            if kind == "expiration"
            else "nudge due chat_id=%s timer_id=%s",
            chat_id,
            claim["timer_id"],
        )
        try:
            telegram.send_message(
                chat_id,
                QUESTION,
                reply_markup=confirmation_keyboard(
                    claim["timer_id"], claim["confirmation_id"]
                ),
            )
        except Exception:
            log.exception(
                "Telegram delivery failed; notification remains retryable chat_id=%s delivery_id=%s",
                chat_id,
                claim["delivery_id"],
            )
            store.release_delivery(
                chat_id,
                claim,
                (now_ms() if moment_ms is None else moment) + DELIVERY_RETRY_MS,
            )
            failures.append(chat_id)
            continue
        delivered = store.finish_delivery(
            chat_id,
            claim,
            now_ms() if moment_ms is None else moment,
            REMINDER_DELAY_MS,
        )
        if not delivered:
            log.error(
                "Telegram accepted a notification but Redis no longer recognizes its claim "
                "chat_id=%s delivery_id=%s",
                chat_id,
                claim["delivery_id"],
            )
            failures.append(chat_id)
            continue
        delivered_count += 1
        log.info(
            "%s sent chat_id=%s timer_id=%s",
            "confirmation" if kind == "expiration" else "5-minute reminder",
            chat_id,
            claim["timer_id"],
        )

    if failures:
        raise DeliveryError(
            f"{len(failures)} Telegram notification(s) failed; retry remains scheduled"
        )
    return {"due": len(chats), "claimed": claimed_count, "delivered": delivered_count}
