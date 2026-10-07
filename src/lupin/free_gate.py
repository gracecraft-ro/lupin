"""Gate a `route()` pick behind the fleet-wide `bmo` Redis slot (issue #37).

`route.route()` only picks a {model, effort} pair; it never touches a lock
(see its own docstring). Nothing in this repo calls `slots_redis.acquire`
for the `bmo` slot before dispatching to a `bmo:` model today -- two
callers could both get routed to bmo at once. `gate()` is the one layer
that does: route first, then, only for a `bmo:` pick, try to reserve the
real slot before calling the pick final.

A non-`bmo:` pick (`sonnet`, `opus`, `local:...`) comes back unchanged --
this module only ever gates bmo, nothing else (#37 decision 2: bmo is tried
whenever `route()` picks it, full stop, regardless of paid-quota surplus).

No keepalive: bmo wakes and sleeps on its own power schedule
(`waifu.nix/hosts/bmo/modules/power.nix`) whether or not a lease is held,
so holding the slot idle would only block real work for nothing (#37
decision 1 -- waking bmo outside that schedule is fine, but this module
adds no time-of-day guard of its own and no wake mechanism; that call, if
one is ever needed, is cross-repo infra, not this function's job).

Two ways to call `gate()`, chosen by the caller via `inline` (#37 decision
3 -- this is the caller's job to know, not something to guess from here):

- `inline=True`: the caller is already inside a live unit that cannot
  block long. Waits at most `INLINE_WAIT_S` -- a short, fixed technical
  ceiling on how long it's OK to stall a live process. It is not a claim
  about how long a real bmo session runs.
- `inline=False`: a deferred/background caller, where the alternative to
  waiting is spending real paid quota. Worth waiting longer, but "longer"
  is read from bmo's actual current lease, not guessed -- every repo's
  tasks run a different real length, so there is no one "typical session"
  constant to hardcode. If bmo is free, waits 0s. If bmo is held, waits
  exactly the current holder's real remaining TTL (plus a small buffer for
  clock skew and the read-then-acquire gap), then gives up.

Either way, failing to acquire does not hide the fact: `gate()` returns
route's pick regardless, but `bmo_acquired` says whether it actually got
the lock, and `lease` is the id to `release()` later, or `None`.
"""

from __future__ import annotations

from pathlib import Path

import redis

from . import route as route_mod
from . import slots_redis

# How long it's acceptable to stall a live, already-running unit waiting on
# bmo. A technical floor on blocking a live process, not a guess about how
# long bmo sessions run -- see module docstring.
INLINE_WAIT_S = 10.0

# Extra seconds past a holder's real remaining TTL before a deferred caller
# gives up -- covers clock skew and the gap between reading the lease and
# calling acquire(), not a guess about bmo's session length.
DEFERRED_WAIT_BUFFER_S = 3.0


def _remaining_wait_s(slot: str, connection: dict) -> float:
    """Seconds until `slot` has room for a new holder, from its real Redis
    state -- 0 if it already has room (or the slot has no holders yet).

    Reuses `slots_redis`'s own connection/retry helpers (`_client`,
    `_call_with_retry`) rather than re-implementing them -- `machines.py`
    already does the same thing for the same reason (see `join`/`_run`
    there): this module exists specifically to act on the bmo slot those
    helpers already model, so going around them would just be a second,
    divergent copy of the same connection logic.

    Returns 0 if Redis can't be reached: there is no lease state to read in
    that case, so this does not block any longer than a plain `acquire`
    call already would once it falls back to the `local` backend for bmo.
    """
    client = slots_redis._client(
        connection.get("redis_host"),
        connection.get("redis_port"),
        connection.get("redis_username"),
        connection.get("redis_password"),
    )
    key = f"{slots_redis.PREFIX}slot:{slot}"
    try:
        max_raw = slots_redis._call_with_retry(lambda: client.get(f"{key}:max"))
        members = slots_redis._call_with_retry(lambda: client.zrange(key, 0, -1, withscores=True))
    except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError):
        return 0.0
    max_n = int(max_raw) if max_raw is not None else 1
    now = slots_redis._now_ms()
    live_scores = [score for _holder, score in members if score >= now]
    if len(live_scores) < max_n:
        return 0.0
    return max(0.0, (min(live_scores) - now) / 1000)


def gate(
    category: str,
    size: str,
    holder: str,
    *,
    inline: bool,
    bmo_available: bool = True,
    quota_exhausted: bool = False,
    primary_effort: str | None = None,
    tiers: dict | None = None,
    inline_wait_s: float = INLINE_WAIT_S,
    deferred_buffer_s: float = DEFERRED_WAIT_BUFFER_S,
    connection: dict | None = None,
    state_root: str | Path | None = None,
) -> dict:
    """Route `(category, size)`, then gate a `bmo:` pick behind the real
    `bmo` slot. Every `route()` arg other than `holder` passes through
    unchanged -- `route()` itself is not touched by this call.

    Returns `{"pick": {"model": ..., "effort": ...}, "bmo_acquired": bool,
    "lease": str | None}`. `bmo_acquired` is `False` and `lease` is `None`
    whenever the pick isn't a `bmo:` model (nothing to gate) or the slot
    stayed full past the wait -- either way, `pick` is still route's real
    choice, never swapped out by this function.

    `inline`/`inline_wait_s`/`deferred_buffer_s` -- see module docstring.
    `holder`/`connection`/`state_root` are the same shape every other fleet
    call in this repo uses (`machines.resolve_connection`'s dict, `cli.py`'s
    `--state-root`).
    """
    pick = route_mod.route(
        category,
        size,
        bmo_available=bmo_available,
        quota_exhausted=quota_exhausted,
        primary_effort=primary_effort,
        tiers=tiers,
    )
    if not pick["model"].startswith("bmo:"):
        return {"pick": pick, "bmo_acquired": False, "lease": None}

    conn = connection or {}
    if inline:
        wait = inline_wait_s
    else:
        remaining = _remaining_wait_s("bmo", conn)
        wait = remaining + deferred_buffer_s if remaining > 0 else 0.0

    try:
        lease = slots_redis.acquire("bmo", holder, wait=wait, state_root=state_root, **conn)
    except slots_redis.SlotFull:
        return {"pick": pick, "bmo_acquired": False, "lease": None}

    return {"pick": pick, "bmo_acquired": True, "lease": lease}
