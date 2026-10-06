# Redis schema (v1)

This is the data model `lupin` uses once the `redis` backend exists
(issue #210). Every key has the prefix `lupin:v1:`.

## Keys

| Key | Type | Holds | Replaces |
| --- | --- | --- | --- |
| `slot:<name>` | sorted set (member = holder, score = expiry in ms) | fleet slots. v1 has one: `bmo`, max 1 holder. | `omp.lock` |
| `claim:<owner>/<repo>#<n>` | string (JSON: host, session, since), with a TTL | one claim per GitHub issue | nothing today |
| `ledger:<owner>/<repo>` | stream (`XADD`) | `ts host issue branch status body` | `.loop/loop-state.json`, once more than one host writes it |
| `machine:<name>` | string (JSON), with a TTL | one fleet machine's status — see Fleet keys below | nothing today |
| `focus:<quest>` | string (JSON), no TTL | which machine a quest is pinned to — see Fleet keys below | nothing today |
| `quest:<id>` | string (JSON), no TTL | one quest's issues, order, machine, state — see Fleet keys below | nothing today |
| `seq:quest` | string (integer, via a Lua script) | the counter that mints the next quest ID | nothing today |

These four are new keys for the fleet CLI (issues #6–#14, split from #2).
They stay under `v1`: `v1` is the shape of each key, not the whole file, and
adding a key doesn't change the shape of any key that already exists.

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
`quest ...` — issues #7, #11–#13). No code reads or writes them yet.

### `machine:<name>`

Written by `lupin join` and `lupin heartbeat`; read by `lupin machines` and
`lupin place` when picking a machine. Renewed every 30s, 120s TTL — a
machine that misses two renewals is offline (same convention as the `bmo`
slot lease, see TTLs below).

```json
{
  "version": "0.4.0",
  "heartbeat": "2026-10-05T12:00:00Z",
  "state": "online",
  "slots": {"bmo": {"used": 1, "max": 1}},
  "providers": ["anthropic", "openai"],
  "quota": {
    "anthropic": {"percent_left": 42, "reset": "2026-10-05T18:00:00Z"}
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
  "order": [11, 12, 13],
  "machine": "jesus",
  "state": "running"
}
```

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

## TTLs

Starting values, not measurements — change them once real use shows better
numbers.

| Resource | Renew every | TTL |
| --- | --- | --- |
| Slot lease | 30s | 120s |
| Claim | 2 min | 10 min |
| Machine heartbeat | 30s | 120s |

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

After an outage ends, a holder tries to renew its lease. If the lease
already expired, `lupin` logs "lease lost" and tries to acquire again.

## Persistence

AOF, `appendfsync everysec`, so the claim ledger survives a restart. Leases
still end by TTL, not by the AOF.
