# Changelog

All notable changes to Lupin. The version has one source:
`pyproject.toml`. Add one line under `Unreleased` in each pull request.

## Unreleased

### Added

- `lupin --version` prints the package version. The flake reads the same
  version from `pyproject.toml`.
- `docs/release.md` gives the release steps.
- The Roadmap board shows stage tabs with counts, like the List view.

### Fixed

- The Roadmap page family uses one set of stage words (In progress, Ready,
  Later, Blocked) on the board, the list, the detail panel and the queue
  graph. The empty states keep the selected repositories visible, the note
  under the board uses plain words, the priority filter offers P4, and the
  epics footer and the pager read the same way when the list is empty.
- Roadmap repo-fetch warnings can be dismissed on Board and List. A dismissal
  stays hidden after a reload and applies to one repo and warning.

### Fixed

- "Run now" passes dash-leading notes as one argument, and rejects notes
  with a NUL byte before dispatch.
- OMP prompt jobs do not wait for caller stdin to close.

## 0.1.0

- First version. Model routing, slot leases, the dashboard, the fleet
  registry, claims, quests, the loop runtime, the supervisor, and debriefs.
