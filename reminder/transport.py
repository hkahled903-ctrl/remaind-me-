"""Transport. Owns the HTTP call to Telegram and nothing else.

Knows nothing about schedules or state. The api_base and sleep are injected so
failure paths can be driven deterministically against a real local server.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_API_BASE = "https://api.telegram.org/bot{token}/{method}"
MAX_ATTEMPTS = 3
TIMEOUT_SECONDS = 20


class Telegram:
    def __init__(self, token: str, api_base: str = DEFAULT_API_BASE, sleep=time.sleep):
        self._token = token
        self._api_base = api_base
        self._sleep = sleep

    def call(self, method: str, payload: dict | None = None) -> dict:
        """Call a Bot API method, retrying transport errors, 429 and 5xx.

        A 4xx other than 429 is our own fault -- a bad token or chat id -- so it
        fails immediately rather than burning the retry budget.
        """
        form = {
            key: json.dumps(value) if isinstance(value, (dict, list)) else value
            for key, value in (payload or {}).items()
        }
        body = urllib.parse.urlencode(form).encode()
        last_error = "unknown"
        for attempt in range(MAX_ATTEMPTS):
            url = self._api_base.format(token=self._token, method=method)
            try:
                request = urllib.request.Request(url, data=body)
                with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                    return json.loads(response.read().decode())
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode(errors="replace")[:200]
                exc.close()  # otherwise the socket waits for the garbage collector
                last_error = f"HTTP {exc.code}: {detail}"
                if exc.code != 429 and exc.code < 500:
                    raise RuntimeError(f"Telegram rejected the request: {last_error}")
            except Exception as exc:  # network, DNS, timeout
                last_error = repr(exc)
            if attempt < MAX_ATTEMPTS - 1:
                self._sleep(2**attempt)  # no point waiting after the last try
        raise RuntimeError(f"Telegram failed after {MAX_ATTEMPTS} attempts: {last_error}")

    def send_message(self, chat_id: str, text: str, reply_markup: dict | None = None) -> dict:
        """Send a message, optionally with inline buttons.

        `reply_markup` is passed through as JSON because Telegram expects the
        keyboard encoded in the form field, not as nested form data.
        """
        payload = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = json.dumps(reply_markup)
        return self.call("sendMessage", payload)

    def answer_callback(self, callback_id: str, text: str = "", alert: bool = False) -> None:
        """Close the spinner on the button the user pressed.

        Telegram leaves a clock spinning on the chat until this is called, so a
        button that does nothing visible feels broken even when it worked.
        """
        payload = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text
            payload["show_alert"] = "true" if alert else "false"
        self.call("answerCallbackQuery", payload)
