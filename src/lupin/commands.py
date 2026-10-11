"""Cross-machine command queue: one machine enqueues a signed command for
another, the target's `lupin agent` process claims and runs it. Implements
#27's design -- see that issue's design comment for the full spec (key
schema, signing, security layers); `docs/redis-schema.md` has the key
shapes as deployed here.

Four key families, all under `lupin:v1:`:
- `cmdq:<machine>` -- sorted set, member = command id, score = issued_at
  (ms). The target machine's pending queue, oldest first.
- `cmd:<id>` -- string (JSON), the signed command itself. `PX` TTL is a
  fixed retention window (`CMD_RETENTION_S`, 1h, matching the design's
  table) -- not the same thing as whether the command is still valid to
  run. That's `expires_at`, a field inside the JSON, checked by the
  executor (`agent.py`). Written once, by `enqueue`'s EVAL (`SET ... NX`).
- `cmdres:<id>` -- string (JSON), the result. The executor claims a
  command with `SET ... NX` (first writer wins a race between two
  pollers), then overwrites its own claim with the final result.
- `cmdlog` -- a capped stream (`XADD ... MAXLEN ~`), one entry per
  enqueue and one per terminal outcome -- the audit trail.

Signing: HMAC-SHA256 over a canonical JSON encoding (`json.dumps(...,
sort_keys=True, separators=(",", ":"))`) of every field except `sig`
itself. One shared secret per target machine (`--signing-key` /
`$LUPIN_CMD_SIGNING_KEY`, same convention as `--redis-password` /
`$LUPIN_REDIS_PASSWORD`) -- whoever sends to a machine must know that
machine's key, so a compromised host can't forge a command for a
different one (design's security section 4b: per-target HMAC, decided
over Redis ACL selectors alone).

This module only handles the Redis plumbing and the signature -- it does
not know or enforce which actions exist. That allowlist lives in
`agent.py`, the thing that actually executes a command, on the principle
that the thing holding the shell is the thing that should say no.

ACL note: the enqueue EVAL does `SET ... NX` + `ZADD` instead of `MULTI` --
`MULTI` is not on this project's Redis ACL command list
(`docs/redis-schema.md`), same reason `slots_redis.py`'s scripts use
`EVAL` for every atomic pair.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
import uuid
from pathlib import Path

import redis

from .slots import CoordinatorUnreachable
from .slots_redis import _auth_failed, _call_with_retry, _client

PREFIX = "lupin:v1:"

# Two different things that both look like "a TTL" -- kept apart on purpose
# (finding #5 of the independent review on #28: conflating them left one
# check dead). `CMD_RETENTION_S` is how long the signed request record
# stays in Redis at all (so `cmd status` can still explain an old id).
# `DEFAULT_PICKUP_S` is how long a command stays *valid to run* -- the
# design's "pickup deadline, not a run-time limit". A command can easily
# still be sitting in Redis (first number) long after it's stopped being
# runnable (second number).
CMD_RETENTION_S = 3600.0  # 1 hour, matches the design's key table
RESULT_TTL_S = 3600.0  # 1 hour -- long enough to query a result after it finishes
DEFAULT_PICKUP_S = 120.0  # design default: issued_at + 120s
CLOCK_SKEW_S = 30.0  # design's allowance on the staleness check

LOG_MAXLEN = 2000  # design: `XADD cmdlog MAXLEN ~ 2000`

STATES = {"queued", "running", "ok", "failed", "rejected", "expired"}


def cmd_key(cmd_id: str) -> str:
    return f"{PREFIX}cmd:{cmd_id}"


def res_key(cmd_id: str) -> str:
    return f"{PREFIX}cmdres:{cmd_id}"


def queue_key(machine: str) -> str:
    return f"{PREFIX}cmdq:{machine}"


LOG_KEY = f"{PREFIX}cmdlog"


def now_ms() -> int:
    return int(time.time() * 1000)


def canonical(fields: dict) -> bytes:
    """The exact bytes a signature covers -- same encoding on both ends, or
    a correct signature from one side looks wrong to the other."""
    return json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()


def sign(fields: dict, key: str) -> str:
    return hmac.new(key.encode(), canonical(fields), hashlib.sha256).hexdigest()


def verify(cmd: dict, key: str) -> bool:
    """Check `cmd`'s `sig` against every other field, signed with `key`.
    `hmac.compare_digest`, not `==` -- a timing difference on a wrong guess
    is itself information an attacker can use.
    """
    payload = {k: v for k, v in cmd.items() if k != "sig"}
    expected = sign(payload, key)
    return hmac.compare_digest(expected, cmd.get("sig", ""))


_MACHINE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


def signing_key_for(
    machine: str,
    *,
    default: str | None = None,
    directory: str | Path | None = None,
) -> str | None:
    """Read a target key from configured or systemd credentials; else default."""
    root = directory
    if root is None:
        root = os.environ.get("LUPIN_CMD_SIGNING_KEYS_DIR")
    if root is None:
        credentials_dir = os.environ.get("CREDENTIALS_DIRECTORY")
        if (
            credentials_dir is not None
            and _MACHINE_NAME_RE.fullmatch(machine)
            and (Path(credentials_dir) / machine).is_file()
        ):
            root = credentials_dir
    if root is None:
        return default
    if not _MACHINE_NAME_RE.fullmatch(machine):
        return None
    try:
        key = (Path(root) / machine).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return key or None


def parse_params(pairs: list[str]) -> dict:
    """`["repo=owner/name", "flag=1"]` -> `{"repo": "owner/name", "flag": "1"}`.
    Raises `ValueError` on anything without a `=`."""
    result: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise ValueError(f"expected key=value, got {pair!r}")
        result[key] = value
    return result


def log_event(client, fields: dict) -> None:
    """Append one `cmdlog` entry. Best-effort: a dropped audit line must
    never abort the write it's describing -- the `cmd`/`cmdres` key is
    already the source of truth, this is observability on top of it.
    """
    flat = {k: str(v) for k, v in fields.items()}
    try:
        _call_with_retry(lambda: client.xadd(LOG_KEY, flat, maxlen=LOG_MAXLEN, approximate=True))
    except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError):
        pass


# KEYS[1] = cmd:<id>, KEYS[2] = cmdq:<target>. ARGV[1] = value (JSON),
# ARGV[2] = retention_ms, ARGV[3] = issued_at (score, ms), ARGV[4] = id
# (member). One EVAL instead of a transaction -- MULTI isn't on the ACL
# list. SET's own NX is the only guard against two calls minting the same
# id.
_ENQUEUE_SCRIPT = """
local ok = redis.call('SET', KEYS[1], ARGV[1], 'NX', 'PX', ARGV[2])
if not ok then
    return 0
end
redis.call('ZADD', KEYS[2], ARGV[3], ARGV[4])
return 1
"""


def enqueue(
    target: str,
    action: str,
    params: dict,
    *,
    key: str,
    actor: str,
    issuer: str,
    pickup_window: float = DEFAULT_PICKUP_S,
    redis_host: str | None = None,
    redis_port: int | None = None,
    redis_username: str | None = None,
    redis_password: str | None = None,
) -> str:
    """Sign and enqueue one command for `target`. Returns the new command id.

    `actor` is who asked for this (audit only, per the design -- never used
    to authorize anything; that's the HMAC's job). `issuer` is what sent it
    (a hostname or system name). Raises `CoordinatorUnreachable` if Redis
    can't be reached -- commands have no local fallback, same as claims.
    """
    client = _client(redis_host, redis_port, redis_username, redis_password)
    cmd_id = uuid.uuid4().hex
    issued_at = time.time()
    expires_at = issued_at + pickup_window
    fields = {
        "v": 1,
        "id": cmd_id,
        "target": target,
        "action": action,
        "params": params,
        "actor": actor,
        "issuer": issuer,
        "issued_at": issued_at,
        "expires_at": expires_at,
    }
    cmd = {**fields, "sig": sign(fields, key)}
    value = json.dumps(cmd)
    retention_ms = int(CMD_RETENTION_S * 1000)
    score_ms = int(issued_at * 1000)
    try:
        result = _call_with_retry(
            lambda: client.eval(
                _ENQUEUE_SCRIPT,
                2,
                cmd_key(cmd_id),
                queue_key(target),
                value,
                retention_ms,
                score_ms,
                cmd_id,
            )
        )
    except redis.exceptions.AuthenticationError as exc:
        raise _auth_failed(exc) from exc
    except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as exc:
        raise CoordinatorUnreachable(target) from exc
    if not result:
        raise RuntimeError(f"command id collision: {cmd_id}")  # practically impossible (uuid4)
    log_event(client, {"id": cmd_id, "target": target, "action": action, "actor": actor, "event": "enqueued"})
    return cmd_id


def get_status(
    cmd_id: str,
    *,
    redis_host: str | None = None,
    redis_port: int | None = None,
    redis_username: str | None = None,
    redis_password: str | None = None,
) -> dict | None:
    """The result if one exists yet, else `{"state": "queued", ...}` if the
    command is still waiting, else `None` if neither key exists (never
    existed, or both have expired)."""
    client = _client(redis_host, redis_port, redis_username, redis_password)
    try:
        raw = _call_with_retry(lambda: client.get(res_key(cmd_id)))
        if raw is not None:
            return json.loads(raw)
        raw_cmd = _call_with_retry(lambda: client.get(cmd_key(cmd_id)))
    except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as exc:
        raise CoordinatorUnreachable(cmd_id) from exc
    if raw_cmd is None:
        return None
    cmd = json.loads(raw_cmd)
    return {"id": cmd_id, "state": "queued", "target": cmd.get("target"), "action": cmd.get("action")}


def get_queue(
    machine: str,
    *,
    redis_host: str | None = None,
    redis_port: int | None = None,
    redis_username: str | None = None,
    redis_password: str | None = None,
) -> list[dict]:
    """Every pending id in `machine`'s queue, oldest first, with what's known
    about each (the action/params if `cmd:<id>` still exists, else flagged
    `expired` -- not yet pruned by an `agent` poll)."""
    client = _client(redis_host, redis_port, redis_username, redis_password)
    try:
        members = _call_with_retry(lambda: client.zrange(queue_key(machine), 0, -1, withscores=True))
        entries = []
        for cmd_id, score in members:
            raw = _call_with_retry(lambda c=cmd_id: client.get(cmd_key(c)))
            entry: dict = {"id": cmd_id, "issued_at": int(score)}
            if raw is not None:
                cmd = json.loads(raw)
                entry["action"] = cmd.get("action")
                entry["params"] = cmd.get("params")
            else:
                entry["expired"] = True
            entries.append(entry)
    except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as exc:
        raise CoordinatorUnreachable(machine) from exc
    return entries
