"""Shared cache for quota data (issue #38): one fleet-wide account per
provider (Claude, opencode-go, OpenAI/Codex -- see issue #36), not a
per-machine fact. `quota.quota_usage()` already reads whichever provider
this machine has credentials for; this module adds the fleet-shared
publish step so `lupin serve` (pinned to a machine with no credentials,
see issue #35) and `lupin quota` on any other machine see the same real
numbers instead of nothing.

Modeled on `gh_cache.py` (#35) and `benchmark_fetch.py` (#17), adapted:

- **5-minute cadence, not daily.** `CACHE_TTL` below.
- **No fixed canonical hostname.** `gh_cache.py` pins one named machine
  (`CANONICAL_GH_FETCHER`) because Grace corrected away from a lock-race
  model for GH specifically -- one machine is always known to hold the
  right `gh` login. Quota credentials for different providers can live on
  different machines, and which machine has which is not known in
  advance, so a fixed pin doesn't fit here. Instead, "canonical" is
  decided dynamically, per provider, by whether a machine's own local
  `quota.quota_usage()` call actually returned real data (a `used_pct`)
  for that provider. A machine with no credentials for a provider never
  has real data for it, so it never writes for it -- it can't clobber a
  good reading from elsewhere, without needing to know in advance who the
  "right" machine is. This is the same end effect as `gh_cache`'s pin
  (one real source of truth per key), reached a different way.
- **One shared key, one entry per provider.** Different providers can go
  stale/fresh independently (one machine might refresh "claude" while
  another refreshes "openai"), so each provider's entry carries its own
  `fetched_at`/`fetched_by`, inside one Redis key (`quota-snapshot`) --
  same single-key-many-entries shape `machine:<name>`'s own `quota` field
  already uses, just shared instead of per-machine.

The lock below (`quota-fetch/<provider>`) only stops two `lupin`
processes on the *same* real-data machine from publishing the same
provider at once -- same narrow job `gh_cache.py`'s and
`benchmark_fetch.py`'s own locks do.
"""

from __future__ import annotations

import json
import os
import socket
from datetime import datetime, timezone

import redis

from . import quota, slots_redis

CoordinatorUnreachable = slots_redis.CoordinatorUnreachable

REDIS_KEY = f"{slots_redis.PREFIX}quota-snapshot"

# How long a provider's entry is trusted before another refresh is worth
# attempting -- the issue's "5-minute refresh cadence". Retention
# (REDIS_KEY_TTL) is separate and far longer, same split
# `benchmark_fetch.py` uses between CACHE_FRESH_SECONDS and REDIS_KEY_TTL.
CACHE_TTL = 300
REDIS_KEY_TTL = 24 * 60 * 60

# Non-blocking, like benchmark_fetch.py's lock: a local quota_usage() call
# is a handful of cheap subprocess/HTTP reads, not worth another machine
# waiting on. If the lock is busy, skip publishing and leave the existing
# cached entry (possibly still fresh) alone.
LOCK_WAIT = 0.0
LOCK_TTL = 60.0

_REDIS_ERRORS = (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _client(connection: dict):
    return slots_redis._client(
        connection.get("redis_host"),
        connection.get("redis_port"),
        connection.get("redis_username"),
        connection.get("redis_password"),
    )


def _is_fresh(entry: dict | None) -> bool:
    if not entry:
        return False
    fetched_at = entry.get("fetched_at")
    if not fetched_at:
        return False
    try:
        epoch = datetime.fromisoformat(fetched_at).timestamp()
    except ValueError:
        return False
    return (datetime.now(timezone.utc).timestamp() - epoch) < CACHE_TTL


def _has_real_data(rows: list[dict] | None) -> bool:
    """True if at least one row carries an actual `used_pct` -- this
    machine has real credentials for the provider, not just a `note`/
    `error` placeholder (`quota.unavailable_usage`, a "no key"/"no login"
    note, etc.)."""
    return any(
        isinstance(row, dict) and isinstance(row.get("used_pct"), (int, float))
        for row in (rows or [])
    )


def group_by_provider(rows: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for row in rows:
        if isinstance(row, dict) and row.get("provider"):
            groups.setdefault(row["provider"], []).append(row)
    return groups


def _restore_durations(rows: list[dict]) -> list[dict]:
    """`row["duration"]` is a `quota.QuotaDuration` -- a `str` subclass --
    while this process still holds it, but a JSON round-trip through Redis
    turns it back into a plain `str` (json.dumps writes a str subclass as
    its raw characters, json.loads has no way to know it was ever an enum).
    `serve.py`'s `quota_duration_label` needs the real enum member to find
    the "5 hours"/"7 days" label, so this restores it, right after reading
    -- the one place the round-trip happens, rather than every row
    consumer re-deriving it.
    """
    restored = []
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("duration"), str):
            try:
                row = {**row, "duration": quota.QuotaDuration(row["duration"])}
            except ValueError:
                pass
        restored.append(row)
    return restored


def read_snapshot(**connection) -> dict:
    """Best-effort, passive read -- no lock, no local provider call, never
    writes anything. `{}` if nothing is cached yet, the cached value is
    corrupt, or Redis can't be reached right now -- same "degrade, don't
    crash" contract as `benchmark_fetch.read_snapshot`. This is what
    `serve.py`'s `/usage` page calls, on every render, so it never hits a
    provider API from the dashboard process.
    """
    try:
        client = _client(connection)
        raw = slots_redis._call_with_retry(lambda: client.get(REDIS_KEY))
    except _REDIS_ERRORS:
        return {}
    if raw is None:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    for provider, entry in data.items():
        if isinstance(entry, dict) and isinstance(entry.get("rows"), list):
            entry["rows"] = _restore_durations(entry["rows"])
    return data


def refresh_snapshot(*, force: bool = False, holder: str | None = None, **connection) -> dict:
    """This machine's chance to publish real quota data, for whichever
    providers it has credentials for, into the shared cache -- then
    returns the merged, fleet-wide view (this machine's fresh rows plus
    whatever is already cached for providers it doesn't have).

    Always calls `quota.quota_usage()` once (the one function in this
    module that may make live HTTP/subprocess calls) -- cheap local file
    reads plus a couple of short-timeout network calls, not worth a cache
    layer of its own. What the freshness check below gates is the
    *publish*: a provider whose cached entry is still fresh (<5 min, see
    `CACHE_TTL`) is left alone rather than overwritten with a redundant
    reading, same number or not. The actual "don't hammer a provider more
    than every 5 minutes fleet-wide" guarantee comes from *this function*
    only being invoked every 5 minutes -- by `lupin quota` on an external
    timer, same convention `fetch-models`/`fetch-benchmarks` already use.
    """
    cached = read_snapshot(**connection)
    local_rows = group_by_provider(quota.quota_usage())
    updated = dict(cached)
    changed = False
    hostname = socket.gethostname()
    for provider, rows in local_rows.items():
        if not _has_real_data(rows):
            continue
        if not force and _is_fresh(cached.get(provider)):
            # Someone else's reading for this provider is still fresh --
            # don't pay for a redundant publish.
            continue
        lease_holder = holder or f"{hostname}:{os.getpid()}"
        try:
            lease = slots_redis.acquire(
                f"quota-fetch/{provider}", lease_holder,
                wait=LOCK_WAIT, ttl=LOCK_TTL, max_holders=1, **connection,
            )
        except slots_redis.SlotFull:
            continue  # another process on this machine is already publishing this provider
        except slots_redis.CoordinatorUnreachable:
            # No fleet cache reachable, so nothing to publish *through* --
            # but this machine's own reading is still real. Surface it to
            # this machine's own caller rather than hiding it over a
            # cache-layer outage, same principle `gh_cache.py`'s
            # `CANONICAL_GH_FETCHER` path uses ("still the authority even
            # when it can't publish for anyone else").
            updated[provider] = {"rows": rows, "fetched_at": _now_iso(), "fetched_by": hostname}
            continue
        try:
            updated[provider] = {
                "rows": rows,
                "fetched_at": _now_iso(),
                "fetched_by": hostname,
            }
            changed = True
        finally:
            try:
                slots_redis.release(lease, **connection)
            except slots_redis.CoordinatorUnreachable:
                pass

    if changed:
        try:
            client = _client(connection)
            slots_redis._call_with_retry(
                lambda: client.set(REDIS_KEY, json.dumps(updated), ex=REDIS_KEY_TTL)
            )
        except _REDIS_ERRORS:
            pass  # best effort -- this machine's own caller still gets `updated` back

    return updated
