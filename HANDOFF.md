# Issue #40 handoff

## Goal
Replace `loopctl` with Lupin for delegation-loop lifecycle. Use one Herdr session per repo.

## Worktrees
- Lupin: `/home/ghosta/jobs/lupin-issue-40`, branch `issue-40-herdr-lifecycle`.
- Ghostbook: `/home/ghosta/jobs/ghostbook.nix-issue-40`, branch `issue-40-herdr-runtime`.
- Merged Lupin `origin/main` at `41d05a4` and Ghostbook `origin/main` at `bc03da8`. Both merges are still uncommitted.
- Separate machine-log commit in mounted Ghostbook `main`: `ac45f20`. No issue-branch commit or push.
- No push, host activation, or deployment.
- Each worktree still has its `issue-40 local work before main sync` stash.

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

## Host build limit
- Jesus and Ralpha system builds both stopped in `contour` because `/bin/sh` was missing from the Nix build sandbox.
- A Jesus retry added the pinned Bash path to that build's sandbox. It then failed to compile `contour`: `simd::rebind_simd_t` and `simd::static_simd_cast` were missing with GCC 16.2.0.
- No Nix config changed. The host system builds are not verified.

## Release gate
- Ghostbook `flake.lock` pins Lupin main `41d05a49db2b5a299d0a68721b26360a4e95545d`. It does not include the issue #40 runtime code. The host build used the local Lupin worktree.
- The host files do not configure a `lupin agent` service or `LUPIN_CMD_SIGNING_KEY` (`grep` for `systemd.services.lupin-agent|LUPIN_CMD_SIGNING_KEY|lupin agent` in both configuration files returned no matches).
- Remote signed commands and remote SSH remain untested. Do not deploy this pin as issue #40 support.
