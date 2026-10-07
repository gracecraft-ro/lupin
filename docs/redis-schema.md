# Redis schema (v1)

This is the data model `lupin` uses once the `redis` backend exists
(issue #210). Every key has the prefix `lupin:v1:`.

## Keys

| Key | Type | Holds | Replaces |
| --- | --- | --- | --- |
| `slot:<name>` | sorted set (member = holder, score = expiry in ms) | fleet slots: `bmo` (max 1 holder), and `repo:<repo>` per loopable repo (a declared loop-concurrency cap, not yet enforced by loopctl — issue #23). | `omp.lock` |
| `claim:<owner>/<repo>#<n>` | string (JSON: host, session, since), with a TTL | one claim per GitHub issue | nothing today |
| `ledger:<owner>/<repo>` | stream (`XADD`) | `ts host issue branch status body` | `.loop/loop-state.json`, once more than one host writes it |
| `machine:<name>` | string (JSON), with a TTL | one fleet machine's status — see Fleet keys below | nothing today |
| `focus:<quest>` | string (JSON), no TTL | which machine a quest is pinned to — see Fleet keys below | nothing today |
| `quest:<id>` | string (JSON), no TTL | one quest's issues, order, machine, state — see Fleet keys below | nothing today |
| `seq:quest` | string (integer, via a Lua script) | the counter that mints the next quest ID | nothing today |
| `cmdq:<machine>` | sorted set (member = command id, score = issued_at in ms) | one machine's pending commands, oldest first — see Command queue keys below | nothing today |
| `cmd:<id>` | string (JSON), with a TTL | one signed command — see Command queue keys below | nothing today |
| `cmdres:<id>` | string (JSON), with a TTL | one command's result — see Command queue keys below | nothing today |
| `cmdlog` | capped stream (`XADD ... MAXLEN ~`) | one entry per enqueue and one per terminal outcome — the audit trail | nothing today |

These are new keys for the fleet CLI (issues #6–#14, split from #2) and the
cross-machine command queue (issue #28, split from #27). They stay under
`v1`: `v1` is the shape of each key, not the whole file, and adding a key
doesn't change the shape of any key that already exists.

## Acquire and release

**Acquire** is one Lua script, one atomic call:
1. Drop any holder past its expiry.
2. If the caller already holds the slot, renew its lease.
3. Else, if the slot is under its max, add the caller as a holder.
4. Else, return 0 (busy).

**Release** is a compare-and-delete script. Only the current holder can
release its own entry.

## Fleet keys

These back the fleet CLI (`lupin join`, `heartbeat`, `drain`, `machines`,
`quest ...` — issues #7, #11–#13).

### `machine:<name>`

Written by `lupin join` and `lupin heartbeat`; read by `lupin machines` and
`lupin place` when picking a machine. Renewed every 30s; a machine that
misses two renewals (120s since its `heartbeat` field) counts as offline —
same convention as the `bmo` slot lease (see TTLs below), checked by
comparing a stored timestamp to now, not by Redis expiring the key. The
Redis key itself gets a longer TTL (40 min), only to clean up records for
machines retired long ago — that longer TTL is not what decides
online/offline.

```json
{
  "version": "0.4.0",
  "heartbeat": "2026-10-05T12:00:00Z",
  "state": "online",
  "slots": {"bmo": {"used": 1, "max": 1}},
  "providers": ["claude", "openai"],
  "quota": {
    "claude": {"pct_left": 42, "resets_at": "2026-10-05T18:00:00Z"}
  }
}
```

`state` is `"online"` or `"draining"` (`lupin drain`/`undrain` set it).

### `focus:<quest>`

Written by `lupin quest focus`/`lupin quest release`; read by `lupin quest`
and `lupin place`. No TTL — `quest release` deletes the key outright, so a
stale focus doesn't need to time out.

```json
{
  "machine": "jesus",
  "pinned": false,
  "since": "2026-10-05T12:00:00Z",
  "release_when": "quest done"
}
```

`release_when` is a cached, human-readable guess (idle, stalled, done —
C10's job to compute); `lupin quest` shows it without recomputing it.

### `quest:<id>`

Written by `lupin quest start`/`lupin quest stop`; read by `lupin quest`
and `lupin quest status`. No TTL — `quest stop` deletes the key.

```json
{
  "issues": [11, 12, 13],
  "targets": ["gracecraft/lupin#11", "gracecraft/lupin#12", "gracecraft/lupin#13"],
  "order": [11, 12, 13],
  "waits_on": [{"number": 12, "blocker": 11}],
  "machine": "jesus",
  "state": "running"
}
```

`targets` is `issues` in the same order, each written as the `claim:<...>`
key it maps to (`owner/repo#n`) -- a quest's issues can come from different
repos, so `stop` needs this to find each one's claim. `waits_on` lists each
issue-blocker pair where both are in the quest -- `quest start` prints one
"waits on" line per pair. Omitted when no issue in the quest blocks
another. `platform`/`note` are optional, carried over as-is from `quest
start`'s own flags.

### `seq:quest`

A plain string holding the last-issued quest ID. `INCR` isn't on this
repo's ACL command list (see below), and Redis checks ACL permissions on
commands called from inside a script too — so the script can't just call
`INCR` either. `lupin quest start` mints the next ID with a Lua script via
`EVAL` that reads and writes the counter with `GET`/`SET` only, both
already allowed:

```lua
local n = tonumber(redis.call('GET', KEYS[1]) or '0') + 1
redis.call('SET', KEYS[1], n)
return n
```

No ACL change needed — `GET`, `SET`, and `EVAL` are already on the list.

## Command queue keys

These back the cross-machine command queue (`lupin cmd send|status|queue`,
`lupin agent` — issue #28, implementing #27's design). One machine enqueues
a signed command; the target machine's `lupin agent` process claims and
runs it through a fixed action table (`loop.stop`, `loop.run` in v1, both
wrapping `loopctl`).

### `cmd:<id>`

Written once by `lupin cmd send`, via one `EVAL` that does `SET ... NX`
here and `ZADD cmdq:<target>` together — `MULTI` isn't on the ACL list (see
below), so this is the atomic primitive instead. The `PX` TTL (1h, fixed)
is just retention — how long the record stays around to look up, not
whether the command is still valid to run. That's `expires_at`, a field
inside the JSON, checked on the target host with a 30s clock-skew
allowance.

```json
{
  "v": 1,
  "id": "a1b2c3d4e5f6...",
  "target": "jesus",
  "action": "loop.stop",
  "params": {"repo": "gracecraft/lupin"},
  "actor": "grace",
  "issuer": "pihome",
  "issued_at": 1759708800.123,
  "expires_at": 1759708920.123,
  "sig": "..."
}
```

`actor` is who asked for this (audit only, never trusted for
authorization — that's the HMAC's job). `issuer` is what sent it. `sig` is
HMAC-SHA256 over a canonical JSON encoding (`json.dumps(..., sort_keys=True,
separators=(",", ":"))`) of every other field, keyed by a secret shared
with the target machine only — per-target HMAC, not a Redis ACL selector,
so a compromised host can't forge a command for a different one.

### `cmdres:<id>`

Claimed with `SET ... NX` (first writer wins a race between two pollers
on the same id), then overwritten by the same claimant with the final
result. `state` is one of `queued` (no `cmdres` yet — the `cmd:<id>` key
is the only record), `running`, `ok`, `failed`, `rejected`, `expired`.

```json
{"id": "a1b2c3d4e5f6...", "state": "ok", "host": "jesus", "action": "loop.stop", "exit_code": 0, "output": "...", "truncated": false}
```

A `running` entry still present when `lupin agent` restarts means the
previous process crashed mid-command — the startup scan marks it `failed`
rather than silently re-running it. `output` is the last 8 KiB of combined
stdout+stderr; `rejected`/`failed`-without-a-run carry a `reason` string
instead.

### `cmdlog`

One stream entry per enqueue and one per terminal outcome
(`ok`/`failed`/`rejected`/`expired`) — the audit trail, capped with
`MAXLEN ~ 2000`.

## TTLs

Starting values, not measurements — change them once real use shows better
numbers.

| Resource | Renew every | TTL |
| --- | --- | --- |
| Slot lease | 30s | 120s |
| Claim | 2 min | 10 min |
| Machine heartbeat | 30s | 120s |
| Command record (`cmd:<id>`) | n/a — not renewed | 1 hour (retention only, see below) |
| Command result (`cmdres:<id>`) | n/a — not renewed | 1 hour |

A command's Redis retention (1h) is not the same thing as how long it's
valid to run — that's `expires_at` inside the record (120s after
`issued_at` by default, the "pickup deadline"), checked by `lupin agent`
with a 30s clock-skew allowance. A command can sit in Redis, inspectable,
long after it's stopped being runnable.

## ACL command list

Each host gets its own Redis user (`lupin-jesus`, `lupin-ralpha`,
`lupin-mac`), limited to keys under `~lupin:*` and these commands:

```
PING GET SET DEL PEXPIRE ZADD ZREM ZCARD ZRANGE ZREMRANGEBYSCORE
XADD XRANGE SCAN EVAL EVALSHA SCRIPT|LOAD
```

## Fallback when Redis is unreachable

Connect timeout 2s, 1 retry, then:

| Resource | Fallback |
| --- | --- |
| `bmo` slot | Local lock (`omp.lock`), plus a warning in the journal. Same cross-host risk as today. |
| Claim | `lupin claim` exits 3. The orchestrator starts no new issue, but keeps working anything already in progress. |
| Ledger | `lupin` always writes the local file too. The Redis copy misses the entry — v1 has no replay. |
| Host-scope slot | No change — these never use Redis. |
| Command queue | `lupin cmd send`/`lupin agent` exit 3. No local fallback, same as claims — a command only means anything if the target machine can see it. |

After an outage ends, a holder tries to renew its lease. If the lease
already expired, `lupin` logs "lease lost" and tries to acquire again.

## Persistence

AOF, `appendfsync everysec`, so the claim ledger survives a restart. Leases
still end by TTL, not by the AOF.
