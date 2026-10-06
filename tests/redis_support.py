"""Standard-library RESP client for isolated Redis integration tests."""

from __future__ import annotations

import os
import socket
from urllib.parse import urlparse

from reminder.storage import TimerStore


class NativeRedisTimerStore(TimerStore):
    def __init__(self, url: str):
        parsed = urlparse(url)
        if parsed.scheme != "redis" or not parsed.hostname:
            raise ValueError("TIMER_TEST_REDIS_URL must be a redis:// URL")
        self.host = parsed.hostname
        self.port = parsed.port or 6379
        self.database = int((parsed.path or "/0").lstrip("/") or "0")
        self.password = parsed.password
        super().__init__("http://test.invalid", "test-only")

    def _command_bytes(self, sock: socket.socket, stream, args: tuple[str, ...]):
        encoded = [str(arg).encode("utf-8") for arg in args]
        sock.sendall(
            b"*" + str(len(encoded)).encode() + b"\r\n"
            + b"".join(
                b"$" + str(len(value)).encode() + b"\r\n" + value + b"\r\n"
                for value in encoded
            )
        )
        return self._read(stream)

    def _read(self, stream):
        prefix = stream.read(1)
        line = stream.readline()
        if prefix == b"+":
            return line[:-2].decode("utf-8")
        if prefix == b"-":
            raise RuntimeError(line[:-2].decode("utf-8", "replace"))
        if prefix == b":":
            return int(line[:-2])
        if prefix == b"$":
            length = int(line[:-2])
            if length == -1:
                return None
            value = stream.read(length)
            stream.read(2)
            return value.decode("utf-8")
        if prefix == b"*":
            count = int(line[:-2])
            return None if count == -1 else [self._read(stream) for _ in range(count)]
        raise RuntimeError("invalid RESP response")

    def command(self, *args: str):
        try:
            with socket.create_connection((self.host, self.port), timeout=3) as sock:
                stream = sock.makefile("rb")
                if self.password:
                    self._command_bytes(sock, stream, ("AUTH", self.password))
                self._command_bytes(sock, stream, ("SELECT", str(self.database)))
                result = self._command_bytes(sock, stream, args)
                if isinstance(result, str) and result.startswith("ERR "):
                    raise RuntimeError(result)
                return result
        except OSError as exc:
            raise RuntimeError(f"test Redis unavailable: {exc}") from exc


def test_store() -> NativeRedisTimerStore | None:
    url = os.environ.get("TIMER_TEST_REDIS_URL", "").strip()
    if not url:
        return None
    return NativeRedisTimerStore(url)
