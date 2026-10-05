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

That command reads the process environment and nothing else. To fill that environment from Infisical rather than from your shell, put `infisical run --env=dev --` in front of it -- see [Secrets live in Infisical](#secrets-live-in-infisical).

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

## Secrets live in Infisical

Nothing in this repository holds a credential. Secrets are stored in
[Infisical](https://infisical.com) and injected into the process environment at
run time, so the code reads `os.environ` exactly as it always did. There is no
`.env` to keep in step with anything.

Set it up once:

1. Create a project named after the service. Every project starts with
   `Development`, `Staging` and `Production` environments, and you can **drag
   your existing `.env` file onto the Secrets Overview page** to import every key
   at once.
2. Install the CLI: Windows `winget install infisical` (or `scoop install
   infisical`), macOS `brew install infisical/get-cli/infisical`.
3. `infisical login`. On a machine with no browser -- WSL 2, Codespaces, a
   remote SSH session -- run `infisical login -i` instead.
4. `infisical init`, and pick the project. That writes `.infisical.json`, which
   holds the project id and no sensitive values, so it is safe to commit.

Then prefix any command with the wrapper and stop thinking about where the
values come from:

```bash
infisical run --env=dev -- python -m reminder --once
```

`--env=dev` is the slug of the `Development` environment. The wrapper passes the
rest of your environment through untouched and exits with the command's own
exit code, so `--dry-run` still sends nothing and still returns `0`.

Every key the code reads belongs in that one environment:

| Key | Needed for |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | everything that sends |
| `WEBHOOK_SECRET` | `--serve`; it refuses to start without one |
| `PUBLIC_URL` | registering the webhook |
| `BOT_USERNAME` | optional; one `getMe` call finds it |
| `UPSTASH_REDIS_REST_URL` / `UPSTASH_REDIS_REST_TOKEN` | the durable store; required for interval mode |

### Proving the values come from Infisical

Print a length, never the value:

```bash
infisical run --env=dev -- python -c "import os; print(len(os.environ.get('TELEGRAM_BOT_TOKEN','')))"
```

Then prove nothing is coming off the disk any more:

```bash
mv .env .env.backup
infisical run --env=dev -- python -m reminder --dry-run
```

If that still works, the secret came from Infisical. `.env.backup` is gitignored,
but delete it once you are satisfied -- it holds every secret you just migrated.
`.env.example` stays in the repository as the list of key names; it never holds a
value.

### In GitHub Actions

CI never runs an interactive login. It uses a **machine identity** and
[Universal Auth](https://infisical.com/docs/documentation/platform/identities/universal-auth):
create the identity under `Access Control > Machine Identities`, give it a client
secret, and add it to this project with a read role on the `dev` environment
only. Then set, in the repository, under `Settings` -> `Secrets and variables` ->
`Actions`:

| | Where | Value |
| --- | --- | --- |
| `INFISICAL_CLIENT_ID` | secret | the identity's client id |
| `INFISICAL_CLIENT_SECRET` | secret | the identity's client secret |
| `INFISICAL_PROJECT_SLUG` | variable | the project slug, from the project's `Settings > General` |

The three send workflows fetch the whole `dev` environment through the
[Infisical Secrets Action](https://github.com/Infisical/secrets-action), pinned
to a commit SHA like every other action in this repository. **No bot token and no
Upstash credential is stored in GitHub**, so rotating one changes nothing you
have to commit. The `CI` workflow deliberately fetches nothing: `--dry-run` has
to stay runnable with no credentials at all.

## First run: 4 steps

### 1. Create a Telegram bot

Open [@BotFather](https://t.me/BotFather) → send `/newbot` → pick a name → pick a
username. It replies with a **token**.

> That token is a password. Do not put it in a file or commit it anywhere.

### 2. Get your chat id

Message the new bot anything (even `hi`). Then, from this folder:

```bash
infisical run --env=dev -- python -m reminder --whoami
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
infisical run --env=dev -- python -m reminder --connect
```

Prints a link. Open it, press Start, and the command waits for Telegram to confirm
and exits `0`. The chat id is saved to `binding.json`, which is gitignored. From
then on `--once` and the daily cron send to that chat, and `config.json` can stay
empty.

### The hosted page

`--serve` runs a one-page site whose only job is that Connect button, plus the
Telegram webhook behind it. No polling and nothing to keep awake: Telegram pushes
the `/start` to `/telegram/webhook` when it happens.

With `WEBHOOK_SECRET`, `PUBLIC_URL` and optionally `BOT_USERNAME` in the `dev`
environment -- `WEBHOOK_SECRET` is 32+ random characters, and `--serve` refuses
to start without it:

```bash
infisical run --env=dev -- python -m reminder --serve
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

> **If you use the hosted page, put `UPSTASH_REDIS_REST_URL` and
> `UPSTASH_REDIS_REST_TOKEN` in Infisical's `dev` environment**, which is what
> both the host and the Actions workflows read.
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

The Production project is on Vercel Hobby, whose native Cron Jobs only support
daily schedules. To check due cycles more closely, configure an external
minute-capable scheduler to make an authenticated `GET` request to
`https://<your-production-host>/internal/interval-cron` every minute. Add a random
`CRON_SECRET` to Vercel's Production environment and configure the scheduler to
send `Authorization: Bearer <the-same-secret>`. The route rejects requests when
the secret is missing or incorrect and calls the existing `cmd_interval`
scheduler when authorized.

The GitHub `interval-reminder.yml` remains a five-minute fallback heartbeat. Both
workers invoke the same due-cycle engine, whose Redis claim prevents duplicate
sends if they overlap. A minute-capable external schedule means a reminder is
normally picked up at the next minute tick after `due_at`, plus scheduler,
function startup, and network latency; it is not a real-time guarantee.

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

**What "every 30 minutes" honestly means.** The external schedule checks once
per minute, while the GitHub fallback checks every five minutes and can be
queued. Treat the interval as approximate, not as a real-time timer.

Periods are numbered from the Unix epoch, so a restart cannot open a fresh window
and re-send. `set_interval` clears the marker when you change the interval.

**Cost:** the external heartbeat invokes a function about 1,440 times a day; the
GitHub fallback runs about 288 times a day. Each invocation is short.

---

## Deploying the Connect page

The page is served by `reminder --serve` on any host with a real process
(Render, Fly.io, Railway, Cloud Run). It reads `PORT`, so no configuration is
needed for the port.

**Vercel is different.** Vercel runs a WSGI function once per request instead of
keeping a server alive, so `api/index.py` re-exposes the same `ConnectApp` behind
the WSGI interface. The routing, the secret check and the button-ownership check
are all still the ones in `reminder/webapp.py` -- the adapter only translates the
request. `vercel.json` points at it.

Two consequences of serverless, stated plainly:

* nonces live in memory and are lost on a cold start. The status handler already
  treats an unknown nonce as "check the store", so the Connect flow degrades to
  "you look connected" instead of breaking.
* `binding.json` and `settings.json` are per-invocation unless a durable store is
  configured. Set Upstash -- which interval and nudge mode require anyway -- and
  the function is stateless and correct.

`tests/test_wsgi.py` starts a real WSGI server and calls every route, so a broken
deployment fails CI rather than showing a 404 page.

## Let it run by itself

Push the folder to a GitHub repository (private is fine), then:

`Settings` → `Secrets and variables` → `Actions`

| Name | Where |
| --- | --- |
| `INFISICAL_CLIENT_ID` | secret |
| `INFISICAL_CLIENT_SECRET` | secret |
| `INFISICAL_PROJECT_SLUG` | variable |

Those three belong to the machine identity, not to the bot -- see
[In GitHub Actions](#in-github-actions) for how to create it. Both workflows are
ready:

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
to your reports. The token lives in Infisical and reaches the process only as an environment variable — never in the code, and never in a GitHub Secret, which `test_h2` and `test_p3` enforce.

To switch it off, delete the workflows. To invalidate the token, send `/revoke` to
@BotFather.
