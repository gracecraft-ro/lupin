"""Slot leases backed by Redis: the `redis` backend (issue #210, part of
#198's plan). See `docs/redis-schema.md` for the key shapes, the two Lua
scripts, TTLs, and the fallback rules this module implements -- that doc is
the spec, this module is just that spec in code.

Same shape as `slots.py`'s `local` backend (`acquire`, `renew`, `release`,
`status`, `hold`), same lease-id convention (`"<slot>:<token>"`, split with
`_lease_runtime.split_lease`), same `SlotFull`/`CoordinatorUnreachable`
exceptions (imported from `slots.py`, not redefined -- one exit-code
contract, see `cli.py`). `hold`'s subprocess/renew-timer plumbing is the
same shared `_lease_runtime.run_with_lease` the `local` backend uses.

Key difference from `local`: the sorted set's member *is* the holder's name,
not a random token (`docs/redis-schema.md`'s acquire script renews in place
when "the caller already holds the slot"). So a lease id here is
`"<slot>:<holder>"` -- there is no secret token, by design: the schema's
trust boundary is the tailnet plus a Redis ACL (#198 section 4), not a
guessable lease id.

Judgment call -- where the slot's `max` lives: the schema's key table does
not list one. Reusing the `local` backend's own judgment call (a slot's
config is read once and fixed at creation), this module stores it in a
plain string key, `lupin:v1:slot:<name>:max`, set with `SET ... NX` on the
first `acquire`/`hold` call and read on every later one. `GET`/`SET` are
already on the ACL command list in `docs/redis-schema.md`, so this adds no
new permission, just a second use of an already-allowed command pair.

A dashboard or operator can still change that number later, with
`set_max()` -- a plain `SET`, no `NX`. It does not touch `slot:<name>`
itself, so a holder already past the new, lower max keeps its lease; only
a later `acquire` sees the new number and can be turned away by it.

Fallback: only the `bmo` slot falls back to the `local` backend when Redis
is unreachable (`docs/redis-schema.md`'s fallback table; a connect timeout
counts as unreachable too). Every other slot name raises
`CoordinatorUnreachable` instead -- the schema does not describe fallback
for a hypothetical second fleet slot, v1 only has `bmo`, so this module does
not invent a rule for a slot that does not exist yet.

Redis can refuse a login, or refuse one command.
redis-py raises `AuthenticationError` for a refused login.
redis-py raises `NoPermissionError` for a command that the ACL denies.
`AuthenticationError` is a subclass of `ConnectionError`. This module checks for a refusal first.
`docs/redis-schema.md` lists the result of each call.
`CoordinatorAuthFailed` is a subclass of `CoordinatorUnreachable`.
Existing handlers still catch it.

Claims and ledger streams are fleet keys. `claims.py` and `ledger.py`
implement them separately. Neither uses a local fallback when Redis is
unreachable.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import redis

from . import slots as local_slots
from ._lease_runtime import run_with_lease, split_lease

SlotFull = local_slots.SlotFull
CoordinatorUnreachable = local_slots.CoordinatorUnreachable


class CoordinatorAuthFailed(CoordinatorUnreachable):
    """Redis answered, but refused the login or one command.

    `password_refused` is True for a bad or missing password.
    It is False for an ACL denial.
    The message never contains the password.
    """

    def __init__(self, message: str, *, password_refused: bool) -> None:
        super().__init__(message)
        self.password_refused = password_refused

    def for_user(self, setting: str) -> str:
        """Return the message for a user. For a bad password, it names `setting`."""
        if not self.password_refused:
            return f"{self}."
        return f"{self}. Set {setting}."


_AUTH_ERRORS = (redis.exceptions.AuthenticationError, redis.exceptions.NoPermissionError)

PREFIX = "lupin:v1:"
FALLBACK_SLOTS = {"bmo"}

CONNECT_TIMEOUT = 2.0

# KEYS[1] = slot:<name>, ARGV[1] = holder, ARGV[2] = now_ms, ARGV[3] = ttl_ms,
# ARGV[4] = max holders. Returns 1 (added or renewed) or 0 (busy).
_ACQUIRE_SCRIPT = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[2])
local expiry = tonumber(ARGV[2]) + tonumber(ARGV[3])
if redis.call('ZSCORE', KEYS[1], ARGV[1]) then
    redis.call('ZADD', KEYS[1], expiry, ARGV[1])
    return 1
end
if redis.call('ZCARD', KEYS[1]) < tonumber(ARGV[4]) then
    redis.call('ZADD', KEYS[1], expiry, ARGV[1])
    return 1
end
return 0
"""

# KEYS[1] = slot:<name>, ARGV[1] = holder, ARGV[2] = now_ms, ARGV[3] = ttl_ms.
# Only pushes the deadline out if the holder is still live -- unlike
# acquire, never takes a new spot. Returns 1 (renewed) or 0 (lease is gone).
_RENEW_SCRIPT = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[2])
if redis.call('ZSCORE', KEYS[1], ARGV[1]) then
    redis.call('ZADD', KEYS[1], tonumber(ARGV[2]) + tonumber(ARGV[3]), ARGV[1])
    return 1
end
return 0
"""

# KEYS[1] = slot:<name>, ARGV[1] = holder. Compare-and-delete: only removes
# the caller's own entry. Returns 1 (released) or 0 (already gone).
_RELEASE_SCRIPT = """
if redis.call('ZSCORE', KEYS[1], ARGV[1]) then
    redis.call('ZREM', KEYS[1], ARGV[1])
    return 1
end
return 0
"""


def _client(
    redis_host: str | None,
    redis_port: int | None,
    redis_username: str | None = None,
    redis_password: str | None = None,
) -> "redis.Redis":
    return redis.Redis(
        host=redis_host or "localhost",
        port=redis_port or 6379,
        username=redis_username,
        password=redis_password,
        socket_connect_timeout=CONNECT_TIMEOUT,
        socket_timeout=CONNECT_TIMEOUT,
        decode_responses=True,
    )


def _call_with_retry(func):
    """Try `func()`, retrying once on a connect/timeout error (the schema's
    "2s connect timeout, 1 retry"), then let the second failure propagate.
    """
    try:
        return func()
    except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError):
        return func()


def _auth_failed(exc: Exception) -> CoordinatorAuthFailed:
    """Build the exception for a refused login or command.
    The message never contains the password.
    """
    reply = str(exc).rstrip(".")
    if isinstance(exc, redis.exceptions.NoPermissionError):
        return CoordinatorAuthFailed(
            "redis user is not allowed to run this command. "
            f"Check the user's ACL in docs/redis-schema.md. Redis said: {reply}",
            password_refused=False,
        )
    return CoordinatorAuthFailed(
        f"redis refused the login. Check the Redis password. Redis said: {reply}",
        password_refused=True,
    )


def _warn_fallback(slot: str, exc: Exception) -> None:
    if isinstance(exc, _AUTH_ERRORS):
        message = f"lupin: {_auth_failed(exc)}. Falling back to the local backend for slot {slot!r}"
    else:
        message = (
            f"lupin: redis unreachable ({exc}); falling back to the local backend "
            f"for slot {slot!r}"
        )
    print(message, file=sys.stderr)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _get_or_set_max(client: "redis.Redis", slot: str, max_holders: int | None) -> int:
    key = f"{PREFIX}slot:{slot}:max"
    chosen = max_holders if max_holders is not None else 1
    client.set(key, chosen, nx=True)
    return int(client.get(key))


def set_max(
    slot: str,
    max_holders: int,
    *,
    redis_host: str | None = None,
    redis_port: int | None = None,
    redis_username: str | None = None,
    redis_password: str | None = None,
) -> int:
    """Change a slot's max, overwriting whatever `_get_or_set_max` set it to.

    A plain `SET`, not `NX` -- unlike `acquire`'s one-time bootstrap, this
    call means to replace the stored number. Does not touch `slot:<name>`
    (the holder sorted set): a lowered max does not evict anyone already
    holding a lease, since `_ACQUIRE_SCRIPT` only checks the max on a new
    acquire, never on an existing holder's renew. New acquires are turned
    away until enough holders release or expire to bring the count back
    under the new max.

    Raises `CoordinatorUnreachable` if Redis can't be reached -- there is no
    local-backend equivalent of this call to fall back to, for `bmo` or any
    other slot.
    """
    client = _client(redis_host, redis_port, redis_username, redis_password)
    key = f"{PREFIX}slot:{slot}:max"
    try:
        _call_with_retry(lambda: client.set(key, max_holders))
    except _AUTH_ERRORS as exc:
        raise _auth_failed(exc) from exc
    except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as exc:
        raise CoordinatorUnreachable(slot) from exc
    return max_holders


def acquire(
    slot: str,
    holder: str,
    *,
    wait: float = 0.0,
    ttl: float = local_slots.DEFAULT_TTL,
    max_holders: int | None = None,
    redis_host: str | None = None,
    redis_port: int | None = None,
    redis_username: str | None = None,
    redis_password: str | None = None,
    state_root: str | Path | None = None,
) -> str:
    """Acquire a lease on `slot`, same contract as `slots.acquire` (blocks up
    to `wait` seconds, polling; raises `SlotFull` once `wait` elapses).

    Falls back to the `local` backend for `slot == "bmo"` if Redis is
    unreachable; raises `CoordinatorUnreachable` for any other slot.
    """
    client = _client(redis_host, redis_port, redis_username, redis_password)
    key = f"{PREFIX}slot:{slot}"
    ttl_ms = int(ttl * 1000)
    deadline = time.monotonic() + wait
    poll_interval = 0.2
    while True:
        try:
            max_n = _call_with_retry(lambda: _get_or_set_max(client, slot, max_holders))
            result = _call_with_retry(
                lambda: client.eval(_ACQUIRE_SCRIPT, 1, key, holder, _now_ms(), ttl_ms, max_n)
            )
        except _AUTH_ERRORS as exc:
            if slot not in FALLBACK_SLOTS:
                raise _auth_failed(exc) from exc
            if isinstance(exc, redis.exceptions.NoPermissionError):
                raise
            _warn_fallback(slot, exc)
            return local_slots.acquire(
                slot, holder, wait=wait, ttl=ttl, max_holders=max_holders, state_root=state_root
            )
        except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as exc:
            if slot not in FALLBACK_SLOTS:
                raise CoordinatorUnreachable(slot) from exc
            _warn_fallback(slot, exc)
            return local_slots.acquire(
                slot, holder, wait=wait, ttl=ttl, max_holders=max_holders, state_root=state_root
            )
        if result:
            return f"{slot}:{holder}"
        if time.monotonic() >= deadline:
            raise SlotFull(slot)
        time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))


def renew(
    lease: str,
    *,
    ttl: float = local_slots.DEFAULT_TTL,
    redis_host: str | None = None,
    redis_port: int | None = None,
    redis_username: str | None = None,
    redis_password: str | None = None,
    state_root: str | Path | None = None,
) -> bool:
    """Extend `lease`'s deadline. Return False if the lease is gone.

    For a slot with no local fallback, return False if Redis is unreachable or refuses a login.
    For `bmo`, an unreachable Redis or a refused login uses the local result.
    Raise `NoPermissionError` if the ACL denies the command.
    The `hold` renew timer calls this. That timer does not catch errors.
    """
    slot, holder = split_lease(lease)
    client = _client(redis_host, redis_port, redis_username, redis_password)
    key = f"{PREFIX}slot:{slot}"
    try:
        result = _call_with_retry(
            lambda: client.eval(_RENEW_SCRIPT, 1, key, holder, _now_ms(), int(ttl * 1000))
        )
        return bool(result)
    except (*_AUTH_ERRORS, redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as exc:
        if isinstance(exc, redis.exceptions.NoPermissionError):
            raise
        if slot not in FALLBACK_SLOTS:
            return False
        _warn_fallback(slot, exc)
        return local_slots.renew(lease, ttl=ttl, state_root=state_root)


def release(
    lease: str,
    *,
    redis_host: str | None = None,
    redis_port: int | None = None,
    redis_username: str | None = None,
    redis_password: str | None = None,
    state_root: str | Path | None = None,
) -> bool:
    """Compare-and-delete release, same contract as `slots.release` (not an
    error to release twice). Falls back to the `local` backend for the
    `bmo` slot if Redis is unreachable.
    """
    slot, holder = split_lease(lease)
    client = _client(redis_host, redis_port, redis_username, redis_password)
    key = f"{PREFIX}slot:{slot}"
    try:
        result = _call_with_retry(lambda: client.eval(_RELEASE_SCRIPT, 1, key, holder))
        return bool(result)
    except _AUTH_ERRORS as exc:
        if slot not in FALLBACK_SLOTS:
            raise _auth_failed(exc) from exc
        if isinstance(exc, redis.exceptions.NoPermissionError):
            raise
        _warn_fallback(slot, exc)
        return local_slots.release(lease, state_root=state_root)
    except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as exc:
        if slot not in FALLBACK_SLOTS:
            raise CoordinatorUnreachable(slot) from exc
        _warn_fallback(slot, exc)
        return local_slots.release(lease, state_root=state_root)


def status(
    *,
    redis_host: str | None = None,
    redis_port: int | None = None,
    redis_username: str | None = None,
    redis_password: str | None = None,
    state_root: str | Path | None = None,
) -> dict[str, dict]:
    """Return `{slot_name: {"holders": live_count, "max": max_or_None}}`,
    read from the sorted sets without pruning them (`ZREMRANGEBYSCORE` is an
    `acquire`/`renew` job, not this one's).

    Listed by `:max` key, not by the sorted set itself -- Redis deletes a
    sorted set once its last member is removed, but a slot that has gone
    back to zero holders still exists (same as the `local` backend, whose
    slot directory outlives its last holder file) and should still show up
    with `holders: 0`, not disappear from the report.

    Falls back to the `local` backend's status wholesale if Redis is
    unreachable -- v1 has exactly one fleet slot (`bmo`), the one slot with
    a local fallback, so there is nothing else this call could report that
    the local backend wouldn't also have a view of.
    """
    client = _client(redis_host, redis_port, redis_username, redis_password)
    try:
        now = _now_ms()
        result: dict[str, dict] = {}
        max_keys = _call_with_retry(lambda: list(client.scan_iter(match=f"{PREFIX}slot:*:max")))
        for max_key in max_keys:
            slot = max_key[len(f"{PREFIX}slot:") : -len(":max")]
            members = client.zrange(f"{PREFIX}slot:{slot}", 0, -1, withscores=True)
            live = sum(1 for _holder, score in members if score >= now)
            max_raw = client.get(max_key)
            result[slot] = {"holders": live, "max": int(max_raw) if max_raw is not None else None}
        return result
    except (*_AUTH_ERRORS, redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as exc:
        if isinstance(exc, redis.exceptions.NoPermissionError):
            raise
        _warn_fallback("status", exc)
        return local_slots.status(state_root=state_root)


def hold(
    command: list[str],
    *,
    lease: str | None = None,
    slot: str | None = None,
    holder: str | None = None,
    wait: float = 0.0,
    ttl: float = local_slots.DEFAULT_TTL,
    max_holders: int | None = None,
    redis_host: str | None = None,
    redis_port: int | None = None,
    redis_username: str | None = None,
    redis_password: str | None = None,
    state_root: str | Path | None = None,
) -> int:
    """Same contract as `slots.hold`: acquire (unless `lease` is already
    held), run `command`, renew while it runs, release on exit.
    """
    if lease is None:
        if slot is None or holder is None:
            raise ValueError("hold needs either lease=, or slot= and holder=")
        lease = acquire(
            slot,
            holder,
            wait=wait,
            ttl=ttl,
            max_holders=max_holders,
            redis_host=redis_host,
            redis_port=redis_port,
            redis_username=redis_username,
            redis_password=redis_password,
            state_root=state_root,
        )

    return run_with_lease(
        command,
        lease,
        ttl=ttl,
        renew=lambda lease_id: renew(
            lease_id,
            ttl=ttl,
            redis_host=redis_host,
            redis_port=redis_port,
            redis_username=redis_username,
            redis_password=redis_password,
            state_root=state_root,
        ),
        release=lambda lease_id: release(
            lease_id,
            redis_host=redis_host,
            redis_port=redis_port,
            redis_username=redis_username,
            redis_password=redis_password,
            state_root=state_root,
        ),
    )
