"""Shared cache for read-only `gh` lookups (issue #35): `place.py`,
`quest.py`, `roadmap.py`, and `roadmap_cli.py` all call `gh` for the same
kind of data -- issue state, body, labels, dependency links. Every machine
doing that independently means duplicate rate-limit pressure and different
machines seeing different snapshots of the same repo at the same moment.

Grace's correction on the first design: the fetcher is not "whichever
machine wins a lock race" -- it is one named machine, pinned by hostname.
`CANONICAL_GH_FETCHER` is that name. Every other machine only ever reads
the cache; on a miss it returns an honest "no data yet" error instead of
calling `gh` itself, even if Redis is down. The `slots_redis` lock below
still matters, but only to stop the canonical fetcher from running the same
fetch twice if two `lupin` invocations on that one machine race each other.

Risk, stated rather than papered over: `CANONICAL_GH_FETCHER` is compared
against `machines.hostname()` (`socket.gethostname()`) with a plain `==`.
If that machine's hostname is ever reported differently -- a FQDN
(`pihome.local`) instead of the short name, or changed by whoever reimages
it -- this check silently stops matching and the fetch path goes cold
fleet-wide (every machine, including the one meant to fetch, falls back to
"no data yet"). Nothing here detects that; it would show up as every
caller's cache staying empty.
"""

from __future__ import annotations

import json
from typing import Any, Callable

import redis

from . import machines, slots_redis

PREFIX = slots_redis.PREFIX
CANONICAL_GH_FETCHER = "pihome"

# A few minutes: short enough that `place`/`quest` aren't deciding off data
# that's badly stale, long enough that a burst of lookups across machines
# within that window shares one fetch instead of each paying for their own.
CACHE_TTL = 300

# The lock only guards the canonical fetcher against itself (two `lupin`
# invocations on the same machine racing), so a short wait is enough --
# contention here is rare and brief. The TTL is generous (2 min) because a
# paginated GraphQL fetch (comments, dependency links) can run several
# `gh api graphql` calls in a row; if a fetch ever runs past this, the lock
# just expires and a second fetch may start -- a harmless duplicate read,
# not a correctness problem.
LOCK_WAIT = 2.0
LOCK_TTL = 120.0

_REDIS_ERRORS = (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError)


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


def cached_gh_json(
    owner: str,
    name: str,
    cache_key: str,
    fetch_fn: Callable[[], tuple[Any, str | None]],
    *,
    connection: dict | None = None,
) -> tuple[Any, str | None]:
    """Return `fetch_fn()`'s result, either from the shared cache or from a
    live `gh` call -- but only on `CANONICAL_GH_FETCHER` does a cache miss
    ever run `fetch_fn`. Every other machine gets `(None, "<explanation>")`
    on a miss, never calling `gh` itself, Redis up or down.

    `fetch_fn` takes no arguments and returns `(data, error)`, the same
    shape every `gh`-calling function in this codebase already uses --
    callers wrap their real call in a closure. A result is cached only when
    `error` is falsy; a failed fetch is never cached, so the next caller
    (which might be able to reach `gh` where this one couldn't) gets a real
    retry instead of a cached failure.
    """
    connection = _resolve(connection)
    client = _client(connection)
    key = f"{PREFIX}gh-cache:{owner}/{name}:{cache_key}"

    hit, cached, redis_ok = _read_cache(client, key)
    if hit:
        return cached, None

    hostname = machines.hostname()
    if hostname != CANONICAL_GH_FETCHER:
        if redis_ok:
            return None, (
                f"no cached GitHub data yet for {cache_key} ({owner}/{name}); "
                f"only {CANONICAL_GH_FETCHER} fetches live data"
            )
        return None, (
            f"the GitHub data cache is unreachable and this machine ({hostname}) "
            f"is not {CANONICAL_GH_FETCHER}, so it cannot fetch directly"
        )

    if not redis_ok:
        # CANONICAL_GH_FETCHER is still the authority even when it can't
        # publish for anyone else -- answer its own caller with a live
        # fetch rather than failing a command over a cache-layer outage.
        return fetch_fn()

    lease = None
    try:
        lease = slots_redis.acquire(
            f"gh-fetch:{owner}/{name}",
            holder=cache_key,
            wait=LOCK_WAIT,
            ttl=LOCK_TTL,
            redis_host=connection.get("redis_host"),
            redis_port=connection.get("redis_port"),
            redis_username=connection.get("redis_username"),
            redis_password=connection.get("redis_password"),
        )
    except slots_redis.SlotFull:
        # Another fetch for this repo is already in flight on this same
        # machine -- check once more in case it just finished, otherwise
        # fetch anyway. A duplicate read is wasted work, not a bug.
        hit, cached, _redis_ok = _read_cache(client, key)
        if hit:
            return cached, None
    except slots_redis.CoordinatorUnreachable:
        return fetch_fn()

    try:
        data, error = fetch_fn()
        if error:
            return data, error
        _write_cache(client, key, data)
        return data, None
    finally:
        if lease:
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
        pass  # best effort -- CANONICAL_GH_FETCHER still has the live answer
