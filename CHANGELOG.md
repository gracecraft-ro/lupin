# Changelog

All notable changes to Lupin. The version has one source:
`pyproject.toml`. Add one line under `Unreleased` in each pull request.

## Unreleased

### Added

- `lupin logs` shows events from a loop run. Lupin keeps up to 20 logs per
  repo and removes logs older than 30 days.
- `lupin --version` prints the package version. The flake reads the same
  version from `pyproject.toml`.
- `docs/release.md` gives the release steps.

### Fixed

- "Run now" passes dash-leading notes as one argument, and rejects notes
  with a NUL byte before dispatch.

## 0.1.0

- First version. Model routing, slot leases, the dashboard, the fleet
  registry, claims, quests, the loop runtime, the supervisor, and debriefs.
