"""Daily report reminder: sends a Telegram message at a fixed time each day.

Modules, one owner each. Dependencies only ever point downward; nothing lower
imports anything higher.

    logs   <-- config  <-- policy  <-- settings <-- commands <-- cli
                    \\         \\             \\          ^   ^
    binding ---------\\---------\-------------/----------+   |
         (chat store)  \\_______________/                   |
    transport ------------------------------------------ +--+   __main__ -> cli
    page          (the HTML; imported by webapp, nothing else)

Read it as a stack, not a graph:

  logs        the only leaf. Everything may write to it, nothing is imported by
              it, so a log call can never start a cycle.
  config      where files live and what a valid setting looks like. Reads no
              clock, calls no network.
  policy      pure answers to "is it time?". Takes the moment as an argument, so
              every boundary is testable without freezing a clock.
  binding     the connected chat. One record, two backends: a file or Upstash.
  settings    the repeat interval and which period was served. Same two backends,
              because the durability requirement is identical.
  transport   the single HTTP call to Telegram. Knows nothing of schedules.
  page        the Connect screen, as data. No logic worth testing.
  webapp      routing and nonce bookkeeping over the two stores.
  commands    one function per mode, composed from everything above.
  cli         argument parsing and exit codes. The only module that reads argv.

Two rules this layout exists to keep:

  * nothing decides *when* to send except `policy`, and it never reads a clock;
  * anything that must survive a redeploy (a chat id, a served marker) goes
    through `binding`/`settings` rather than into `config.json`.
"""

__version__ = "1.0.0"