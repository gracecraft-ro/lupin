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
