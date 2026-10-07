"""The fleet machine registry: `lupin join`/`heartbeat`/`drain`/`undrain`/
`machines` (issue #7, part of #2's plan). See `docs/redis-schema.md`'s
"Fleet keys" section for the `machine:<name>` key shape this module reads
and writes -- that doc is the spec, this module is just that spec in code.

Reuses `slots_redis._client()`/`_call_with_retry()` for the Redis
connection and retry-once rule, and `slots_redis.status()` for the slot
summary -- same backend, same conventions, not reimplemented here.

Judgment call -- offline detection: the schema lists `machine:<name>` as
"with a TTL" and says a machine that misses two 30s renewals (120s) is
offline, "same convention as the bmo slot lease". The bmo lease enforces
its TTL by comparing a stored expiry to "now" when read, not by relying on
Redis to delete the key the instant it expires -- `status()` still reports
a holder whose score has passed until the next acquire/renew prunes it.
This module copies that: `OFFLINE_AFTER` (120s) is compared against the
record's own `heartbeat` field by `machines()`, so a dead machine shows up
as "offline" instead of silently vanishing. The Redis key itself gets a
much longer TTL (`RECORD_TTL`, 20x `OFFLINE_AFTER`) purely as a janitor for
machines retired long ago -- not the thing that decides online/offline.

`quota` comes from `quota.snapshot()` (issue #8), recomputed on every
write. `providers` is still a stub (`[]`) -- nothing populates it yet, but
`_write_record` carries over whatever is already there instead of
overwriting it, so a future writer's value survives the next heartbeat.

`loops` (issue #2 phase A) is this machine's live loops, each
`{"repo", "platform", "state", "since"}` -- `state` is always `None` for
now, since there is no Herdr/agent-state signal on the tmux backend yet.
Like `providers`, a caller that doesn't recompute it on a given write
(`join`/`drain`/`undrain`) leaves the existing value alone rather than
wiping it; `heartbeat()` is meant to be the one that keeps it fresh.
`machines.py` has no tmux/loopctl access of its own (that's `serve.py`'s
domain, and `serve.py` already imports this module, so the reverse import
would cycle) -- the caller (`cli.py`) gathers the list and passes it in.
`session_backend` is a fixed `"tmux"` for now (every machine, until
ghostbook.nix's `LOOP_BACKEND` switch ships, phase B of issue #2's plan).
`actions` is the fixed list of queue actions this machine's `lupin agent`
accepts -- read straight from `agent.ACTIONS`, so it can never drift from
what the agent actually runs.
"""

from __future__ import annotations

import json
import socket
import time
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path

import redis

from . import quota, slots_redis

CoordinatorUnreachable = slots_redis.CoordinatorUnreachable

PREFIX = slots_redis.PREFIX
OFFLINE_AFTER = 120.0
RECORD_TTL = int(OFFLINE_AFTER * 20)
DEFAULT_CONFIG_PATH = Path.home() / ".config" / "lupin" / "fleet.json"
_REDIS_ERRORS = (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError)

# Phase A of issue #2's plan: every machine runs loops over tmux. Becomes a
# per-machine setting once ghostbook.nix's `LOOP_BACKEND` switch ships
# (phase B) -- not this module's decision to make yet.
SESSION_BACKEND = "tmux"


def package_version() -> str:
    """This machine's `lupin` version, for the record's `version` field and
    for comparing against another machine's. `importlib.metadata` reads it
    off the installed package's metadata (how the Nix-built `lupin` runs);
    in a dev checkout that was never `pip install`-ed (e.g. this repo's own
    test suite, run via `pytest`'s `pythonpath` instead), there is no such
    metadata, so this falls back to a fixed placeholder rather than raising.
    """
    try:
        return _pkg_version("lupin")
    except PackageNotFoundError:
        return "0.0.0+dev"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(stamp: str) -> float:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def hostname() -> str:
    return socket.gethostname()


def _record_key(name: str) -> str:
    return f"{PREFIX}machine:{name}"


def _slot_totals(slots: dict) -> tuple[int, int]:
    """(used, max) summed across every slot a `machines()` record reports.
    Shared by `place.py` (ranking candidates) and `quest.py` (picking a
    focus machine) -- one reading of a machine's free capacity, not two.
    """
    used = sum(int(entry.get("used", 0)) for entry in (slots or {}).values())
    max_ = sum(int(entry.get("max", 0)) for entry in (slots or {}).values())
    return used, max_


def _heartbeat_age(record: dict, now: float) -> float:
    """Seconds since a `machines()` record's own heartbeat. Shared the same
    way `_slot_totals` is -- `place.py` and `quest.py` both break ranking
    ties on heartbeat freshness.
    """
    stamp = record.get("heartbeat")
    if not stamp:
        return float("inf")
    try:
        return now - _parse_iso(stamp)
    except ValueError:
        return float("inf")


def load_config(config_path: str | Path | None = None) -> dict:
    """What `lupin join` last wrote: `redis_host`, `redis_port`, and
    `redis_username` if one was given. `{}` if this machine hasn't joined.
    """
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return {}


def _write_config(config: dict, config_path: str | Path | None = None) -> Path:
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return path


def resolve_connection(
    *,
    redis_host: str | None = None,
    redis_port: int | None = None,
    redis_username: str | None = None,
    redis_password: str | None = None,
    config_path: str | Path | None = None,
) -> dict:
    """Fill in whatever `redis_host`/`redis_port`/`redis_username` a caller
    didn't pass from the config `lupin join` wrote, then a hardcoded
    default. `redis_password` is never read from the config file (`join`
    never writes it there -- see `join`'s docstring), only from the caller
    (in practice, `cli.py`'s `--redis-password`/`$LUPIN_REDIS_PASSWORD`).
    """
    config = load_config(config_path)
    return {
        "redis_host": redis_host or config.get("redis_host") or "localhost",
        "redis_port": redis_port or config.get("redis_port") or 6379,
        "redis_username": redis_username or config.get("redis_username"),
        "redis_password": redis_password,
    }


def _slot_summary(connection: dict) -> dict:
    raw = slots_redis.status(**connection)
    return {name: {"used": info["holders"], "max": info["max"]} for name, info in raw.items()}


def _write_record(client, name: str, *, state: str, connection: dict, loops: list[dict] | None = None) -> dict:
    """`providers`/`loops` have no dedicated writer that runs on every call
    site -- carry over whatever the existing record has (same reason
    `heartbeat` carries over `state`) when `loops` isn't given, so a plain
    `join`/`drain`/`undrain` can't wipe it out. `quota` is the opposite: it
    is recomputed here every time, since this function is the only writer
    of `machine:<name>` and a stale quota reading is worse than the extra
    `quota.snapshot()` call.

    `actions` is read live from `agent.py`'s own `ACTIONS` table (imported
    here, not at module load, to avoid a top-level import cycle -- `agent.py`
    imports this module to check `draining` state). This way the list can
    never drift from what the agent here actually supports.
    """
    from . import agent  # deferred import, dodges the cycle noted above

    existing = _read_record(client, name)
    record = {
        "version": package_version(),
        "heartbeat": _now_iso(),
        "state": state,
        "slots": _slot_summary(connection),
        "providers": existing.get("providers", []) if existing else [],
        "quota": quota.snapshot(),
        "loops": loops if loops is not None else (existing.get("loops", []) if existing else []),
        "session_backend": SESSION_BACKEND,
        "actions": sorted(agent.ACTIONS),
    }
    client.set(_record_key(name), json.dumps(record), ex=RECORD_TTL)
    return record


def _read_record(client, name: str) -> dict | None:
    raw = client.get(_record_key(name))
    return json.loads(raw) if raw is not None else None


def _run(op):
    """`_call_with_retry`, but a connection failure becomes
    `CoordinatorUnreachable` -- the schema's fallback table has no fallback
    for the fleet keys (unlike the `bmo` slot), so there is nothing to fall
    back to, just one exception `cli.py` already knows how to report.
    """
    try:
        return slots_redis._call_with_retry(op)
    except _REDIS_ERRORS as exc:
        raise CoordinatorUnreachable("machine registry") from exc


def join(
    coordinator: str,
    *,
    redis_username: str | None = None,
    redis_password: str | None = None,
    config_path: str | Path | None = None,
    loops: list[dict] | None = None,
) -> dict:
    """Write the Redis location (+ user, if given) to the local fleet
    config, then register this machine as online. The password is taken
    only to make the registering call -- it is never written to the config
    file; every later command re-supplies it (flag or `$LUPIN_REDIS_PASSWORD`).
    """
    host, _, port_str = coordinator.partition(":")
    port = int(port_str) if port_str else 6379
    config = {"redis_host": host, "redis_port": port}
    if redis_username:
        config["redis_username"] = redis_username
    path = _write_config(config, config_path)

    connection = {
        "redis_host": host,
        "redis_port": port,
        "redis_username": redis_username,
        "redis_password": redis_password,
    }
    client = slots_redis._client(host, port, redis_username, redis_password)
    name = hostname()
    record = _run(lambda: _write_record(client, name, state="online", connection=connection, loops=loops))
    return {"name": name, "config_path": str(path), **record}


def heartbeat(connection: dict, *, loops: list[dict] | None = None) -> dict:
    """Refresh this machine's record, keeping whatever `state` it already
    had (so a draining machine stays draining through a heartbeat). `loops`
    is this machine's current live-loop list (issue #2 phase A) -- the
    caller (`cli.py`) gathers it, since this module has no tmux/loopctl
    access of its own (see module docstring).
    """
    client = slots_redis._client(
        connection.get("redis_host"),
        connection.get("redis_port"),
        connection.get("redis_username"),
        connection.get("redis_password"),
    )
    name = hostname()

    def op():
        existing = _read_record(client, name)
        state = existing["state"] if existing else "online"
        return _write_record(client, name, state=state, connection=connection, loops=loops)

    return _run(op)


def _set_state(connection: dict, state: str) -> dict:
    client = slots_redis._client(
        connection.get("redis_host"),
        connection.get("redis_port"),
        connection.get("redis_username"),
        connection.get("redis_password"),
    )
    name = hostname()
    return _run(lambda: _write_record(client, name, state=state, connection=connection))


def drain(connection: dict) -> dict:
    return _set_state(connection, "draining")


def undrain(connection: dict) -> dict:
    return _set_state(connection, "online")


def machines(connection: dict) -> list[dict]:
    """Every registered machine, each as:
    `{"name", "state", "version", "heartbeat", "version_mismatch", "slots",
    "providers", "quota", "loops", "session_backend", "actions"}`.

    `state` is the record's own `online`/`draining`, overridden to
    `offline` once `OFFLINE_AFTER` seconds have passed since `heartbeat`
    with no renewal -- see this module's docstring for why that is computed
    here rather than left to Redis's key TTL.

    `slots`/`providers`/`quota` are carried straight through from the
    record (see `_write_record`) -- added for `place` (issue #9), which
    scores machines on exactly this data. Earlier callers only read
    name/state/version/heartbeat, so this is a pure addition, not a change
    to those fields. `loops`/`session_backend`/`actions` (issue #2 phase A)
    are the same kind of addition -- `.get(..., default)` throughout, so a
    machine still running an older `lupin` that never wrote them shows up
    with an empty/unknown value instead of a `KeyError`.
    """
    client = slots_redis._client(
        connection.get("redis_host"),
        connection.get("redis_port"),
        connection.get("redis_username"),
        connection.get("redis_password"),
    )
    local_version = package_version()
    now = time.time()

    def op():
        keys = list(client.scan_iter(match=f"{PREFIX}machine:*"))
        result = []
        for key in keys:
            name = key[len(f"{PREFIX}machine:") :]
            raw = client.get(key)
            if raw is None:
                continue
            record = json.loads(raw)
            state = record.get("state", "online")
            if now - _parse_iso(record["heartbeat"]) > OFFLINE_AFTER:
                state = "offline"
            result.append(
                {
                    "name": name,
                    "state": state,
                    "version": record.get("version"),
                    "heartbeat": record.get("heartbeat"),
                    "version_mismatch": record.get("version") != local_version,
                    "slots": record.get("slots", {}),
                    "providers": record.get("providers", []),
                    "quota": record.get("quota", {}),
                    "loops": record.get("loops", []),
                    "session_backend": record.get("session_backend"),
                    "actions": record.get("actions", []),
                }
            )
        return result

    return _run(op)
