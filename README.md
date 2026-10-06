# Telegram countdown timer

A small persistent timer controlled from Telegram:

1. Send `/start` to the bot.
2. Start a timer with `/timer 30` (minutes).
3. Close Telegram, your browser, or your device. The expiry is stored in Redis.
4. When the timer expires, the bot asks **“Did you save it?”**
5. Press **✅ Yes, I saved it** to stop, or **⏱️ Give me more time** to start the
   same duration again. Without an answer, the bot asks again every five minutes.

There is one timer per private Telegram chat. Starting another timer while one is
active is rejected. Duration input is a whole number from 1 to 1440 minutes.

## Runtime design

- `reminder/timer.py` contains timestamp and callback-data helpers.
- `reminder/storage.py` stores one timer hash per chat and performs every state
  change through Redis Lua scripts. Redis is required; no local-file fallback is
  used in production.
- `reminder/bot.py` handles `/start`, `/timer`, `/status`, and the two confirmation
  callbacks.
- `reminder/scheduler.py` claims due timers, sends their Telegram messages, and
  records success or makes failures retryable.
- `api/index.py` exposes the Telegram webhook and shared-secret-authenticated scheduler
  tick as Vercel serverless routes.

Redis keys:

```text
timer:{chat_id}  -> hash containing the state and timestamps for that chat
timers:due       -> sorted set of active chat IDs, scored by their next due time
```

The timer hash stores `chat_id`, `state`, `duration_seconds`, `started_at`,
`expires_at`, `next_reminder_at`, `timer_id`, `confirmation_id`, `completed_at`,
and temporary delivery-claim fields. Timestamps are UTC epoch milliseconds.
The sorted set is an index, not a second source of timer state.

### Delivery and retry behavior

The scheduler atomically claims a due event for a bounded lease before contacting
Telegram. A successful Telegram response is followed by an atomic Redis
finalization that schedules the next five-minute reminder. On failure, the claim
is released for retry; a worker crash is recovered after the lease expires.
Storage errors return a failed request and are never treated as an empty timer
list.

Redis and Telegram cannot share a transaction, and Telegram `sendMessage` has no
idempotency key. Delivery is therefore at-least-once: if Telegram accepted a
message but the worker lost the response before recording success, a retry may
send a duplicate. This deliberately favors retrying a reminder over permanently
losing it. While a delivery lease is active, callback transitions return a
retryable failure so they cannot race a claimed send.

## Deployment

### Required environment variables

Configure runtime values in Vercel Production. The Telegram bot token and webhook
secret are also needed in the Infisical environment used by webhook registration:

| Variable | Purpose |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot API access |
| `WEBHOOK_SECRET` | Telegram webhook secret-token validation |
| `UPSTASH_REDIS_REST_URL` | Upstash Redis REST endpoint |
| `UPSTASH_REDIS_REST_TOKEN` | Upstash Redis access token |
| `TIMER_SCHEDULER_SECRET` | Random scheduler secret (at least 32 characters) |

`LOG_LEVEL` is optional. Keep all credentials in environment/Infisical; never put
values in this repository. `.env.example` lists names only.

### Telegram webhook

Deploy the project to Vercel, then run the manually triggered
**Register Telegram webhook** GitHub Actions workflow. It injects the bot token
and webhook secret through Infisical and registers `/telegram/webhook` with
Telegram. The webhook accepts only Telegram requests with the configured
`X-Telegram-Bot-Api-Secret-Token`.

### The one production scheduler

Keep exactly one existing recurring schedule in the Upstash QStash dashboard as a
deployment/setup concern—not from application code:

- Method: `POST`
- Destination: `https://remaind-me-xi.vercel.app/internal/tick`
- Cron: `* * * * *` (once per minute)
- Body: `{}`
- Forwarded header: `Upstash-Forward-Authorization: Bearer <TIMER_SCHEDULER_SECRET>`

Set the forwarded header value to the exact Production `TIMER_SCHEDULER_SECRET`
as `Bearer <secret>`. The endpoint compares it in constant time with the
configured secret and rejects missing or invalid credentials. Do not expose the
secret in logs, repository files, or the schedule's public configuration view.
Update the existing schedule; do not create a second one.

Verify there is only one QStash schedule targeting this endpoint. Do not create
a schedule on deployment, webhook, or tick requests. Remove old QStash schedules
and disable/delete the former GitHub interval and nudge workflows; only this
one-minute QStash schedule should invoke the worker.

The tick endpoint queries `timers:due` through the current minute, and then each
candidate is atomically rechecked and claimed in Redis. Each invocation handles
at most ten candidates; any backlog remains indexed for the next minute. Delayed
or overlapping ticks cannot claim the same live lease. Scheduler cadence and
platform latency mean delivery is approximate, not real-time.

### Infisical and CI

Continue using the existing Infisical machine identity to supply secrets to the
webhook-registration workflow. GitHub Actions retain read-only repository
permissions and SHA-pinned actions. CI runs the product and WSGI tests against a
disposable Redis 7 service; tests do not use production credentials.

## Local tests

Python 3.12+ is required. Runtime dependencies are installed from
`requirements.txt`. Run the unit and API tests with:

```powershell
python -m unittest discover -s tests -t . -v
```

To run the Lua-backed integration tests locally, set `TIMER_TEST_REDIS_URL` to a
disposable Redis database. The integration tests issue `FLUSHDB`, so never point
this variable at production or a database containing user data.
