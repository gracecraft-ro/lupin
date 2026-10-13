"""Share read-only GitHub lookups across machines.

The machine named by `LUPIN_GH_CANONICAL_FETCHER` fetches live data. Other
machines use the cache while it is online. If it is offline or the setting is
empty, a machine with Redis access may fetch under the per-repo slot. With no
pin, a caller that finds the slot busy returns a fetch-in-progress error.
Machines without Redis access cannot fetch unless they are the named fetcher.
The name must match `machines.hostname()` exactly.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any, Callable

import redis

from . import machines, slots_redis

PREFIX = slots_redis.PREFIX

# A few minutes: short enough that `place`/`quest` aren't deciding off data
# that's badly stale, long enough that a burst of lookups across machines
# within that window shares one fetch instead of each paying for their own.
CACHE_TTL = 300

# The per-repo slot guards concurrent fetches. A paginated GitHub request
# can take longer than the brief wait. Its lease lasts two minutes.
LOCK_WAIT = 2.0
LOCK_TTL = 120.0

_REDIS_ERRORS = (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError)


def canonical_fetcher() -> str | None:
    """The hostname of the machine that runs `gh` for the fleet.

    Read from `LUPIN_GH_CANONICAL_FETCHER` on each call. Returns None when
    the variable is unset or empty. Then no machine is pinned.
    """
    return os.environ.get("LUPIN_GH_CANONICAL_FETCHER") or None


def _resolve(connection: dict | None) -> dict:
    """Fold `connection` (whatever a caller passed, if anything) into the
    fleet's configured Redis location -- `machines.resolve_connection`
    already knows how to do this (reads `~/.config/lupin/fleet.json`,
    falls back to localhost). Without this, a machine that never threaded
    a `connection` dict this deep would always miss the fleet's real Redis
    and talk to a local, empty one -- defeating the point of a shared cache.
    """
    connection = connection or {}
    return machines.resolve_connection(
        redis_host=connection.get("redis_host"),
        redis_port=connection.get("redis_port"),
        redis_username=connection.get("redis_username"),
        redis_password=connection.get("redis_password"),
    )


def _client(connection: dict):
    return slots_redis._client(
        connection.get("redis_host"),
        connection.get("redis_port"),
        connection.get("redis_username"),
        connection.get("redis_password"),
    )


def _canonical_fetcher_live(connection: dict) -> bool:
    """Check if the pinned fetcher is online in the fleet registry.

    With no pin, return False. If the registry cannot be read, keep the
    pin active so another machine does not start a live fetch.
    """
    pinned = canonical_fetcher()
    if pinned is None:
        return False
    try:
        records = {record["name"]: record for record in machines.machines(connection, strict=True)}
    except Exception:
        return True
    record = records.get(pinned)
    return bool(record) and record.get("state") == "online"


def _fetch_and_publish(
    owner: str,
    name: str,
    cache_key: str,
    fetch_fn: Callable[[], tuple[Any, str | None]],
    client,
    connection: dict,
    unpinned: bool,
) -> tuple[Any, str | None]:
    """Fetch under the shared per-repo slot and publish the result."""
    key = f"{PREFIX}gh-cache:{owner}/{name}:{cache_key}"
    # A unique holder per call, not just `cache_key`: `slots_redis.acquire`
    # treats a second acquire from the *same* holder as a renew, not
    # contention (see `_ACQUIRE_SCRIPT` -- same holder means no wait at
    # all). Two real concurrent calls for the same cache_key need distinct
    # holders to actually serialize against each other; `cache_key` alone
    # would make them look like the same caller renewing its own lease.
    holder = f"{cache_key}:{uuid.uuid4().hex[:8]}"

    # No colon in the slot name: `_lease_runtime.split_lease` rebuilds
    # `(slot, holder)` from the lease string `f"{slot}:{holder}"` by
    # splitting on the *first* colon only, so a colon inside `slot` itself
    # (e.g. "gh-fetch:acme/repo") makes it parse the wrong slot and
    # `release()`/`renew()` silently act on a key that was never acquired
    # -- the real lock then never clears until LOCK_TTL expires. "/" has
    # the same job without that trap.
    lease = None
    try:
        lease = slots_redis.acquire(
            f"gh-fetch/{owner}/{name}",
            holder=holder,
            wait=LOCK_WAIT,
            ttl=LOCK_TTL,
            redis_host=connection.get("redis_host"),
            redis_port=connection.get("redis_port"),
            redis_username=connection.get("redis_username"),
            redis_password=connection.get("redis_password"),
        )
    except slots_redis.SlotFull:
        hit, cached, _redis_ok = _read_cache(client, key)
        if hit:
            return cached, None
        if unpinned:
            return None, (
                f"no cached GitHub data yet for {cache_key} ({owner}/{name}); "
                "fetch in progress"
            )
    except slots_redis.CoordinatorUnreachable:
        return fetch_fn()
    else:
        # We got the lock, possibly after waiting on LOCK_WAIT -- the
        # holder we were waiting behind may have already finished and
        # published the result. Check before paying for a second live
        # fetch; without this, every caller that waits (the normal case,
        # not just SlotFull) double-fetches `gh`.
        hit, cached, _redis_ok = _read_cache(client, key)
        if hit:
            _release(lease, connection)
            return cached, None

    try:
        data, error = fetch_fn()
        if error:
            return data, error
        _write_cache(client, key, data)
        return data, None
    finally:
        if lease:
            _release(lease, connection)


def cached_gh_json(
    owner: str,
    name: str,
    cache_key: str,
    fetch_fn: Callable[[], tuple[Any, str | None]],
    *,
    connection: dict | None = None,
) -> tuple[Any, str | None]:
    """Read the shared cache or fetch live GitHub data.

    The named fetcher can fetch on a miss. Other machines can fetch under
    the per-repo slot when the named fetcher is offline or unset. When
    Redis is unavailable, only the named fetcher can fetch.

    `fetch_fn` returns `(data, error)`. A failed fetch is not cached.
    """
    connection = _resolve(connection)
    client = _client(connection)
    key = f"{PREFIX}gh-cache:{owner}/{name}:{cache_key}"

    hit, cached, redis_ok = _read_cache(client, key)
    if hit:
        return cached, None

    pinned = canonical_fetcher()
    hostname = machines.hostname()
    canonical = hostname == pinned
    if not canonical:
        if not redis_ok:
            unpinned = "the canonical fetcher (LUPIN_GH_CANONICAL_FETCHER is unset)"
            return None, (
                f"the GitHub data cache is unreachable and this machine ({hostname}) "
                f"is not {pinned or unpinned}, so it cannot fetch directly"
            )
        if _canonical_fetcher_live(connection):
            return None, (
                f"no cached GitHub data yet for {cache_key} ({owner}/{name}); "
                f"only {pinned} fetches live data"
            )
        # Nobody is maintaining the cache, so fall through and fetch here.

    if not redis_ok:
        # Only the canonical fetcher reaches this with Redis down: it is
        # still the authority even when it cannot publish for anyone else
        # -- answer its own caller with a live fetch rather than failing a
        # command over a cache-layer outage.
        return fetch_fn()

    return _fetch_and_publish(
        owner, name, cache_key, fetch_fn, client, connection, unpinned=pinned is None
    )


def _release(lease, connection: dict) -> None:
    try:
        slots_redis.release(
            lease,
            redis_host=connection.get("redis_host"),
            redis_port=connection.get("redis_port"),
            redis_username=connection.get("redis_username"),
            redis_password=connection.get("redis_password"),
        )
    except slots_redis.CoordinatorUnreachable:
        pass


def _read_cache(client, key: str) -> tuple[bool, Any, bool]:
    try:
        raw = slots_redis._call_with_retry(lambda: client.get(key))
    except _REDIS_ERRORS:
        return False, None, False
    if raw is None:
        return False, None, True
    try:
        envelope = json.loads(raw)
        return True, envelope["data"], True
    except (json.JSONDecodeError, KeyError, TypeError):
        return False, None, True


def _write_cache(client, key: str, data: Any) -> None:
    try:
        slots_redis._call_with_retry(
            lambda: client.set(key, json.dumps({"data": data}), ex=CACHE_TTL)
        )
    except _REDIS_ERRORS:
        pass  # best effort -- the canonical fetcher still has the live answer
