# Roadmap — daily report reminder

Where this project is going, and the evidence behind each decision.

Every claim is either **measured** (a scenario test that passes or fails today) or
**assumed** (labelled as such). Run the suite yourself:

```bash
python -m unittest discover -s tests -t . -v
```

---

## Where we are

One reminder a day, from a GitHub Actions cron, to one Telegram chat. Four config
keys, no report storage, standard library only.

## Structure

One owner per concern, as separate modules so each is understandable on its own.
Dependencies only ever point downward, so the graph is acyclic:

```
logs ──> config ──> policy
                   │
state ─────────────┼──> commands ──> cli ──> __main__
                   │
transport ─────────┘
```

| Module | Owns | Never does |
| --- | --- | --- |
| `logs` | how a line reaches a human | anything else — it is the root |
| `config` | settings, credentials, file locations | decide when to send |
| `policy` | the only answer to "is it time?" | touch the network or disk |
| `transport` | the HTTP call and its retry policy | know about schedules |
| `state` | what was already sent today | decide when to send |
| `commands` | one function per mode | re-decide policy |
| `cli` | argument parsing and exit codes | hold business rules |

Three consequences that were real defects before the split:

- **`policy` has exactly one owner.** The window logic used to be duplicated
  between the loop and the scheduled branch, so the two could drift.
- **`cli` owns exit codes.** `parse_hhmm` used to call `sys.exit` from inside
  logic; it now raises `ConfigError`. That is what makes every mode callable from a
  test without spawning a process.
- **A typo cannot start the endless mode.** Unknown flags are rejected with exit 2.
  Previously an unknown flag fell through to the bare "run forever" path, which
  looks exactly like the tool going quiet. Guarded by `test_s24b`.

## Coverage

52 tests, split by what they protect:

| Suite | Count | Protects |
| --- | --- | --- |
| `tests/test_scenarios.py` | 31 | Runtime behaviour, over real HTTP and real subprocesses |
| `tests/test_pipeline.py` | 21 | The GitHub Actions setup itself |

Telegram is faked with a **real local HTTP server**, so status codes, retry timing
and request bodies cross an actual socket. The CLI tests copy the project to a temp
directory and spawn `python -m reminder`, asserting real exit codes — the closest
thing to what you actually run.

---

## Measured gaps

Reproduced, not hypothesised. The test name is the proof.

### G1 — A clock change can silently delete a day `test_s4b`

The window opens at `reminder_time` and closes 2h later. If Cairo becomes **UTC+3**
(DST), the fixed `0 7 UTC` cron fires at 10:00 local — absorbed, the reminder still
arrives. If Cairo becomes **UTC+1**, the same cron fires at 08:00 local, *before* the
window opens, and the day is skipped: no message, no error, no state change.

The asymmetry is the whole problem. **A late shift is noisy; an early shift is
silent.**

### G2 — CI has no memory, so a re-run double-sends `test_s3`

`state.json` works on a host that keeps it (`test_s2`). A GitHub runner is ephemeral:
the file is written and discarded. Re-running a failed job, or pressing **Run
workflow**, sends a second identical reminder.

### G3 — A revoked token is loud on paper, invisible in practice `test_s9`

A 401 correctly fails fast with no pointless retries. But it fails into a log nobody
opens. The reminder dies and the only symptom is a red X you have to go looking for.

### G4 / G5 — structural limits

- One fixed time. "Daily standup at 09:00 *and* a client report on the 1st" cannot be
  expressed.
- One fixed message. "Don't remind me today" cannot be expressed — there is no input
  channel, only a file.

---

## Phases

### P0 — Structure, scenarios and a pipeline that cannot rot ✅ done

Split into modules with one owner each, collapsed the duplicated window policy, moved
exit codes to the CLI. Replaced smoke checks with 52 scenario tests. Hardened both
workflows and added `tests/test_pipeline.py` to hold the hardening in place.

**Exit criteria, met:** every scenario runs in ~19s with no network, no credentials
and no local server; the suite is the pipeline's own regression gate.

### P1 — Never lose a day (fixes G1)

Open the window on **both** sides of the target: start sending `GRACE` before
`reminder_time`, close `WINDOW` after. Pair it with an hourly cron so there are 24
chances to land instead of one.

Cheap and correct — but only while at most one send lands per day, which is P2.

- **Change:** `should_send` becomes a two-sided window; `cron` becomes `0 * * * *`.
- **Exit:** `test_s4b` flips to asserting a send; a new test proves an 08:00 fire and
  a 10:00 fire both deliver.
- **Risk:** hourly cron on memoryless state spams you. Ship with P2 or gate the
  hourly cron behind the durable marker.

### P2 — Durable idempotency (fixes G2, unlocks P1)

The marker has to outlive one runner.

**Decision required — not made for you.** Candidates and what each really costs:

| Option | Cost | Honest drawback |
| --- | --- | --- |
| Free KV (Upstash Redis etc.) | New account, new secret, ~50 lines | A second thing that can be down |
| Commit `state.json` back | No new service | Dirty commits on every send; needs `contents: write`, which contradicts the least-privilege posture just established |
| GitHub Actions cache | No new service | Eviction is not guaranteed; unsafe as a correctness store |
| Accept at-most-once, drop the hourly cron | Nothing | G1 stays open |

**Exit:** `test_s3` flips to delivering exactly one message from two independent state
paths. It is written to be flipped, not rewritten.

### P3 — Several schedules (fixes G4)

`reminder_time: "09:00"` becomes a list, with the scalar form still accepted.

**Real driver:** a daily standup plus a monthly client report on the 1st.

**Exit:** both schedules coexist; one pass evaluates each; the legacy scalar still
works untouched.

### P4 — Be able to talk to it (fixes G5)

`/remind`, `/snooze 2h`, `/status`, `/set 08:00`.

**State this honestly: P4 breaks the current deployment model.** Answering commands
needs long-polling, which needs a process that stays up. The Actions cron cannot do
it, so P4 forces a choice — a host that stays up, or a webhook endpoint plus a
trigger. Neither is a five-minute change, and neither is a laptop.

**Do not start P4 before P2.** Snooze without durable state is meaningless.

### P5 — Make failure visible (fixes G3)

GitHub already emails repository watchers on failure; make sure it reaches you. Then
a second chat that only receives "reminder at 09:00 failed: HTTP 401".

**The honest limit:** when Telegram is the thing that is down, a Telegram fallback
cannot work. A genuinely independent channel is a decision, not an implementation.

**Exit:** revoking the token produces a notification you receive without opening a
browser.

---

## Ordering

```
P0 done ─┬─> P2 (durable state) ──┬─> P1 (two-sided window + hourly cron)
         │                         ├─> P3 (multiple schedules)
         │                         └─> P4 (commands; needs a long-lived host)
         └─> P5 (failure visibility)      ← independent, cheap, start any time
```

P1 and P2 ship together, in that order. P5 is independent. P3 is the next real
feature. P4 is a commitment, not a task.

## Explicitly not doing

- **Storing reports.** You asked for a reminder. A database and an archive are a
  different product.
- **A multi-user model.** One chat, one person. Sharing means a config change, not
  an auth system.
- **Abstracting transport behind a provider interface.** There is one provider.
- **A Python version matrix in CI.** One pinned version is more predictable, and the
  schedule matters more than breadth. Revisit if you ever publish this.
