"""Page. The Connect screen, as one string of HTML, CSS and JavaScript.

Split out of `webapp` so the routing module is about routing. Nothing here has
behaviour that the tests need to reach: the browser owns it, and the server only
ever hands the whole document over unchanged.

Kept in Python rather than a static file so the project stays one importable
package with no asset pipeline and no packaging step.

Depends on nothing.
"""

from __future__ import annotations

PAGE = """<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#E9EBE5">
<title>Daily report reminder</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  /* Instrument palette: enamel ground, printed ink, one alarm accent. */
  :root {
    color-scheme: light;
    --ground:   #E9EBE5;
    --face:     #F6F7F3;
    --ink:      #141A1C;
    --ink-soft: #5E655F;
    --rule:     #C8CCC2;
    --flame:    #BE3B26;

    --sans: "Instrument Sans", ui-sans-serif, system-ui, "Segoe UI", Helvetica, sans-serif;
    --measure: 34ch;
  }

  * { box-sizing: border-box; }

  html, body { height: 100%; }

  body {
    margin: 0;
    background: var(--ground);
    color: var(--ink);
    font-family: var(--sans);
    font-size: 1rem;
    line-height: 1.55;
    font-variant-numeric: tabular-nums;
    -webkit-font-smoothing: antialiased;
    overflow-x: hidden;
    -webkit-tap-highlight-color: rgba(190, 59, 38, .12);
  }

  .page {
    min-height: 100%;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: clamp(1.5rem, 5vh, 2.75rem);
    padding:
      max(clamp(1.5rem, 5vh, 3rem), env(safe-area-inset-top))
      max(1.25rem, env(safe-area-inset-right))
      0
      max(1.25rem, env(safe-area-inset-left));
  }

  .sr-only {
    position: absolute; width: 1px; height: 1px;
    padding: 0; margin: -1px; overflow: hidden;
    clip: rect(0 0 0 0); clip-path: inset(50%); white-space: nowrap; border: 0;
  }

  /* ---- the figure ------------------------------------------------------- */

  .figure {
    text-align: center;
    width: 100%;
    max-width: var(--measure);
    margin-top: clamp(1rem, 6vh, 3rem);
  }

  .countdown {
    /* One number, one job. Tight tracking and a heavy weight so it reads as a
       figure rather than as a headline. */
    font-size: clamp(4rem, 22vw, 8.5rem);
    font-weight: 600;
    letter-spacing: -0.045em;
    line-height: 0.95;
    margin: 0;
    font-variant-numeric: tabular-nums;
  }

  .caption {
    margin: .5rem 0 0;
    color: var(--ink-soft);
    font-size: .9375rem;
  }

  .caption.is-error { color: var(--flame); }

  .precision {
    margin: .35rem 0 0;
    color: var(--ink-soft);
    font-size: .8125rem;
  }

  /* ---- the segmented control -------------------------------------------- */

  .pick {
    width: 100%;
    max-width: 26rem;
    margin: 0;
    padding: 0;
    border: 1px solid var(--rule);
    border-radius: 10px;
    background: var(--face);
    display: flex;
    overflow: hidden;
  }

  .opt {
    flex: 1 1 0;
    min-width: 0;
    position: relative;
    display: grid;
    place-items: center;
    padding: .7rem .25rem;
    cursor: pointer;
    touch-action: manipulation;
    transition: background-color .15s ease;
  }

  /* Hairline dividers between stops, not gaps: these are marks on one scale. */
  .opt + .opt { border-left: 1px solid var(--rule); }
  .opt:hover { background: rgba(20, 26, 28, .045); }

  /* The control stays in the accessibility tree and in the tab order; only its
     box is hidden. */
  .opt input {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    margin: 0;
    opacity: 0;
    cursor: pointer;
  }

  .opt span {
    font-size: .875rem;
    font-weight: 500;
    color: var(--ink-soft);
    padding-bottom: 2px;
    border-bottom: 2px solid transparent;
    transition: color .15s ease, border-color .15s ease;
    text-align: center;
  }

  /* Selection is an underline on the scale, not a filled pill. */
  .opt input:checked + span { color: var(--ink); border-bottom-color: var(--ink); }
  .opt:has(input:checked) { background: var(--face); }

  .opt input:focus-visible + span {
    outline: 2px solid var(--flame);
    outline-offset: 4px;
    border-radius: 3px;
  }

  .opt input:focus-visible ~ * { pointer-events: none; }

  /* ---- actions ---------------------------------------------------------- */

  button {
    font: inherit;
    font-weight: 500;
    cursor: pointer;
    touch-action: manipulation;
    transition: background-color .15s ease, color .15s ease, opacity .15s ease;
  }

  .connect {
    background: var(--ink);
    color: var(--face);
    border: 1px solid var(--ink);
    border-radius: 999px;
    padding: .75rem 1.5rem;
    letter-spacing: -0.005em;
  }
  .connect:hover { background: #000; }

  .quiet {
    background: none;
    border: 1px solid transparent;
    color: var(--ink-soft);
    padding: .2rem .35rem;
    border-radius: 6px;
    text-decoration: underline;
    text-underline-offset: 3px;
    text-decoration-color: var(--rule);
  }
  .quiet:hover { color: var(--ink); text-decoration-color: var(--ink); }

  :focus-visible {
    outline: 2px solid var(--flame);
    outline-offset: 3px;
    border-radius: 4px;
  }

  [hidden] { display: none !important; }

  .link-wrap { margin: 0.85rem 0 0; }
  .link-wrap[hidden] { display: none; }

  .telegram-link {
    display: inline-block;
    color: var(--ink);
    font-weight: 500;
    text-decoration: underline;
    text-underline-offset: 0.2em;
    overflow-wrap: anywhere;
  }

  .hint {
    margin: 0.5rem 0 0;
    color: var(--ink-soft);
    font-size: 0.9rem;
  }

  /* ---- text and footer -------------------------------------------------- */

  .reading {
    width: 100%;
    max-width: var(--measure);
  }

  .reading h1 {
    margin: 0 0 .5rem;
    font-size: clamp(1.375rem, 4vw, 1.625rem);
    font-weight: 600;
    letter-spacing: -0.021em;
    line-height: 1.2;
    text-wrap: balance;
  }

  .reading p { margin: 0; color: var(--ink-soft); max-width: 32ch; text-wrap: pretty; }

  .status {
    margin-top: 1rem;
    font-size: .9375rem;
    font-weight: 500;
    display: flex;
    align-items: baseline;
    gap: .6rem;
  }

  .status .bead {
    width: .5rem; height: .5rem; border-radius: 50%;
    background: var(--rule); flex: none;
    transform: translateY(-.0625rem);
  }

  [data-state="waiting"] .status .bead { background: var(--flame); }
  [data-state="connected"] .status .bead { background: var(--flame); }
  [data-state="error"] .status .bead { background: var(--flame); }
  [data-state="error"] .status { color: var(--flame); }

  .foot {
    margin-top: auto;
    width: 100%;
    max-width: 52rem;
    border-top: 1px solid var(--rule);
    padding: 1rem 0 1.25rem;
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 1rem;
    flex-wrap: wrap;
    font-size: .8125rem;
    color: var(--ink-soft);
  }

  .foot .who { color: var(--ink); font-weight: 500; }

  @media (prefers-reduced-motion: reduce) {
    * { animation: none !important; transition: none !important; }
  }

  @media (max-width: 30rem) {
    .page { gap: 1.75rem; }
    .foot { flex-direction: column; align-items: flex-start; gap: .5rem; }
    .opt { padding: .65rem .15rem; }
    .opt span { font-size: .8125rem; }
  }
</style>
</head>
<body>
<div class="page" id="page" data-state="idle">

  <div class="figure">
    <!-- The ticking digits are hidden from assistive tech: announcing every
         second would make the page unusable with a screen reader. The sentence
         below is the accessible equivalent and updates only when it must. -->
    <p class="countdown" id="countdown" aria-hidden="true">--:--</p>
    <p class="sr-only" id="countdown-sentence" aria-live="polite"></p>
    <p class="caption" id="caption">Loading…</p>
    <p class="precision" id="precision" hidden></p>
  </div>

  <form class="reading" id="reading" novalidate>
    <h1>Daily report reminder</h1>
    <p id="blurb">One message a day, in Telegram.</p>

    <div class="status">
      <span class="bead" aria-hidden="true"></span>
      <span id="state">Not connected yet.</span>
    </div>

    <div style="margin-top:1.5rem">
      <button id="connect" class="connect" type="button">Connect Telegram</button>
      <p class="link-wrap">
        <a id="telegram-link" class="telegram-link" href="#" hidden rel="noopener">Open the bot in Telegram</a>
      </p>
      <p class="hint">Then press <strong>Start</strong> in Telegram. This page updates itself.</p>
    </div>
  </form>

  <fieldset class="pick" id="pick">
    <legend class="sr-only">How often should the reminder repeat?</legend>
  </fieldset>

  <div class="foot">
    <span id="who" hidden></span>
    <span id="foot-note">Connect once, then you can close this page.</span>
    <span>
      <button id="test" class="quiet" type="button" hidden>Send a test reminder</button>
      <button id="reset" class="quiet" type="button" hidden>Unbind</button>
    </span>
  </div>
</div>

<script>
const $ = (id) => document.getElementById(id);
const page = $("page");
let nonce = null;
let timer = null;
let countdownTimer = null;

function setState(state, text, isError) {
  page.dataset.state = state;
  const el = $("state");
  el.textContent = text;
  el.classList.toggle("is-error", Boolean(isError));
}

/* ---- the countdown ------------------------------------------------------ */

function formatClock(total) {
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}

// "27:43" read alone is meaningless; these give it a unit and a subject.
function describe(total, interval) {
  const hours = Math.floor(total / 3600);
  const minutes = Math.ceil(total / 60);
  const amount = hours > 0
    ? `${hours} hour${hours === 1 ? "" : "s"}`
    : `${minutes} minute${minutes === 1 ? "" : "s"}`;
  const daily = !interval || Number(interval) === 0;
  const repeat = daily ? "once a day" : `every ${interval} minutes`;
  return `Next reminder in about ${amount}, ${repeat}.`;
}

function captionFor(interval, stopped) {
  if (stopped) return "Timer off";
  return !interval || Number(interval) === 0 ? "Until tomorrow morning" : "Until the next one";
}

// A stopped timer says so in words, not just by an empty countdown: an empty
// counter would read as a bug rather than as a choice the user made.
function describeStopped() {
  return "Timer is off. Pick a time to start the reminders again.";
}

function startCountdown(seconds, interval) {
  let left = Math.max(0, seconds);
  const tick = () => {
    $("countdown").textContent = formatClock(left);
    if (left > 0) left -= 1;
  };
  tick();
  clearInterval(countdownTimer);
  countdownTimer = setInterval(tick, 1000);
  if (!$("countdown-sentence").textContent) {
    $("countdown-sentence").textContent = describe(seconds, interval);
  }
}

/* ---- the interval control ---------------------------------------------- */

function buildPicker(options, selected) {
  const pick = $("pick");
  pick.querySelectorAll(".opt").forEach((n) => n.remove());
  for (const stop of options) {
    const label = document.createElement("label");
    label.className = "opt";

    const input = document.createElement("input");
    input.type = "radio";
    input.name = "interval";
    input.value = String(stop.value);
    input.checked = Number(stop.value) === Number(selected);

    const span = document.createElement("span");
    span.textContent = stop.label;

    label.append(input, span);
    pick.append(label);
  }
  pick.querySelectorAll("input").forEach((input) => {
    input.addEventListener("change", () => { if (input.checked) save(input.value); });
  });
}

async function save(value) {
  $("caption").textContent = "Saving…";
  const res = await fetch("/connect/interval", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ minutes: value }),
  });
  const data = await res.json();
  if (!data.ok) {
    $("caption").textContent = data.error || "Could not change the interval.";
    $("caption").classList.add("is-error");
    return;
  }
  $("caption").classList.remove("is-error");
  $("caption").textContent = captionFor(data.interval_minutes);
  $("blurb").textContent = data.interval_minutes === 0
    ? "One message a day, in Telegram."
    : `One message every ${data.interval_minutes} minutes, in Telegram.`;
  // Only now, after a confirmed change, is it worth telling a screen reader.
  $("countdown-sentence").textContent = describe(data.seconds_until, data.interval_minutes);
  startCountdown(data.seconds_until, data.interval_minutes);
}

async function loadSchedule() {
  const res = await fetch("/connect/interval");
  const data = await res.json();
  if (data.error) {
    $("caption").textContent = data.error;
    $("caption").classList.add("is-error");
    return;
  }
  buildPicker(data.options, data.interval_minutes);
  $("blurb").textContent = data.interval_minutes === 0
    ? "One message a day, in Telegram."
    : `One message every ${data.interval_minutes} minutes, in Telegram.`;
  if (data.stopped) {
    $("caption").textContent = captionFor(data.interval_minutes, true);
    $("countdown").textContent = "--:--";
    $("precision").hidden = true;
    $("countdown-sentence").textContent = describeStopped();
    return;
  }
  $("caption").textContent = captionFor(data.interval_minutes, false);
  $("precision").hidden = data.interval_minutes === 0;
  startCountdown(data.seconds_until, data.interval_minutes);
}

// A page loaded after the connection already happened used to render "Not
// connected yet" forever: nothing checked the status until the button was pressed.
// Reloading after connecting is the most natural thing to do, so the state is
// restored on load. Without a nonce the server skips the freshness check and
// reports whatever binding exists.
async function restoreConnection() {
  try {
    const res = await fetch("/connect/status");
    if (!res.ok) return;
    const data = await res.json();
    if (data.connected) connected(data.label);
  } catch (err) {
    // A status check that fails is not a reason to hide a working page.
  }
}

loadSchedule();
restoreConnection();

/* ---- connect ----------------------------------------------------------- */

function connected(label) {
  setState("connected", "Reminders go to your Telegram chat.");
  $("who").hidden = false;
  $("who").textContent = label ? "Connected as @" + label : "Connected";
  $("foot-note").hidden = true;
  $("test").hidden = false;
  $("reset").hidden = false;
  $("connect").textContent = "Connect another";
}

async function poll() {
  const res = await fetch(`/connect/status?nonce=${encodeURIComponent(nonce)}`);
  const data = await res.json();
  if (data.connected) {
    clearInterval(timer);
    timer = null;
    connected(data.label);
  }
}

// The Telegram link must be a real anchor the person clicks.
//
// It used to open a popup after an `await fetch()`. By then the browser was no
// longer inside the click gesture, so the popup was blocked without a word and
// the page sat on "Waiting for Telegram" forever. An anchor with an href
// navigates for real, and showing the URL also means a blocked t.me handler is
// something you can see and copy instead of a mystery.
$("connect").onclick = async () => {
  $("connect").disabled = true;
  setState("waiting", "Waiting for Telegram.");
  $("foot-note").textContent = "Press Start in Telegram. This page updates itself.";
  const res = await fetch("/connect/start");
  const data = await res.json();
  if (data.error) {
    $("connect").disabled = false;
    setState("error", data.error);
    return;
  }
  nonce = data.nonce;
  const link = $("telegram-link");
  link.href = data.deep_link;
  link.textContent = data.deep_link;
  link.hidden = false;
  link.focus();
  clearInterval(timer);
  timer = setInterval(poll, 2000);
};

$("test").onclick = async () => {
  $("test").disabled = true;
  const res = await fetch("/connect/test", { method: "POST" });
  const data = await res.json();
  setState(
    page.dataset.state,
    data.ok ? "Test reminder sent." : data.error || "Could not send the test reminder.",
    !data.ok
  );
  $("test").disabled = false;
};

$("reset").onclick = async () => {
  await fetch("/connect/reset", { method: "POST" });
  location.reload();
};
</script>
</body>
</html>
"""
