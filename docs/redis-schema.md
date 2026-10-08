# Redis schema (v1)

This is the data model `lupin` uses once the `redis` backend exists
(issue #210). Every key has the prefix `lupin:v1:`.

## Keys

| Key | Type | Holds | Replaces |
| --- | --- | --- | --- |
| `slot:<name>` | sorted set (member = holder, score = expiry in ms) | fleet slots: `bmo` (max 1 holder), and `repo:<repo>` per loopable repo (a declared loop-concurrency cap, not yet enforced by loopctl — issue #23). | `omp.lock` |
| `claim:<owner>/<repo>#<n>` | string (JSON: host, session, since), with a TTL | one claim per GitHub issue | nothing today |
| `ledger:<owner>/<repo>` | stream (`XADD`/`XRANGE`) | `ts host event issue status branch summary highlights evidence decisions next children` | `.loop/loop-state.json` is ignored; no local fallback |
| `machine:<name>` | string (JSON), with a TTL | one fleet machine's status — see Fleet keys below | nothing today |
| `focus:<quest>` | string (JSON), no TTL | which machine a quest is pinned to — see Fleet keys below | nothing today |
| `quest:<id>` | string (JSON), no TTL | one quest's issues, order, machine, state — see Fleet keys below | nothing today |
| `seq:quest` | string (integer, via a Lua script) | the counter that mints the next quest ID | nothing today |
| `cmdq:<machine>` | sorted set (member = command id, score = issued_at in ms) | one machine's pending commands, oldest first — see Command queue keys below | nothing today |
| `cmd:<id>` | string (JSON), with a TTL | one signed command — see Command queue keys below | nothing today |
| `cmdres:<id>` | string (JSON), with a TTL | one command's result — see Command queue keys below | nothing today |
| `cmdlog` | capped stream (`XADD ... MAXLEN ~`) | one entry per enqueue and one per terminal outcome — the audit trail | nothing today |
| `benchmark-snapshot` | string (JSON), with a TTL | one fleet-wide benchmark/quality score per model ID — see Fleet keys below | nothing today |
| `model-snapshot` | string (JSON), with a TTL | latest model list and prices from `lupin fetch-models` | nothing today |
| `gh-cache:<owner>/<repo>:<cache-key>` | string (JSON: `{"data": ...}`), with a TTL | one read-only `gh` lookup's cached result — see Fleet keys below | each machine's own direct `gh` call for the same lookup |
| `quota-snapshot` | string (JSON), with a TTL | one fleet-wide quota reading per provider — see Fleet keys below | nothing today |
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

## Repository ledger

`lupin ledger append OWNER/REPO` adds one event to the shared stream.
`lupin ledger read OWNER/REPO` returns up to the latest 10 events, oldest
first. Use `--limit N` to choose another positive count. Add `--json` for a
JSON array. An empty stream returns `[]`.

Each event has a UTC `ts`, a `host`, and an event name. Optional fields are
an issue number, status, branch, summary, and digest lists: highlights,
evidence, decisions, and next steps. `children` is a list of issue numbers
split from the event's issue. List fields are JSON arrays in Redis.

Use these commands to write and read events:

```sh
lupin ledger append OWNER/REPO --event handoff --issue 42 \
  --status done --branch BRANCH --summary TEXT \
  --highlights TEXT --evidence TEXT --decisions TEXT --next TEXT \
  --child 43
lupin ledger read OWNER/REPO --limit 50 --json
```

The Python API accepts `limit=None` to read all events. The roadmap does this
because older events can hold the current status for an issue.

Ledger commands exit 3 when Redis is unavailable. The roadmap then shows no
ledger annotations and adds a warning. Lupin does not read or copy the old
`.loop/loop-state.json` file.

The roadmap reads the stream on each page load. It reloads about every five
minutes while open, so new events appear without a manual refresh.

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
  },
  "repos": [{"repo": "field-trip", "enabled": true, "loopable": true}],
  "actions": ["loop.peek", "loop.run", "loop.stop", "schedule.pause", "schedule.resume", "schedule.set", "schedule.show"]
}
```

`state` is `"online"` or `"draining"` (`lupin drain`/`undrain` set it). A
draining machine's `agent.py` still accepts actions in its own
`DRAIN_ALLOWED` set (`loop.stop`, `loop.peek`, `schedule.show`,
`schedule.pause`) but rejects the rest -- it can wind work down or read
state, not start anything new. `loop.run` starts a loop outright;
`schedule.set`/`schedule.resume` arm a timer to start one later, which
still counts as new work, just deferred.

`actions` is this machine's `agent.py` `ACTIONS` table (issue #27/#28),
written by `_write_record` so it can never list an action the agent here
doesn't actually run.

`repos` lists directories under `/code` on this machine. Each item has a
repo name, whether it is enabled, and whether it has a loop doc. The
dashboard combines these lists. Each machine must run the Lupin version
that sends this field before its repos appear.

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

### `benchmark-snapshot`

Written by `lupin fetch-benchmarks` (issue #17's reopen; see
`benchmark_fetch.py`); read by `lupin fetch-benchmarks` and the Models
page. One key, fleet-wide — a benchmark score doesn't depend on which
machine looked it up, unlike `model:<name>`'s per-machine model-fetch
snapshot, so there is no `benchmark-snapshot:<machine>` variant.

```json
{
  "fetched_at": "2026-10-07T12:00:00+00:00",
  "live": true,
  "source": "claude -p sonnet, web search/fetch",
  "scores": [
    {"id": "claude-opus-4-5", "score": 73.1, "scale": "Artificial Analysis Intelligence Index (0-100)",
     "source": "https://artificialanalysis.ai/models/claude-opus-4-5", "as_of": "2026-10-07"},
    {"id": "gpt-5.4", "score": null, "reason": "not found"}
  ]
}
```

`live: false` carries a `stale_reason` instead of a `source`/`scores` list
with real entries — the dispatched agent timed out, exited non-zero, or
returned something that didn't match its schema. TTL is retention only
(7 days, `benchmark_fetch.REDIS_KEY_TTL`) — freshness is decided by
comparing `fetched_at` to a 20-hour window
(`benchmark_fetch.CACHE_FRESH_SECONDS`), not by the key expiring; the
same "stored timestamp, not Redis TTL" convention as `machine:<name>`'s
offline detection above.

Only one machine fetches at a time: `lupin fetch-benchmarks` wraps the
actual dispatch in `slot:benchmark-fetch` (max 1 holder, no renewal —
see `benchmark_fetch.py`'s docstring for why a lease TTL alone, not a
renew timer, is what recovers this slot if its holder crashes mid-fetch).
A machine that finds the slot already held just reads whatever is in
`benchmark-snapshot` right now instead of waiting.

### `gh-cache:<owner>/<repo>:<cache-key>`

Backs `place`/`quest`/`roadmap`'s read-only `gh` lookups (issue #35):
issue state, issue body/labels, quest-labeled issues, dependency links.
Written only by `pihome` (the fixed value of `gh_cache.CANONICAL_GH_FETCHER`
— a hard pin to one named machine, not a race any machine could win). Read
by every machine, `pihome` included.

```json
{"data": {"number": 42, "state": "OPEN"}}
```

`data` is whatever the wrapped `gh` call returns, wrapped so a legitimate
`null` result (e.g. "this issue does not exist") is still a cache hit, not
indistinguishable from "nothing cached yet".

TTL 5 minutes — long enough that a burst of `place`/`quest`/`roadmap` calls
across several machines shares one fetch, short enough that a placement or
quest decision isn't made against issue data that's badly stale.

`<cache-key>` names which lookup, since the same repo backs several
different queries that must not share one cache entry: `issues:<state>`
(issue list), `comments:<state>` (per-issue comment pagination),
`dependencies` (blockedBy/blocking links), `quests` (quest-labeled issues),
`issue:<number>` (`place`'s single-issue lookup), `issue-state:<number>`
(`roadmap_cli`'s blocker-exists check), `quest-locate-issue:<number>`
(`quest`'s issue-to-repo lookup).

Guarded by the existing `slot:<name>` shape above, as
`slot:gh-fetch/<owner>/<repo>` (max 1 holder, `/` not `:` -- a colon in the
slot name breaks `release`/`renew`'s lease-id parsing, which splits on the
first colon) — this only stops `pihome` from running the same fetch twice
if two `lupin` invocations there race each other. A non-`pihome` machine
never takes this lock and never calls `gh` for these lookups at all; on a
miss it reports "no data yet" instead.

### `quota-snapshot`

Backs `lupin quota` and `serve.py`'s `/usage` page (issue #38). Quota is
one shared account per provider (Claude, opencode-go, OpenAI/Codex — issue
#36), so one real reading per provider is the fleet's answer, not
something to merge across machines.

```json
{
  "claude": {
    "rows": [{"provider": "claude", "duration": "PT5H", "used_pct": 42, "resets_at": 1_790_547_474_348}],
    "fetched_at": "2026-10-07T12:00:00+00:00",
    "fetched_by": "jesus"
  },
  "openai": {
    "rows": [{"provider": "openai", "duration": "PT5H", "used_pct": 10, "resets_at": 1_790_550_000_000}],
    "fetched_at": "2026-10-07T11:58:00+00:00",
    "fetched_by": "mini"
  }
}
```

One key, one entry per provider — each provider goes stale/fresh on its
own, so each carries its own `fetched_at`/`fetched_by`. `rows` is
`quota.quota_usage()`'s own row shape for that provider (one row per
window: 5 hours, 7 days, 30 days), kept whole rather than collapsed to one
number, so a reader can show duration, percent left, and time to reset —
not just one of them. A provider's `duration` is written as the plain
string `quota.QuotaDuration`'s own value serializes to (`"PT5H"` etc.);
`quota_cache.py` converts it back to the real enum on read.

Unlike `gh-cache`'s fixed `pihome` pin, there is no fixed canonical
machine here: credentials for different providers can live on different
machines, unknown in advance. Instead, whichever machine's own local
`quota.quota_usage()` call actually returns a real `used_pct` for a
provider is treated as that provider's fetcher for this round — a machine
with no credentials for a provider never has real data for it, so it
never writes for it, and can never clobber a good reading from elsewhere.

TTL on the key is retention only (24 hours) — freshness is judged per
provider, by comparing that provider's own `fetched_at` to 5 minutes
(`quota_cache.CACHE_TTL`), the same "stored timestamp, not Redis TTL"
convention `benchmark-snapshot`/`machine:<name>` already use. A provider
entry older than that is still shown (better than nothing) but is no
longer trusted to trigger a skip — the next `lupin quota` run on a
credentialed machine republishes it.

Guarded by `slot:quota-fetch/<provider>` (max 1 holder, non-blocking, no
wait) — only stops two `lupin` processes on the *same* machine from
publishing the same provider at once, same narrow job the `gh-fetch`/
`benchmark-fetch` locks do.

## Command queue keys

These back the cross-machine command queue (`lupin cmd send|status|queue`,
`lupin agent` — issue #28, implementing #27's design). One machine enqueues
a signed command; the target machine's `lupin agent` process claims and
runs it through a fixed action table: `loop.stop`, `loop.run`, `loop.peek`,
`schedule.show`, `schedule.set`, `schedule.pause`, `schedule.resume` (issue
#2 phase A), all wrapping `loopctl`.

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
| `slot:benchmark-fetch` lease | n/a — not renewed | ~7 min (`benchmark_fetch.CLAUDE_TIMEOUT` + 60s headroom) |
| `benchmark-snapshot` | n/a — not renewed | 7 days (retention only, see below) |
| `model-snapshot` | n/a — not renewed | 7 days (retention only) |
| GitHub data cache (`gh-cache:...`) | n/a — not renewed | 5 min |
| GitHub fetch lock (`slot:gh-fetch/<owner>/<repo>`) | n/a — held only for one fetch | 2 min |
| `quota-snapshot` | n/a — not renewed | 24 hours (retention only; freshness is per-provider, 5 min, see above) |
| Quota fetch lock (`slot:quota-fetch/<provider>`) | n/a — held only for one fetch, no wait | 1 min |

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
| Benchmark snapshot | `lupin fetch-benchmarks` reports `live: false` with a `stale_reason` (exit 0, same convention as `model_fetch.py`'s own failure cases — see `cli.py`'s exit-code table, "any other error" doesn't fit this, it's a data-availability fact, not a usage error). No local fallback — a fleet-shared cache has nothing meaningful to fall back to on one machine. |
| GitHub data cache | `pihome` calls `gh` directly anyway (it just can't publish for other machines). Every other machine reports "no data yet" instead of calling `gh` itself — no direct-call fallback here, unlike the resources above. |
| Quota snapshot | A machine with real provider credentials still returns its own live reading (it just can't publish for other machines). A machine with no credentials for a provider has nothing to fall back to and reports "no data cached yet" for it. |

After an outage ends, a holder tries to renew its lease. If the lease
already expired, `lupin` logs "lease lost" and tries to acquire again.

## Persistence

AOF, `appendfsync everysec`, so the claim ledger survives a restart. Leases
still end by TTL, not by the AOF.
