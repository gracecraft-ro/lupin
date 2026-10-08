# Issue 42 handoff

Use Redis as the shared source for roadmap annotations and handoff status.
When open, the roadmap reloads about every five minutes and reads new events.

## Current Step
Commit and push the change, open a linked PR, and post the issue report with
the before and after screenshots.

## Files Changed
`src/lupin/ledger.py`, `roadmap.py`, `serve.py`, `cli.py`, `slots_redis.py`;
ledger, roadmap, and serve tests; `docs/redis-schema.md`, root `AGENTS.md`,
and the delegation-loop and triage skills.

## Verification Status
The first `nix flake check` ran 741 tests: 737 passed and four failed.
Fixes removed identity caching, corrected handoff assertions, and set quest
state in the roadmap route. The second check ran 741 tests: 739 passed; the
serve assertion had a punctuation error, now fixed. Three affected existing
tests passed on clean `origin/main` (0.59s). The targeted branch suite passed:
263 tests and 9 subtests in 221.60s. CLI tests use an isolated fleet config.
The final `nix flake check` passed on aarch64-linux; it omitted
aarch64-darwin and x86_64-linux.

The roadmap reloaded after 306 seconds and showed the isolated Redis event.
Before and after screenshots are in `evidence/42/`. The isolated GitHub cache
was empty, so the page showed zero issues; `test_serve.py` covers the Redis
issue annotation route.

## Next Action
Commit the change, open the PR, then attach the report and screenshots to
issue #42.

## Known Blockers
None. The main checkout has a user change in `tests/test_gh_cache.py`; it remains untouched.
