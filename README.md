# Daily report reminder

Sends you a Telegram message at a fixed time each day. It **stores nothing** — it
reads one config file and reminds you.

It runs on a free server, so your machine can be off, asleep, or thrown away.

See [ROADMAP.md](ROADMAP.md) for where this is going and what is still broken.

---

## Layout

```
telegram-daily-reminder/
├── reminder/                  the code, one module per concern
│   ├── config.py              settings, credentials, file locations
│   ├── policy.py              "is it time to send?" -- the only answer
│   ├── transport.py           the Telegram HTTP call and its retries
│   ├── state.py               what has already been sent today
│   ├── binding.py             which chat to remind (file or Upstash)
│   ├── settings.py            the repeat interval and the last period served
│   ├── page.py                the Connect screen, as one string of HTML
│   ├── webapp.py              routing, nonces, and the Telegram webhook
│   ├── commands.py            one function per mode
│   ├── cli.py                 argument parsing and exit codes
│   ├── logs.py                how a line reaches a human
│   └── __main__.py            python -m reminder
├── config.json                your settings
├── tests/
│   ├── test_scenarios.py      real HTTP, real subprocesses
│   ├── test_connect.py        the Connect page over real sockets
│   ├── test_interval.py       the repeating schedule and its boundaries
│   ├── test_audit.py          one test per bug found in review
│   └── test_pipeline.py       is the CI setup still safe?
└── .github/workflows/
    ├── daily-reminder.yml     sends the reminder
    ├── interval-reminder.yml  the */5 heartbeat that drives interval mode
    └── ci.yml                 runs the tests on every push
```

Standard library only at runtime. PyYAML is a test-time convenience; the suite still
runs without it, just with fewer structural checks.

### Commands

Run everything as `python -m reminder` from this folder.

| Command | What it does | Exit |
| --- | --- | --- |
| `--dry-run` | Print the message and whether right now is the window | 0 |
| `--once` | Send one message right now and exit | 0 / 1 |
| `--once --force` | Send now even if today was already recorded | 0 |
| `--whoami` | Print your chat id (refuses when a webhook is registered) | 0 / 1 |
| `--connect` | Link your Telegram chat without a web page | 0 / 1 |
| `--serve` | Run the hosted Connect page and the webhook | — |
| `--every 30` | Set the repeat interval: 15, 30, 60 or 180. Needs no token | 0 / 1 |
| `--interval` | One heartbeat. Sends only if this period is unserved; refuses on a runner whose disk does not survive | 0 / 1 |
| `--nudge` | Ask again if the last reminder went unanswered. Silent when nothing is open | 0 |
| `--scheduled` | The CI mode: send only if the window is open | 0 / 1 |
| `--help` | Usage. Never needs credentials | 0 |
| *(no flag)* | Stay up and send every day | — |
| *unknown flag* | Rejected, so a typo cannot start the endless mode | 2 |

---

## First run: 4 steps

### 1. Create a Telegram bot

Open [@BotFather](https://t.me/BotFather) → send `/newbot` → pick a name → pick a
username. It replies with a **token**.

> That token is a password. Do not put it in a file or commit it anywhere.

### 2. Get your chat id

Message the new bot anything (even `hi`). Then, in PowerShell or Command Prompt:

```powershell
$env:TELEGRAM_BOT_TOKEN = "your_token"
python -m reminder --whoami
```

```
[14:02:11] bot is live as @my_report_bot
[14:02:11] chats that have messaged this bot:
[14:02:11]   987654321   <- Hassan
```

### 3. Fill in the config

`config.json`:

```json
{
  "telegram_chat_id": "987654321",
  "reminder_time": "09:00",
  "timezone": "Africa/Cairo",
  "message": "Reminder: your daily report is due."
}
```

### 4. Prove it works before you rely on it

```bash
python -m unittest discover -s tests -t . -v   # 76 tests, no credentials needed
python -m reminder --dry-run                   # print the message, send nothing
python -m reminder --once                      # send one right now
```

When the message arrives, continue.

---

## Connecting Telegram without copying a chat id

Step 2 above is the fiddly part: finding a number out of a log and pasting it into
JSON. You can skip it.

> **One tap is unavoidable.** Telegram only lets a bot message a chat that has
> pressed **Start** on it. No website can create that chat for you. Every flow below
> is therefore "press Connect, press Start once, never think about it again" --
> and after that the reminder needs nothing from you.

### The short way (nothing to host)

```bash
$env:TELEGRAM_BOT_TOKEN = "your_token"
python -m reminder --connect
```

Prints a link. Open it, press Start, and the command waits for Telegram to confirm
and exits `0`. The chat id is saved to `binding.json`, which is gitignored. From
then on `--once` and the daily cron send to that chat, and `config.json` can stay
empty.

### The hosted page

`--serve` runs a one-page site whose only job is that Connect button, plus the
Telegram webhook behind it. No polling and nothing to keep awake: Telegram pushes
the `/start` to `/telegram/webhook` when it happens.

```bash
$env:WEBHOOK_SECRET   = "<32+ random chars>"   # required; --serve refuses without it
$env:PUBLIC_URL      = "https://your-host"     # where the page is reachable
$env:BOT_USERNAME    = "my_report_bot"         # optional; discovered if omitted
python -m reminder --serve
```

| Variable | Needed for |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | everything that sends, and discovering `BOT_USERNAME` |
| `WEBHOOK_SECRET` | `--serve`. Refuses to start without it, because an unauthenticated webhook would let a stranger repoint your reminder at their chat |
| `PUBLIC_URL` | registering the webhook. Without it `--serve` still runs, but Telegram cannot reach it |
| `BOT_USERNAME` | optional; one `getMe` call finds it |
| `UPSTASH_REDIS_REST_URL` / `UPSTASH_REDIS_REST_TOKEN` | optional; both together switch the binding store from a local file to Upstash, which is what you want on a host whose disk does not survive a redeploy |

Deploy it anywhere that runs a Python process with a public HTTPS URL (Koyeb,
Fly.io, Render, Cloud Run). Point `PUBLIC_URL` at the deployed URL and Telegram
registers itself on boot.

> **If you use the hosted page, add `UPSTASH_REDIS_REST_URL` and
> `UPSTASH_REDIS_REST_TOKEN` to GitHub Actions secrets as well as to the host.**
> The 09:00 send runs on an ephemeral runner: without them it falls back to a local
> `binding.json` that it writes and throws away, and a chat connected through the
> page would silently stop being reachable. (Leave them unset and the reminder
> simply falls back to `config.json`, which is the old behaviour.)

### Which chat gets used

`config.json` wins if it has one; the binding is the fallback. Nobody who
configured a number by hand is affected, and first run needs nothing configured.

---

## Did you save it?

Every reminder carries two buttons: **I saved it** and **Not yet**. The feature is
one promise, and it is worth stating exactly:

> Set the time once. The reminder asks. If nobody answers, it asks again every
> five minutes. Press **I saved it** and *everything stops* until a new time is set.

**Stopping means stopped.** Pressing "I saved" clears the open question *and* sets
`stopped` in the settings store. Without the second half the next period would
still arrive, which is the behaviour this feature exists to remove. Choosing a new
time on the page, or with `--every N`, clears the flag and starts it again.

`--nudge` runs on its own `*/5` cron and sends nothing when no question is open,
which is what makes the stop real rather than cosmetic. Asking is capped at
`MAX_NUDGES` (12) so a chat that never answers is not pestered forever.

An open question also outranks the schedule: while one is unanswered, `--interval`
sends no new reminder. It would be nagging twice over.

**Only the chat that was asked can answer.** The webhook secret proves an update
came from Telegram, not that it came from *your* chat, so the handler compares the
callback's `chat.id` with the pending record and ignores anyone else.

## Repeating every N minutes

```bash
python -m reminder --every 30    # 15, 30, 60 or 180. 0 restores the daily reminder.
```

Or pick it on the Connect page: the segmented control under the countdown. The
countdown is computed on the server and ticks down from there, so a wrong client
clock cannot make the page promise a time nobody calculated.

Two workflows run side by side and this changes neither. The daily one keeps its
`0 7 * * *` cron and still reads `reminder_time`. The new `interval-reminder.yml`
is a **heartbeat**, firing every five minutes, and `cmd_interval` decides whether
any particular beat belongs to a period that has not been served.

**Why not put the interval in the cron?** Because the cron cannot follow a setting
you change on a page -- it is a file in git, and a runner reads whatever was last
committed. Worse, "have I already sent this period?" cannot live in git either: a
runner writes its file and discards it, so every beat would look like a fresh
period and you would be reminded every five minutes for the rest of the day.

So the interval and the served-marker live in the same durable store as the chat
binding: **Upstash when configured, a local file otherwise.**

> **Set `UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN` before enabling
> interval mode.** Without them the heartbeat is not safe to run: each runner
> forgets what it sent. `--interval` detects this case and **refuses to send**
> rather than reminding you 288 times a day, naming the two variables above. The
> guard looks for `CI`/`GITHUB_ACTIONS`/`GITLAB_CI`/`BUILDKITE`/`TF_BUILD`; a
> durable disk on some other CI can opt out with `REMINDER_ALLOW_FILE_INTERVAL=1`.
> `tests/test_audit.py` covers both branches.

**What "every 30 minutes" honestly means.** GitHub will not run a cron finer than
five minutes, and even that is only a request -- scheduled workflows queue and
commonly start several minutes late. Treat the interval as approximate, not as a
timer. If precision matters more than "roughly every half hour", a process that
stays up (`cmd_loop`, or systemd/Task Scheduler) is the honest choice; a cron is
not.

Periods are numbered from the Unix epoch, so a restart cannot open a fresh window
and re-send. `set_interval` clears the marker when you change the interval.

**Cost:** the heartbeat fires about 288 times a day. Each run is a few seconds.
Widen the cron if that matters -- and then the smallest offered reminder with it.

---

## Let it run by itself

Push the folder to a GitHub repository (private is fine), then:

`Settings` → `Secrets and variables` → `Actions` → `New repository secret`

| | |
| --- | --- |
| Name | `TELEGRAM_BOT_TOKEN` |
| Value | your token |

That is the whole setup. Both workflows are ready:

- **`Daily report reminder`** fires at `0 7 * * *` UTC = **09:00 Cairo**.
- **`CI`** runs the 76 tests on every push and pull request, so a broken change is
  caught before it can reach your morning.

Press **Run workflow** on either to try it now.

### The workflow is hardened

Because a reminder that silently stops is worse than no reminder:

- **Actions are pinned to commit SHAs**, not tags like `@v4`, which can be
  repointed at new code. Dependabot updates them; the version is noted in a comment.
- **`permissions: contents: read`** — the job cannot write to your repository.
- **`timeout-minutes: 5`** — a hang cannot hold a runner.
- **`concurrency`** — two reminders can never overlap.
- **A missing secret fails loudly** with `::error::` instead of a quiet skip.
- **`tzdata` is installed**, because without it `zoneinfo` silently falls back to
  UTC and the reminder would fire at the wrong hour.

`tests/test_pipeline.py` asserts all of the above, so the protection cannot rot.

### Updating the pinned actions

When Dependabot does not: replace the SHA in both workflow files with the SHA from
the upstream release, and update the `# v4` comment. Get it from
`https://api.github.com/repos/actions/checkout/commits/v4` (same for
`setup-python`). Never invent one — a wrong SHA breaks the pipeline.

---

## Changing the time

Two places, or the change silently does not apply:

1. `config.json` → `reminder_time`
2. `.github/workflows/daily-reminder.yml` → the `cron` number

The cron is **UTC**. Egypt has been UTC+2 year-round since 2023.

| Your time | `cron` |
| --- | --- |
| 07:00 | `0 5 * * *` |
| 09:00 | `0 7 * * *` |
| 13:00 | `0 11 * * *` |
| 17:00 | `0 15 * * *` |
| 23:00 | `0 21 * * *` |

If you change one and not the other, **CI fails the build** (`test_p10`).

> **Known gap:** if the UTC offset ever moves *backwards*, the cron can fire before
> the window opens and the reminder is silently skipped. Reproduced in
> `test_s4b`; the fix is phase P1 in the roadmap.

---

## Running without GitHub

`python -m reminder` with no flags stays up and sends every day by itself, but it
needs your machine open — the thing we are avoiding. For that case: a **systemd**
unit on Linux, or **Task Scheduler** on Windows.

---

## When it goes wrong

| Symptom | Cause | Fix |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN is not set` | Environment variable not set | Redo step 2 |
| `Telegram rejected the request: HTTP 400` | Wrong chat id | Re-run `--whoami` |
| `the bot has no messages yet` | The bot has not received anything | Message it first |
| Nothing at the configured time | Cron and config disagree | Check the last run log in the Actions tab |
| Nothing sent, no error | The G1 gap above | Look for `outside the reminder window` in the log |
| CI is red after an edit | A pipeline invariant broke | The failing test name says which one |
| `ModuleNotFoundError: No module named 'zoneinfo'` | No timezone database | `pip install tzdata` |

---

## Security

The script reads `config.json` and nothing else from your machine. It has no access
to your reports. The token lives only in the environment, or in a GitHub Secret —
never in the code, which `test_h2` and `test_p3` enforce.

To switch it off, delete the workflows. To invalidate the token, send `/revoke` to
@BotFather.
