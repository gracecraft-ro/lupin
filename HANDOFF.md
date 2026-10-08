# Issue 42 handoff

Use Redis as the shared source for roadmap annotations and handoff status.
When open, the roadmap reloads about every five minutes and reads new events.

## Current Step
PR #44 is open. Tests and the Nix check passed; all follow-up commits are pushed.

## Files Changed
`src/lupin/ledger.py`, `cli.py`, and `roadmap.py`; ledger and roadmap tests;
`docs/redis-schema.md` and root `AGENTS.md`.

## Verification Status
Focused pytest: `pytest -q tests/test_ledger.py tests/test_roadmap.py`
and `tests/test_serve.py`: 268 passed, 9 subtests in 220.72s.
Installed CLI smoke on isolated loopback Redis: default returned issues
3–12; `--limit 3` returned 10–12. `--limit 0` returned exit 1.
`nix flake check`: all checks passed on aarch64-linux; aarch64-darwin
and x86_64-linux were omitted.
Earlier roadmap reload smoke completed after 306 seconds and showed the
isolated Redis event. The isolated GitHub cache showed zero issues.
The HTTP route test covered issue annotations.

PR #44 is open from `gracecraft-ro/lupin`. The report is on issue #42 and
PR #44. A final handoff event was written to fleet Redis (ID
`1791430490296-0`).

## Next Action
Review and merge PR #44 after approval.

## Known Blockers
The active OAuth account cannot upload to the upstream repo. A PAT with
upstream push permission returned HTTP 403: the attachment endpoint does not
accept personal access tokens. Screenshots remain local in `evidence/42/`.
The main checkout user change in `tests/test_gh_cache.py` remains untouched.

# Issue #40 handoff

## Goal
Replace `loopctl` with Lupin for delegation-loop lifecycle. Use one Herdr session per repo.

## Worktrees
- Lupin: `/home/ghosta/jobs/lupin-issue-40`, branch `issue-40-herdr-lifecycle`.
- Ghostbook: `/home/ghosta/jobs/ghostbook.nix-issue-40`, branch `issue-40-herdr-runtime`.
- Local `/code/lupin` `main` includes issue commit `2cb0863` and sync merge `561ca89`.
- Local `/code/ghostbook.nix` `main` includes issue commit `b4bded1` and sync merge `5c22694`.
- These commits are local only. No push, host activation, or deployment.
- Uncommitted work is not part of these commits. Lupin: `AGENTS.md`, `src/lupin/benchmark_fetch.py`, `src/lupin/cli.py`, `tests/test_benchmark_fetch.py`, and `tests/test_gh_cache.py`. Ghostbook: `docs/sandbox-todo.md`, `flake.nix`, `hosts/ralpha/README.md`, and `.serena/logs/`.
- Each issue worktree still has its `issue-40 local work before main sync` stash.

## Changes
- Lupin stores Herdr loop state and repo inventory in the machine heartbeat.
- The dashboard shows remote repos as read-only. Local one-off runs use `lupin once now <repo>`.
- Updated the machine schema, CLI callers, dashboard, tests, and docs after the main merge.
- Kept the upstream quota publisher and fleet update script in Ghostbook.

## Checks
- Lupin `nix flake check --no-write-lock-file`: all checks passed on `aarch64-linux`. Nix skipped `aarch64-darwin` and `x86_64-linux`.
- The corrected `tests/test_machines.py::test_cli_join_heartbeat_drain_undrain_machines_roundtrip` passed.
- The real `/repos` page showed `widgets` running on `smoke-remote` as a read-only fleet entry. It had no schedule or add form for that remote repo.
- `bash -n` passed for the four changed Ghostbook scripts.
- Herdr 0.9.3 local smoke created and closed a workspace. It also read a pane and checked agent state. No real coding agent or remote SSH ran.

## Host builds
- The Jesus NixOS build passed with the local Lupin issue worktree as an input override. No host was activated.
- The Ralpha build stopped in U-Boot: `./scripts/gcc-version.sh -p gcc` returned “No such file or directory.” The system build is not complete.

## Release gate
- Ghostbook `flake.lock` pins Lupin `20f96406a224510b47a150ff45e71f894be806b1`. The host build used the local Lupin issue worktree, not this pin.
- Search for `systemd.services.lupin-agent`, `LUPIN_CMD_SIGNING_KEY`, and `lupin agent` in both host configs found no matches.
- Remote signed commands and remote SSH remain untested. Do not treat the pinned input as verified for issue #40.
