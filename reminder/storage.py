"""Upstash Redis storage and atomic timer transitions."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

TIMERS_KEY = "timers:due"

START_SCRIPT = """
if redis.call('GET', KEYS[3]) then return -1 end
local state = redis.call('HGET', KEYS[1], 'state') or 'IDLE'
if state ~= 'IDLE' then return 0 end
local claimed = redis.call('SET', KEYS[3], '1', 'NX', 'EX', ARGV[6])
if not claimed then return -1 end
redis.call('HSET', KEYS[1],
  'chat_id', ARGV[1], 'state', 'RUNNING',
  'duration_seconds', ARGV[2], 'started_at', ARGV[3], 'expires_at', ARGV[4],
  'next_reminder_at', '', 'timer_id', ARGV[5], 'confirmation_id', '',
  'completed_at', '', 'pending_delivery_kind', '', 'pending_delivery_id', '',
  'delivery_token', '', 'delivery_lease_until', '0')
redis.call('ZADD', KEYS[2], ARGV[4], ARGV[1])
return 1
"""

CLAIM_SCRIPT = """
local chat = ARGV[1]
local now = tonumber(ARGV[2])
local delivery_id = ARGV[3]
local confirmation_id = ARGV[4]
local token = ARGV[5]
local lease_until = ARGV[6]
local score = redis.call('ZSCORE', KEYS[2], chat)
if not score or tonumber(score) > now then return {} end
local state = redis.call('HGET', KEYS[1], 'state') or 'IDLE'
if state == 'IDLE' then
  redis.call('ZREM', KEYS[2], chat)
  return {}
end
local old_token = redis.call('HGET', KEYS[1], 'delivery_token') or ''
local old_lease = tonumber(redis.call('HGET', KEYS[1], 'delivery_lease_until') or '0')
if old_token ~= '' and old_lease > now then
  redis.call('ZADD', KEYS[2], old_lease, chat)
  return {}
end
local kind = redis.call('HGET', KEYS[1], 'pending_delivery_kind') or ''
local current_delivery_id = redis.call('HGET', KEYS[1], 'pending_delivery_id') or ''
local current_confirmation_id = redis.call('HGET', KEYS[1], 'confirmation_id') or ''
if current_delivery_id ~= '' then
  if state ~= 'WAITING_CONFIRMATION' then return {} end
  kind = kind ~= '' and kind or 'expiration'
  delivery_id = current_delivery_id
  confirmation_id = current_confirmation_id
else
  if state == 'RUNNING' then
    local expires_at = tonumber(redis.call('HGET', KEYS[1], 'expires_at') or '0')
    if expires_at > now then return {} end
    state = 'WAITING_CONFIRMATION'
    kind = 'expiration'
    redis.call('HSET', KEYS[1], 'state', state, 'confirmation_id', confirmation_id,
      'next_reminder_at', '')
  elseif state == 'WAITING_CONFIRMATION' then
    local next_reminder_at = tonumber(redis.call('HGET', KEYS[1], 'next_reminder_at') or '0')
    if next_reminder_at <= 0 or next_reminder_at > now then return {} end
    kind = 'reminder'
  else
    return {}
  end
  current_confirmation_id = confirmation_id
  current_delivery_id = delivery_id
  redis.call('HSET', KEYS[1], 'pending_delivery_kind', kind,
    'pending_delivery_id', delivery_id)
end
redis.call('HSET', KEYS[1], 'delivery_token', token, 'delivery_lease_until', lease_until)
redis.call('ZADD', KEYS[2], lease_until, chat)
return {'claimed', kind,
  redis.call('HGET', KEYS[1], 'timer_id') or '',
  current_confirmation_id, current_delivery_id,
  redis.call('HGET', KEYS[1], 'duration_seconds') or '0'}
"""

FINISH_SCRIPT = """
if redis.call('HGET', KEYS[1], 'state') ~= 'WAITING_CONFIRMATION'
  or redis.call('HGET', KEYS[1], 'timer_id') ~= ARGV[1]
  or redis.call('HGET', KEYS[1], 'confirmation_id') ~= ARGV[2]
  or redis.call('HGET', KEYS[1], 'pending_delivery_id') ~= ARGV[3]
  or redis.call('HGET', KEYS[1], 'delivery_token') ~= ARGV[4] then
  return 0
end
local next_at = tonumber(ARGV[5]) + tonumber(ARGV[6])
redis.call('HSET', KEYS[1], 'next_reminder_at', next_at,
  'pending_delivery_kind', '', 'pending_delivery_id', '',
  'delivery_token', '', 'delivery_lease_until', '0')
redis.call('ZADD', KEYS[2], next_at, ARGV[7])
return 1
"""

RELEASE_SCRIPT = """
if redis.call('HGET', KEYS[1], 'timer_id') ~= ARGV[1]
  or redis.call('HGET', KEYS[1], 'pending_delivery_id') ~= ARGV[2]
  or redis.call('HGET', KEYS[1], 'delivery_token') ~= ARGV[3] then
  return 0
end
local retry_at = tonumber(ARGV[4])
redis.call('HSET', KEYS[1], 'delivery_token', '', 'delivery_lease_until', '0')
redis.call('ZADD', KEYS[2], retry_at, ARGV[5])
return 1
"""

CALLBACK_SCRIPT = """
if redis.call('HGET', KEYS[1], 'state') ~= 'WAITING_CONFIRMATION'
  or redis.call('HGET', KEYS[1], 'timer_id') ~= ARGV[1]
  or redis.call('HGET', KEYS[1], 'confirmation_id') ~= ARGV[2] then
  return 'stale'
end
local lease_until = tonumber(redis.call('HGET', KEYS[1], 'delivery_lease_until') or '0')
if (redis.call('HGET', KEYS[1], 'delivery_token') or '') ~= ''
  and lease_until > tonumber(ARGV[3]) then
  return 'busy'
end
if ARGV[4] == 'yes' then
  redis.call('HSET', KEYS[1], 'state', 'IDLE', 'next_reminder_at', '',
    'confirmation_id', '', 'completed_at', ARGV[3],
    'pending_delivery_kind', '', 'pending_delivery_id', '',
    'delivery_token', '', 'delivery_lease_until', '0')
  redis.call('ZREM', KEYS[2], ARGV[5])
  return 'completed'
end
if ARGV[4] == 'more' then
  local duration = tonumber(redis.call('HGET', KEYS[1], 'duration_seconds') or '0')
  local expires_at = tonumber(ARGV[3]) + duration * 1000
  redis.call('HSET', KEYS[1], 'state', 'RUNNING', 'timer_id', ARGV[6],
    'started_at', ARGV[3], 'expires_at', expires_at,
    'next_reminder_at', '', 'confirmation_id', '', 'completed_at', '',
    'pending_delivery_kind', '', 'pending_delivery_id', '',
    'delivery_token', '', 'delivery_lease_until', '0')
  redis.call('ZADD', KEYS[2], expires_at, ARGV[5])
  return 'restarted'
end
return 'invalid'
"""


class RedisError(RuntimeError):
    """Redis is unavailable or rejected a storage operation."""


class TimerStore:
    """Small direct Upstash REST client; Redis is the sole production store."""

    def __init__(self, url: str, token: str):
        if not url.strip() or not token.strip():
            raise RedisError(
                "UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN are required"
            )
        self._url = url.rstrip("/")
        self._token = token.strip()

    @staticmethod
    def key(chat_id: str) -> str:
        return f"timer:{chat_id}"

    def command(self, *args: str):
        request = urllib.request.Request(
            self._url,
            data=json.dumps(list(args)).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RedisError(f"Redis returned HTTP {exc.code}") from None
        except (OSError, ValueError) as exc:
            raise RedisError(f"Redis request failed: {exc}") from None
        if not isinstance(payload, dict):
            raise RedisError("Redis returned an invalid response")
        if payload.get("error"):
            raise RedisError(f"Redis rejected the command: {payload['error']}")
        return payload.get("result")

    def _eval(self, script: str, keys: tuple[str, ...], args: tuple[object, ...]):
        command = ("EVAL", script, str(len(keys)), *keys, *(str(arg) for arg in args))
        try:
            return self.command(*command)
        except RedisError:
            raise
        except Exception as exc:
            raise RedisError(f"Redis atomic operation failed: {exc}") from exc

    def start(
        self,
        chat_id: str,
        duration_seconds: int,
        now_ms: int,
        timer_id: str,
        update_id: str = "",
    ) -> int:
        import secrets

        dedupe_id = update_id or secrets.token_urlsafe(16)
        result = self._eval(
            START_SCRIPT,
            (self.key(chat_id), TIMERS_KEY, f"telegram:update:{dedupe_id}"),
            (
                chat_id, duration_seconds, now_ms,
                now_ms + duration_seconds * 1000, timer_id, 604800,
            ),
        )
        return int(result)

    def get(self, chat_id: str) -> dict[str, str] | None:
        result = self.command("HGETALL", self.key(chat_id))
        if not result:
            return None
        if isinstance(result, dict):
            return {str(key): str(value) for key, value in result.items()}
        if isinstance(result, list) and len(result) % 2 == 0:
            return {str(result[i]): str(result[i + 1]) for i in range(0, len(result), 2)}
        raise RedisError("Redis returned malformed timer data")

    def due_chats(self, now_ms: int, limit: int = 100) -> list[str]:
        result = self.command(
            "ZRANGEBYSCORE", TIMERS_KEY, "-inf", str(now_ms), "LIMIT", "0", str(limit)
        )
        if not isinstance(result, list):
            raise RedisError("Redis returned malformed due-timer data")
        return [str(chat_id) for chat_id in result]

    def claim_due(
        self,
        chat_id: str,
        now_ms: int,
        delivery_id: str,
        confirmation_id: str,
        token: str,
        lease_until_ms: int,
    ) -> dict[str, str] | None:
        result = self._eval(
            CLAIM_SCRIPT,
            (self.key(chat_id), TIMERS_KEY),
            (chat_id, now_ms, delivery_id, confirmation_id, token, lease_until_ms),
        )
        if not result or result[0] != "claimed":
            return None
        return {
            "kind": str(result[1]),
            "timer_id": str(result[2]),
            "confirmation_id": str(result[3]),
            "delivery_id": str(result[4]),
            "duration_seconds": str(result[5]),
            "token": token,
        }

    def finish_delivery(
        self,
        chat_id: str,
        claim: dict[str, str],
        now_ms: int,
        reminder_delay_ms: int,
    ) -> bool:
        return self._eval(
            FINISH_SCRIPT,
            (self.key(chat_id), TIMERS_KEY),
            (
                claim["timer_id"], claim["confirmation_id"], claim["delivery_id"],
                claim["token"], now_ms, reminder_delay_ms, chat_id,
            ),
        ) == 1

    def release_delivery(
        self, chat_id: str, claim: dict[str, str], retry_at_ms: int
    ) -> bool:
        return self._eval(
            RELEASE_SCRIPT,
            (self.key(chat_id), TIMERS_KEY),
            (
                claim["timer_id"], claim["delivery_id"], claim["token"],
                retry_at_ms, chat_id,
            ),
        ) == 1

    def callback(
        self,
        chat_id: str,
        action: str,
        timer_id: str,
        confirmation_id: str,
        now_ms: int,
        new_timer_id: str,
    ) -> str:
        result = self._eval(
            CALLBACK_SCRIPT,
            (self.key(chat_id), TIMERS_KEY),
            (timer_id, confirmation_id, now_ms, action, chat_id, new_timer_id),
        )
        return str(result)
