# Redis schema (v1)

This is the data model `lupin` uses once the `redis` backend exists
(issue #210). Every key has the prefix `lupin:v1:`.

## Keys

| Key | Type | Holds | Replaces |
| --- | --- | --- | --- |
| `slot:<name>` | sorted set (member = holder, score = expiry in ms) | fleet slots. v1 has one: `bmo`, max 1 holder. | `omp.lock` |
| `claim:<owner>/<repo>#<n>` | string (JSON: host, session, since), with a TTL | one claim per GitHub issue | nothing today |
| `ledger:<owner>/<repo>` | stream (`XADD`) | `ts host issue branch status body` | `.loop/loop-state.json`, once more than one host writes it |

## Acquire and release

**Acquire** is one Lua script, one atomic call:
1. Drop any holder past its expiry.
2. If the caller already holds the slot, renew its lease.
3. Else, if the slot is under its max, add the caller as a holder.
4. Else, return 0 (busy).

**Release** is a compare-and-delete script. Only the current holder can
release its own entry.

## TTLs

Starting values, not measurements — change them once real use shows better
numbers.

| Resource | Renew every | TTL |
| --- | --- | --- |
| Slot lease | 30s | 120s |
| Claim | 2 min | 10 min |

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
