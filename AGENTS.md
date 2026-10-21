# lupin

`lupin` is a tool for multi-machine task loops. It does four jobs:

1. It picks a model and effort level for a task. Commands: `route`,
   `classify`, `review-route`. `fetch-models` fetches a daily snapshot of
   which models each subscription can call today, and their prices.
2. It controls slots. A slot is a resource with a limit on how many
   callers can use it at one time. Commands: `acquire`, `hold`, `release`,
   `status`.
3. It serves a dashboard for the loops, for both viewing and controlling
   them (start/stop/close a loop, add or remove a repo, trigger a run,
   adjust a machine's slots). Command: `serve`.
4. It coordinates a fleet of machines: issue claims, machine records, quests
   (sets of issues worked together on one machine), scheduled runs, and shared
   repository events. Commands: `claim`, `renew-claim`, `release-claim`, `join`,
   `heartbeat`, `drain`, `undrain`, `machines`, `place`, `quest`, `reconcile`,
   `roadmap`, `ledger`, `fleet-run`.
   Loop lifecycle: `run`, `once`, `enable`, `disable`, `loops`, `stop`, `peek`,
   `attach`, `schedule`, `pause`, `resume`, `supervise`.

One program, `lupin`, with subcommands. Run `lupin --help` for the full list.

## Commands

```
lupin route <category> <size> [--no-bmo] [--primary-effort E] [--json]
lupin classify --issue-json FILE [--diff-stat FILE] [--json]
lupin fetch-models [--snapshot-file FILE] [--no-write] [--json]
lupin fetch-benchmarks [--force] [--json]
lupin quota [--json]
lupin review-route (--category C --size S | --issue-json FILE) [--mode M]
1: lupin review-route --prefetch N[,N...] [--repo OWNER/REPO] [--json]
lupin pr-status [--repo OWNER/REPO] [--json] [--full]
lupin acquire <slot> --holder H [--wait SECONDS] [--max N] [--ttl SECONDS]
lupin hold (--lease ID | <slot> --holder H --wait S) [--ttl SECONDS] -- <command>
lupin release --lease ID
lupin status [--json]
lupin dispatch OWNER/REPO#N [--holder HOLDER] [--json] [--no-start]
lupin ledger append OWNER/REPO --event EVENT [--issue N] [--status S] \
  [--branch B] [--summary TEXT] [--highlights TEXT] [--evidence TEXT] \
  [--decisions TEXT] [--next TEXT] [--child N] [--json]
lupin ledger read OWNER/REPO [--limit N] [--json]
lupin serve [--bind 127.0.0.1] [--port 8788] [--roadmap REPO]
lupin agent [--machine M] [--batch N] [--poll-interval S]
lupin run <repo> [--machine M] [--platform claude|omp] [--provider P] [--model M] [--note TEXT] [--resume]
lupin fleet-run [--json]
lupin supervise [--every [SECONDS]] [--dry-run] [--json]
lupin once <when> [repo ...] [--platform claude|omp] [--provider P] [--model M] [--note TEXT] [--resume]
lupin enable <repo> [--platform claude|omp] [--orchestrator SELECTOR ...] [--clear-orchestrators]
lupin disable <repo>
lupin loops [repo] [--machine M] [--json]
lupin stop <repo> [--machine M] [--force] [--wait S] [--json]
lupin peek <repo> [LINES] [--machine M] [--json]
lupin attach <repo> [--machine M] [--print]
lupin schedule [--machine M] [--json]
lupin schedule cal <expr> [--machine M] [--json]
lupin schedule first <when> every <interval> [--machine M] [--json]
lupin pause [--machine M | --all] [--json]
lupin resume [--machine M | --all] [--json]
```

`route` and `classify` print a plain result by default (`model effort`, or
`category size`). Add `--json` for a JSON object instead. If the
category or size is not known, `route` and `review-route` print one line to
stderr. The line lists the valid values. The exit code is 2.

`ledger append` stores events in Redis. It records the time and host. Repeat
digest options to add more than one item. Repeat `--child` for each split
issue. `ledger read --json` returns up to the latest 10 events, oldest first.
Use `--limit N` to choose another positive count. `_decode` in
`src/lupin/ledger.py` turns one entry's ID and decoded fields into a dict.
`read_events` decodes each entry on its own. It skips an entry when a
field is not valid UTF-8. It also skips an entry when `_decode` raises
KeyError, ValueError, or RecursionError. `_decode` raises ValueError when
a list field nests brackets more than 8 levels deep. A real field has 1 level.
The check does not use the C stack, so all platforms give the same result.
It reports each skipped entry on stderr. A skipped entry still counts toward
the limit. Both commands exit 3
if Redis is unavailable. When Redis is unavailable, the roadmap shows no
ledger annotations and a warning. It ignores `.loop/loop-state.json`.

`review-route --prefetch` fetches issue/PR text up front -- body, comments,
and (for a PR) reviews and a diff stat (changed files, additions/deletions,
no diff text). It prints markdown by default, one `brief_markdown` section
per item separated by a blank line. Add `--json` for the keyed object,
trimmed to the fields a worker reads: comments keep
`author.login`/`createdAt`/`body`, reviews keep
`author.login`/`state`/`submittedAt`/`body`, and the item keeps `kind`,
`body`, and `diff_stat`. A number that is neither an issue nor a PR gets
`{"error": ...}` in its own slot instead of failing the whole batch.

`dispatch OWNER/REPO#N` claims the item, writes a markdown brief with its
body and every comment (plus reviews and changed files for a PR) under the
loop state directory's `notes/`, and starts the loop with that brief. Add
`--no-start` to only write the brief; the claim stays with the holder.
`--json` prints the brief's path and the claim. A brief over 8,000
characters is passed to the loop as a short note naming its path, because
the loop's note field has a cap. The brief is never committed to the
repository.

`acquire` prints a lease ID on success. Exit code 2 means the slot is full.
The caller must skip and try again later. Exit code 3 means the `redis`
backend cannot reach the coordinator. The slot has no local fallback.

Only the `bmo` slot falls back to `local`. If Redis is unreachable, `acquire`,
`renew`, `release`, and `status` use `local` for `bmo` and print a warning.
A refused login never falls back. It raises `CoordinatorAuthFailed` for every
slot. An ACL denial raises `NoPermissionError` for every slot. It never falls back.

For a refused login or an ACL denial, the `lupin` command exits with code 3 and
prints one line that names the setting to check. For a refused login, the setting
is `--redis-password` or `LUPIN_REDIS_PASSWORD`. For a fleet command, it also names
the systemd credential `redis-password`. The line never shows a password.
Some read paths are not covered yet. See `docs/redis-schema.md`.

The `local` backend's coordinator is the filesystem, so it never returns 3.

`hold` acquires a lease (or reuses one from `--lease`), runs `<command>`,
renews the lease while the command runs, and releases it when the command
ends — on a normal exit, a non-zero exit, or the command being killed by a
signal.

`fetch-models` checks which model IDs each subscription can call today and
what they cost. It saves the result to
`~/.local/state/lupin/model-snapshot.json` unless `--no-write` is given. It
also publishes the snapshot to Redis, so the dashboard shows pulls
from other fleet machines. A Redis error leaves the local file in place and
prints a warning. Model lists for opencode-go and claude come from live
provider calls. For opencode-go, `fetch-models` finds the API key in the same
order as `quota` (see the opencode-go key order below). It never prints or
logs the key. If the key is expired, the row says "OpenCode Go API key
expired". Codex uses `omp models` when available; otherwise it uses
models.dev and is marked `live: false`. Prices
come from models.dev, matched by model ID. A model with no match has
`price: null`. Promo pricing has no live source, so `promo` stays `null`.

A subscription whose fetch fails — an expired OAuth token, a missing key —
does not empty its row in the fleet snapshot. The fetch keeps that
subscription's last verified list, marks it `live: false` with the reason,
and the Models page shows the rows with that reason under each one.

`run <repo>` starts a Lupin worker and a Herdr workspace. The agent works
in a new Git worktree, not in the main checkout (see "State"). `run` refuses
to start if the main checkout has an unfinished Git operation. `run --all`
starts every enabled repo. Add `--machine M` to use the signed fleet queue
on another machine; `run --all --machine M` starts its enabled repos.

Each run has one claim holder: `<host>:<repo>:<run branch>`. The agent's
shell gets it as `LUPIN_CLAIM_HOLDER`. `claim`, `renew-claim`, and
`release-claim` use it when you do not give `--holder`. The monitor renews
each claim of that holder every 2 minutes, until the run's workspace closes
or the Herdr server stops. It also renews while the agent waits for input
(state `done`). It renews no claim of another holder. When the renewals
stop, the claims expire within 10 minutes. So a finished loop whose
workspace is still open keeps its claims until `lupin stop`. A renew error
does not stop the loop. The monitor log gets one line for each new error.

Without a profile, Lupin uses the repo's configured platform. To let Lupin
choose among orchestrators, add a profile when you enable the repo. Repeat
`--orchestrator` for each allowed choice:

```
lupin enable my-repo --platform omp \
  --orchestrator openai/gpt-5 \
  --orchestrator opencode-go/beam-free:xhigh \
  --orchestrator claude
```

Profiles are local to each worker. Set them on each worker that can start the
repo. At loop start, Lupin uses quota data from the shared cache. The data
must be less than five minutes old and include usable quota for a candidate.
Lupin skips blocked providers, then picks the candidate with the most quota
headroom across its known windows. For a provider with more than one login,
headroom is that of the best login that is not blocked. Profile order breaks
ties. If no candidate
has fresh, usable quota, Lupin refuses to start the loop.

Explicit `--platform`, `--provider`, or `--model` options select a fixed
orchestrator for that run and skip profile routing. For OMP, use
`--platform omp` and optionally `--provider openai-codex|opencode-go` or
`--model MODEL`. `openai` and `openai/MODEL` remain accepted aliases for
`openai-codex` and `openai-codex/MODEL`. For example, use
`--model openai-codex/gpt-5.6-luna` or
`--model opencode-go/beam-free:xhigh`. Set auto-compaction for the
selected model in OMP, near 200k tokens. Lupin passes the model to OMP; it
does not set or check OMP's compaction settings. These options work with
`run` and `once`; future one-off runs keep them. `run --all` applies them to
each repo.
`once now` starts the listed repos. If you omit repos, it starts every
enabled repo. A future `once` run uses the same default. It fails if no repo
is enabled. `schedule first` sets the first timer run. Use `+2h` for a
relative time or a calendar expression such as `tomorrow 09:00`. The
interval controls later runs.
`disable` also removes the repo's orchestrator profile. Use
`--clear-orchestrators` with `enable` to clear a profile without disabling the repo.

`fleet-run` sends each enabled repo to one online worker. It skips the local
machine, workers with an active loop for that repo, and workers without that
repo's checkout.
Fleet and dashboard run-now actions use the target worker's local platform
and orchestrator profile. Configure the profile on each worker that can start
the repo.

The coordinator needs one signing key file per worker. Lupin reads these
files from `LUPIN_CMD_SIGNING_KEYS_DIR`, or from systemd's
`CREDENTIALS_DIRECTORY` when the first variable is unset. Each file name is
the worker name. Each worker needs `lupin agent` and its matching key. The
command queues runs but does not wait for them to start.

`supervise` checks the Lupin loops on this machine. It uses fixed rules and
no model. For each loop, it reads the Herdr state of the loop's pane, the
Lupin loop state, the systemd unit, the last 20 pane lines and the newest
handoff from this run. If Herdr does not list the loop's own pane, the Herdr
state is `unknown`. It gives each loop one verdict. The first rule that
matches gives the verdict:

1. `stopped`, reason "waiting for the owner": Herdr says `blocked`, or the
   loop needs attention (Lupin state `needs_attention`). It flags the loop. It
   does not send a nudge.
2. `stopped`: work is left and the unit is `inactive` or `failed`. It flags
   the loop. It sends the nudge only when Herdr says `idle` or `done`.
3. `busywork`: Herdr says `working` and the pane text is the same as in the
   last pass. It flags the loop. It does not send a nudge.
4. `idle`: work is left and Herdr says `idle` or `done`. It sends the nudge.
   If the pane or the handoff contains "tell me if you want" or "if you want
   me to", the verdict is `stopped` and it sends the nudge.
5. `repeated`: the last 3 ledger events (not `supervise`) have one issue and
   one status. It flags the loop. It does not send a nudge. Other machines
   write to the same ledger, so rule 4 comes first.
6. `ok`: none of the above.

Work left is the count of `ready` issues from `lupin roadmap` (open, not
claimed, not blocked) plus the count of open pull requests that are not
drafts. An open pull request that waits for the owner to merge it also counts,
so `--every` does not end while a repo has one. If Redis refuses the claim read
with a permission error, each issue counts as not claimed, and the event says
`claims: unreadable`. If a count cannot be read, work left is not known. Then
rules 2 and 4 do not apply, and `--every` does not end. The pull request count
uses the shared `gh` cache key `open-prs`. Only `supervise` fills that key. If
`LUPIN_GH_CANONICAL_FETCHER` names a machine that does not run `supervise`,
the other machines cannot read the count, and their work left is not known.

The nudge is a fixed text that starts with `Supervisor:`. Lupin sends it to the
loop's pane ID with `herdr agent prompt`. A loop with no pane ID gets no nudge.
Before it sends, the supervisor takes the loop's lock without a wait. If
`lupin stop` holds the lock, it skips the nudge (`nudge=busy`). Under the lock,
it reads the loop state again and runs `herdr agent get` on the pane. If the
loop is not `running` in the same pane, or the agent is not `idle` or `done`
now, it skips the nudge (`nudge=stale`). Only one pass runs at a time. A second pass
prints "another supervise pass is running" and exits 0. A `--dry-run` pass does
not take this lock. A loop gets at most one nudge try in 3600 seconds. A failed
try also counts. If the clock goes back, a stored time in the future counts as
recent while it is less than 3600 seconds ahead.

The supervisor writes one `supervise` event to the ledger for each repo. The
event has no pane or handoff text. The supervisor module
itself calls only `systemctl is-active`, `herdr agent prompt`, `herdr agent
get` and, through the shared `gh` cache, `gh pr list`. A test checks this. It also uses Lupin's read
functions, which run read-only `herdr`, `gh` and `git` commands. It keeps a hash
of each pane and the time of each nudge try in
`$XDG_STATE_HOME/lupin/supervise.json` (default
`~/.local/state/lupin/supervise.json`).

Without `--every`, it makes one pass. With `--every`, it makes a pass every
SECONDS (default 3600, minimum 600) and ends on the first pass with no work
left. With no loops on the machine, it prints "no loops on this machine" and
exits 0. `--dry-run` sends no nudge and writes no ledger event, state file or
lock file. It shows `nudge=would-be-stale` when the loop is not `running` in
the same pane. It still reads Redis for claims, the ledger and the `gh` cache. Exit
code 3 means a ledger event was not written because Redis is unavailable, or
Redis refused the login. A host timer can
run `lupin supervise` once an hour. This repo does not ship one.

On the dispatch host, the systemd timer runs `fleet-run` every 5h15m. The
dashboard can start or stop that timer.

The dispatch host sends work to other machines. It does not run loops, and it
has no Herdr. Only worker machines run loops.
Use the dashboard to see all machines. Use Herdr on a worker to see its
loops.

`loops` reads Herdr's agent list and Lupin's saved loop state. Saved state
remains visible if Herdr is unavailable. It groups agents by repo and reports
their panes. `idle` and `working` mean `running`; `blocked` means
`needs_attention`. `peek` can read a discovered agent pane. The command
reports backend, session, workspace and pane IDs. It does not infer state
from pane text.
Each machine has one Herdr session, `lupin-loops`, and one Herdr server.
Each run has one workspace in that session. Each workspace gets its
repo's GitHub token as `GH_TOKEN`. `stop` closes the workspace and leaves
the session running. The loops on a machine share the server's memory.
Loops that started before this change keep their own session until you
stop them. A completed workspace stays open for review and blocks another
run until you stop it. A Herdr state of `needs_attention` means the
workspace needs review. If Herdr cannot provide state, Lupin reports
`unknown`. Both states block a
new run until you resolve the state.

`run`, `stop`, `peek`, `schedule`, `pause`, and `resume` can target any
fleet machine. Local actions run through Lupin. Remote actions use the signed
Redis queue and wait up to `--wait` seconds (default 20). The target must run
`lupin agent` with Redis access and its own signing key. The sender also needs
that key. Inject it only into the Lupin process with protected secret
management. Do not pass it in command arguments or save it in global
environment settings. Exit code 4 means the result is unknown after the
wait; check it with `lupin cmd status <id>`. `stop`, `peek`, and `attach`
need `--machine` if Lupin cannot find one machine for the repo.

`stop` asks the agent to run `/handoff` before it closes anything. It waits up
to 10 minutes for the agent to finish. Then it saves a report, closes the
workspace, and stops the worker. Then it removes the run's worktree and branch,
if git allows it (see "State"). If the wait ends first, `stop` continues and
says so in its output. If Herdr cannot send the request (for example, the
agent waits for your answer), `stop` closes nothing and exits non-zero. Use
`--force` to stop at once without a handoff. A remote `stop` can take longer
than `--wait`. Then it exits with code 4. Check it with `lupin cmd status <id>`.
The dashboard Stop button always uses `--force`. Its request cannot wait 10
minutes.

`attach` never uses the queue. It opens Herdr here or connects to the remote
Herdr server over SSH. `--print` shows the command instead of running it.
Remote attach needs a local SSH target in `~/.config/lupin/ssh-targets`, one
`<machine> <target>` pair per line. Blank lines and `#` comments are ignored.
Add one line for each worker machine. Do not add the dispatch host. It has no
Herdr:

```
worker-a  user@host-a
worker-b  user@host-b
```

This file is local config, not fleet state. Herdr must be installed on both
machines. SSH must allow key-based access. A missing target makes `attach`
fail instead of guessing a hostname.

To also use these machines in Herdr itself, run `herdr machine add <target>`
for each one. `herdr machine list` shows the saved machines.

Debriefs stay as files on the worker that wrote them. The agent also puts each
debrief in Redis with a digest. The digest is a short summary that a model
writes (`src/lupin/digest.py`). `lupin route prose size-s` picks the model. A
digest says when data is missing. It does not call missing data an empty
result. A period with no change gets no digest (the page says "no changes").
A digest is a short story with up to 3 pictures, not a list of pull requests.
Periodic debriefs use fixed windows on the Los Angeles clock, so every machine
computes the same window. One machine in the fleet writes each window: the first
to claim it in Redis. Only the 12-hour and 7-day windows get a digest, so a repo
has one digest per window, not one per machine. The other debriefs (6h, 24h,
loop stops) are records.
The Debriefs page (`/debrief`) shows the debriefs of all workers, so the
dispatch host shows them too. Redis holds text only. A screenshot file stays on its
worker. A GitHub attachment image is shown through the dashboard.
The digest uses these models in this order: bmo (`bmo:qwen3.8-flash-next`,
tier0 in `model-tiers.json`), then `opencode-go/deepseek-v4.1-flash`, then
Claude (`sonnet`). The worker reserves the fleet `bmo` slot and waits up to
300 s, because bmo can sleep. A failed or out-of-quota model moves to the next
one. Quota and rate-limit errors do not use a try; other failed model calls
count (3 tries at most).
If every model is blocked, the debrief shows with no digest and Lupin tries
again later.

`fetch-benchmarks` gets public benchmark scores. It does not run models.
The lookup worker is a free opencode-go model (`beam-free`) run
through `omp -p` with maximum thinking. It reads each model's own
Artificial Analysis page directly -- the page states the Intelligence
Index score in plain text, and one page read costs no search quota --
and falls back to web search for a model the site does not list. The
model list is split into calls of 6 models, up to 2 at a time, and each
score is cached with its own timestamp, so a refresh asks only about the
models whose score is missing, unscored, or older than 20 hours, and a
failed call loses only its own models. A lookup that finds nothing does
not erase a verified score. Use `--force` to fetch everything now.
Plain output lists each unscored model and the reason from the agent.
Model IDs come from the latest `fetch-models` snapshot. If that snapshot
is missing or empty, Lupin uses the active picks in `model-tiers.json`.

A fresh result may include a short, source-linked note about a model's
publicly reported strengths or limits. The agent gets zero-price model IDs
only when a live model snapshot reports zero input and output cost.

The Models page shows scores from public sources by task category. It uses
exact model IDs only. It does not copy a provider-family score to each model.
Missing cells mean no verified score is available. The Notes column shows
source-backed public observations. Older snapshots may not include notes.
The page does not show private test results.
"Perf" is each model's average score as a percent of the best score in each
benchmark, averaged equally across scored categories. "Value" is Perf divided
by the mean input and output price. Future Lupin tests may run one model at a
time. The page does not run private model tests.

`quota` reads provider quota on machines with provider access and publishes
it to shared Redis. The `/usage` page reads these shared rows. It also reads
each machine's local 7-day token and cost totals, stored in separate
per-machine Redis snapshots. The page adds totals by provider and marks a
snapshot stale after five minutes. Each snapshot expires after 24 hours.
Systemd runs `lupin quota` every five minutes on each worker machine.

For opencode-go, Lupin looks for the API key in this order:
`~/.local/share/opencode/auth.json`, the `OPENCODE_API_KEY` variable, then the
output of `omp token opencode-go` (it waits 10 seconds at most). Lupin never
prints or logs the key. A provider with no cached usage rows shows its note
instead, for example "OpenCode Go API key expired" or "no OpenCode Go API key
available". `lupin quota` shows the note in plain output and in `--json`.
`lupin quota` does not publish a provider that has only notes.

A provider can have more than one login, for example a free and a paid
OpenAI account. `omp usage` reports each login. Lupin adds an account
label when a provider has more than one login. The label is the plan name
(`free`, `plus`). When two logins have the same plan, the label also has a
short hash of the email and the account ID. The `/usage` page and
`lupin quota` show the label. Lupin never shows an email or account ID.
`omp` chooses the login for each session. So a provider is blocked only
when all of its logins are blocked. Lupin does not select a blocked login.
It selects the open login with the most quota left. A machine that
publishes a provider replaces the whole entry for that provider, so each
machine that publishes it needs every login for it.

Lupin versions before issue #169 write quota rows with no labels to a
different Redis key. Lupin shows those rows on the `/usage` page as
stale. A loop start does not use them. Update every machine that runs
`lupin quota`. Until you do, a loop start can refuse a provider that only
an old machine publishes.

## How a slot's limit (`max`) works

A slot does not know its own limit until something tells it. The first
`acquire` call for a new slot sets the limit, from `--max` (default: 1).
Every later `acquire` call for that slot keeps the stored limit — `--max` on
a later call does nothing. `status` reads the same stored limit.

## State

The `local` backend stores slot state as files, under
`$LUPIN_STATE_ROOT` or `~/.lupin/slots/` by default. Each slot is one
directory; each active holder is one file inside it, holding a PID and an
expiry time. The `redis` backend (`slots_redis.py`) covers leases that
several machines must see. Its schema is in `docs/redis-schema.md`.

Loop state is stored under `$LUPIN_LOOP_STATE_DIR` or
`/var/lib/delegation-loop`. Lupin stores enabled repos in `repos`, per-repo
orchestrator profiles in `orchestrators.json`, loop metadata in
`herdr-loops/`, run prompts in `notes/`, stop reports in `reports/`, run
worktrees in `worktrees/`, and one-off schedules in `once/`. `locks/`
serializes local start and stop actions. Herdr keeps its own session and
workspace state.
The dashboard caches GitHub data in `~/.local/state/lupin/cache.json`.
The same file also holds each checkout's repo identity, so a restart does
not pay for `gh` again. If `gh` is missing, Lupin gets the GitHub owner
and repo name from a standard `origin` URL. It uses that name for claim keys,
even when the checkout folder has another name.

`gh_cache.py` shares read-only `gh` results across the fleet through Redis.
`LUPIN_GH_CANONICAL_FETCHER` names the one machine that runs `gh` on a cache
miss. Set it to that machine's hostname on every machine that reads the cache.
The named machine also fetches when Redis is down. When the variable is unset,
no machine is pinned. A machine fetches on a miss only while Redis is
reachable, and only when it gets the lock for that cache key. A machine that
cannot get the lock reads the cache again. If the cache is still empty, it
returns a "fetch in progress" error and does not fetch. The lock is a plain
Redis key with a TTL, not a slot. It does not show on the Machines page.

Each run has its own Git worktree in `worktrees/<repo>/` in the loop state
directory. A worktree is a second working copy of the repo, with its own files
and branch. This one is on a new branch, `lupin-loop/<time>`. `run` runs
`git fetch origin` first. The branch starts at `origin/release/next` when the
remote has it. Otherwise it starts at the remote default branch: the target of
`origin/HEAD`, or else `origin/main` or `origin/master`.
If the fetch fails, `run` refuses to start. The agent works in this worktree.
The main checkout in `LUPIN_LOOP_CODE_DIR` is never the agent's working
directory.

`run` removes the previous run's worktree and branch before it adds a new one.
If git keeps that worktree, `run` refuses to start and names its path. With
`--resume`, `run` reuses the worktree that the last run recorded. It never
creates a worktree on resume. If that worktree is missing, or is on another
branch, `run` refuses and names the path. If `run` fails after it adds a
worktree, it removes that worktree and branch. If git keeps the worktree, the
error names its path. If the platform slot is full when the agent launches, the
loop removes its worktree at once.

`run` refuses to start when the main checkout has a merge, rebase,
cherry-pick, or revert in progress, or has unmerged files. The error names the
state and the path. Lupin never aborts, resets, stashes, or cleans the
checkout. Fix the checkout by hand, then run again.

`stop` removes the run's worktree, then its branch. Git refuses to remove a
worktree that has modified or untracked files. In that case `stop` keeps the
worktree and prints its path. `stop` also keeps the worktree when its commit is
on no branch. `stop` never passes `--force` to git and never deletes a branch
with `-D`.

Before removal, `stop` copies two files from the worktree into `handoffs/` in
the loop state directory. `.loop/loop-state.json` becomes `<repo>.json`.
`HANDOFF.md` becomes `<repo>.HANDOFF.md`. A file the worktree does not have
keeps its earlier copy. The next `run` prompt names each copy that exists. Git
deletes the other ignored files in the worktree. Lupin does not keep them.

After removal, `stop` runs `git branch -d` on the run branch. Git refuses that
when the checkout HEAD does not contain the branch tip. The branch then stays,
and `stop` prints its name. Delete it by hand after its work is merged.
`git -C <checkout> worktree list` shows kept worktrees.

`/roadmap` returns the page shell at once and fills in the board or list
from `/roadmap/board`, which renders one HTML fragment per query and keeps
the last results for `LUPIN_ROADMAP_FRAGMENT_TTL` seconds (default 30).
`?full=1` renders the whole page in one response, with no JavaScript.

The Repos and Roadmap pages read the checkouts in `LUPIN_LOOP_CODE_DIR`
(default `/code`) on the machine that runs `lupin serve`. A repo with no
checkout there does not appear. The serving user must be able to read each
checkout. If a different user owns it, Git reports "dubious ownership" and
the page shows no issues. Add the checkout to Git's `safe.directory` list.


## Dashboard mockups

`docs/ui.dc.html` contains dashboard mockups. Read the matching section before
changing a dashboard page. For example, the Machines page is section `1k`.
`docs/ui-mockups.md` shows a screenshot for each mockup section. Use the
matching screenshot as a visual reference. Use `docs/ui.dc.html` for layout and
content. Treat its values as sample data.
Keep real data and controls accurate. Do not show shared fleet capacity as a
per-machine limit or add controls that the current code cannot support.

## Code layout

- `src/lupin/route.py`, `src/lupin/classify.py`, `src/lupin/model-tiers.json`
  — moved from an earlier project. See the comments at the top of each file for
  the source commit.
- `src/lupin/slots.py` — the `local` slot backend. New code.
- `src/lupin/slots_redis.py` — the `redis` slot backend, with a fallback to
  `local` for the `bmo` slot.
- `src/lupin/_lease_runtime.py` — the `hold` subprocess/lease-renewal code
  shared by both slot backends above.
- `src/lupin/review_dispatch.py` — picks which lock a routed model needs.
- `src/lupin/pr_status.py` — `pr-status`: one read-only view of every open
  pull request. It makes one `gh pr list` call. It shows merge, checks,
  review, approval, claim, comments, description and reviewers.
- `src/lupin/free_gate.py` — routes, then reserves the fleet `bmo` Redis
  slot for a `bmo:` pick before calling it final (issue #37).
- `src/lupin/serve.py`, `src/lupin/roadmap.py` — the dashboard. Moved from
  the earlier dashboard project (issue #204).
- `src/lupin/quota.py` — the `/usage` page's quota reads, split out of
  `serve.py`.
- `src/lupin/quota_cache.py` — `lupin quota`'s shared Redis cache: one
  machine's real quota reading, published for the whole fleet to read
  (issue #38).
- `src/lupin/usage_cache.py` — publishes each machine's 7-day usage totals
  for the `/usage` page to add across the fleet.
- `src/lupin/model_fetch.py` — `fetch-models`: a daily snapshot of model
  IDs and prices per subscription.
- `src/lupin/model_tiers.py` — loads and validates the configured model-tier
  file for routing, the Models page, and benchmark fallback.
- `src/lupin/claims.py` — `claim`/`renew-claim`/`release-claim`: one GitHub
  issue claimed by one host at a time.
- `src/lupin/dispatch.py` — `dispatch`: claims one issue or PR, writes its
  markdown brief outside the repo, and starts its loop.
- `src/lupin/digest.py` — writes a digest for each debrief and shares both
  through Redis.
- `src/lupin/ledger.py` — appends and reads shared repository events in Redis.
- `src/lupin/machines.py` — `join`/`heartbeat`/`drain`/`undrain`/`machines`:
  the fleet's machine registry.
- `src/lupin/loops.py` — signed local-or-remote dispatch for loop controls.
  Attach uses Herdr directly over SSH.
- `src/lupin/loop_runtime.py` — Herdr sessions, workspaces, state, reports,
  schedules, and systemd worker lifecycle.
- `src/lupin/quest.py` — `quest start`/`stop`/`focus`/`release`: a set of
  issues worked together on one machine.
- `src/lupin/place.py` — `place`: picks which fleet machine should run a
  task.
- `src/lupin/supervise.py` — `supervise`: checks this machine's loops with
  fixed rules and nudges idle loops.
- `src/lupin/reconcile.py` — `reconcile`: applies the automatic claim/
  focus/quest release rules.
- `src/lupin/roadmap_cli.py` — the `roadmap` subcommand's own CLI surface
  (distinct from `roadmap.py`, which the dashboard also uses).
- `src/lupin/cli.py` — the `lupin` command. It owns the argument parsing for
  every subcommand; the modules above take an argument list instead of
  parsing their own.
- `tests/` — one test file per module above.

## Build and test

Full gate (the repo's gate, used by the merge rules):

```
nix flake check                                    # build + test, current system
nix develop -c python3 -m pytest -v
```

`nix develop <dir> -c <command>` runs `<command>` in the current directory. It
does not run in `<dir>`. A relative test path then picks up files from the
directory you are in. Run `cd <checkout>` first. To test a reviewed commit, run
`git archive <sha> | tar -x -C <empty-dir>`, then run the tests from `<empty-dir>`.

## Commands in docs and skills

Commands in this file and in the skills must work for any user of this tool.
Use `OWNER/REPO` for the repo, `origin` for the upstream remote, and `<checkout>`
for a path. Do not write a real owner name or machine path into a command. Prose
may name this repo.

## Pull requests, review, and handoff

Every code change goes through a pull request to upstream `main`. Upstream is
the `origin` remote. The owner merges approved pull requests into `main`. The
orchestrator can also merge, when all four conditions in step 2 are true.
The loop policy is in `docs/delegation-loop.md`.

1. Push the branch with `git push -u origin <branch>`. If your token cannot push
   to upstream, push to the existing fork with `git push -u fork <branch>`.
   Never push `main`. Do not create a new fork. You may use `/ship`. Check
   that it does not push `main`. Open the PR on upstream. From a fork branch,
   add `--head FORK_OWNER:<branch>`:

   <!-- markdownlint-disable MD013 -->
   ```sh
   gh pr create --repo OWNER/REPO --base main --title "<title>" --body "Closes #N"
   ```
   <!-- markdownlint-enable MD013 -->

   The PR body has `Closes #N`. GitHub closes the issue when the PR merges
   into the default branch, `main`. If the push fails, report the branch name
   and commit range. Do not merge that branch. If the PR opens, report its
   number to the orchestrator. The orchestrator dispatches `/code-review`.
   Stage every file you change before you commit it. If you edit a file after
   `git add`, run `git add FILE` again. A commit holds the text that was
   staged. After the commit, run `git diff --stat HEAD`. It must print
   nothing. The gate reads the working tree, not the commit, so a missing
   stage looks like a pass.
2. Do not merge your own work. A reviewer who is not the author runs
   `/code-review`. The orchestrator (the agent that dispatches and merges
   work) merges into `main` only when all four are true:
   1. The PR is open on upstream, `OWNER/REPO`, with base `main`.
   2. A reviewer approves the current head SHA. The head SHA is the newest
      commit ID on the branch.
   3. The PR branch contains the current `main`. The ancestor check asks git
      whether one branch contains another. The check is in the
      `delegation-loop` skill.
   4. The repo's full gate, as its `AGENTS.md` defines it, passes.

   A worker or reviewer never merges a pull request.
   On every orchestrator cycle, merge each approved PR that passes these gates.
   See "Continuous merge of approved PRs" in the `delegation-loop` skill.
3. At the end of a session, run `/handoff`. It runs `lupin ledger append`.
4. Loop details for this repo: `docs/delegation-loop.md`.

### Preview server

This repo has a runnable dashboard. Its preview serves `main`.

- Worktree: `.claude/worktrees/preview`. The skill gives the setup commands.
- Start command. Run it from the preview worktree:

```sh
nix develop --command env \
  -u LUPIN_REDIS_USERNAME -u LUPIN_REDIS_PASSWORD \
  LUPIN_REDIS_HOST=127.0.0.1 LUPIN_REDIS_PORT=6399 \
  LUPIN_FLEET_CONFIG="$HOME/.local/state/lupin-preview/fleet.json" \
  LUPIN_LOOP_STATE_DIR="$HOME/.local/state/lupin-preview" PYTHONPATH=src \
  python3 -c '
import sys
from lupin.cli import main
sys.exit(main(["serve", "--bind", "127.0.0.1", "--port", "8789"]))
'
```

- Port: `8789`. Check it is free with `ss -ltn` first. The `ss` command lists
  listening ports.
- Machine: the owner names the machine in their own machine notes. The preview
  serves `main` there on `127.0.0.1:8789`.
- Owner tunnel command: `ssh -N -L 8789:127.0.0.1:8789 MACHINE`. A tunnel
  forwards a port on your computer to the machine.
- Local URL: `http://localhost:8789`.
- Keep it running: a Herdr pane, or `systemd-run --user`. The `systemd-run`
  command starts a command as a background service. If the shell has no user
  bus, set `XDG_RUNTIME_DIR=/run/user/$(id -u)`.
- To bind means to choose the address a server listens on. Never bind to
  `0.0.0.0`. This is a security rule.
- Do not point the preview at the fleet Redis. Start a scratch Redis and use
  it, for example
  `nix develop --command redis-server --port 6399 --bind 127.0.0.1 --save ""`.
  Check port `6399` is free with `ss -ltn` before you start it, so a taken
  port cannot point the preview at another service.
  Redis is not on the PATH outside the dev shell, so run it through
  `nix develop`. Inherited `LUPIN_REDIS_*` values from the loop shell reach
  the server, so the start command removes them and sets the scratch values.
- Do not use the start, stop, or run controls on this dashboard. New runs use
  the `lupin-loops` session. Older loops may use an old session until they
  stop.

## Rules for unattended loops

These rules keep a loop moving when no person is present.

1. **Do not use the question menu.** The menu asks a person to choose. No
   person is there, so the loop waits forever. Write the question as a comment
   on the issue. Start it with `Loop:`. Then take the next item.
2. **Do not stop while work exists.** An unmerged pull request is not a reason
   to stop. Reviews, research, and docs are work. Do them.
3. **Use the fork login for fork pushes.** A fork is your own copy of a repo.
   On a host where `GH_TOKEN` cannot push to the fork, the saved fork login
   can. `FORK_OWNER` is the fork owner on that host. Put `env -u GH_TOKEN`
   before each command. It removes `GH_TOKEN` for that one command only. Do not
   run `unset GH_TOKEN`:

   <!-- markdownlint-disable MD013 -->
   ```sh
   env -u GH_TOKEN git push -u fork <branch>
   env -u GH_TOKEN gh pr create --repo OWNER/REPO --base main --head FORK_OWNER:<branch> --title "<title>" --body-file <file>
   ```
   <!-- markdownlint-enable MD013 -->

4. **Do not run `rm` with a glob.** A glob is a pattern such as `*`. It can
   delete more files than you mean. Do not delete old files with a pattern.
   Give each new file a new name.
5. **Ask a worker with no result for it.** If a worker has no result 30
   minutes after it starts, ask it for the result. If it has none, write that
   on the issue or the PR. Start it with `Loop:`. Then stop the worker. The
   `delegation-loop` skill shows how to ask a worker (`herdr agent prompt`).
6. **Log environment problems in the ledger.** On some hosts, `lupin roadmap`
   fails. The Redis user for that host has no permission to run `MULTI`.
   `MULTI` is a Redis command that runs several commands as one unit. Also,
   on some hosts, `gh` cannot see the environment repo. That repo tracks
   environment problems. Write each problem to the ledger instead.
   `OWNER/REPO` is that environment repo:

   ```sh
   lupin ledger append OWNER/REPO --event env-friction --summary "<problem>"
   ```

7. **Do not stop after a review.** A review with findings starts the next
   round. Send the findings to a fix worker. Then get a new review at the new
   head. Repeat until the pull request is ready for production. Ready for
   production means three things. A reviewer approved the current head. The
   full gate passes. The branch contains `main`. There is no limit on rounds.
   If the same finding comes back for 3 rounds, give the fix worker a model
   one tier higher.
8. **Work the queue until nothing is left.** Nothing is left when all of these
   are true. No open issue that you may take is unclaimed. Every pull request
   you own is ready for production. No pull request waits for a review. No
   research, docs, or triage item is left. Waiting for the owner is not the
   end. Write the question as a comment on the issue. Start it with `Loop:`.
   Then take the next item. Never write "tell me if you want me to continue".
