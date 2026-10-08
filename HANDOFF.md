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
