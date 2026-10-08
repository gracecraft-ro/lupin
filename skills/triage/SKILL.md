---
name: triage
description: >-
  Prioritize GitHub issues and choose the next work to delegate. Use this
  skill when sorting a backlog, applying issue labels, checking dependencies,
  or preparing an issue for Lupin routing.
compatibility: Requires Lupin and GitHub CLI. Redis is required for fleet claims.
---

# Triage issues with Lupin

Read the repository's `AGENTS.md` and delegation notes first. Read the latest
issue comments before making a decision. An issue body can be out of date.

## Read the queue

From a configured repository checkout, run:

```sh
lupin roadmap --repo REPO --stage all --json
lupin roadmap --dag --json
lupin review-route --prefetch 123 --repo OWNER/REPO
```

`roadmap` ranks open work. `roadmap --dag` shows issue dependencies across
enabled repositories. `review-route --prefetch` fetches issue or pull request
text and comments; it does not choose a model. Omit `--repo` when Lupin's
enabled-repository list is the intended scope.

Use the repository's label map if it has one. Keep its priority, size, type,
and blocker labels. Use a separate `claimed` label for active work. Add it
during repository setup if it does not exist. If you cannot add it, post the
claim comment and report that the label is missing. Lupin does not change
GitHub labels.

## Label UI pressure-test work

Use `ui-pressure-test` for an issue whose main goal is a human-like UI
pressure test, when the repo's label map supports it. This is a work label,
not a priority, size, issue type, or status. Do not apply it to every bug or
design ticket found by a test. If the label is missing, report the gap; do not
create a tracker label during triage.

For Lupin's benchmark category, use `frontend-ui` for visual and interaction
testing. Do not create a separate `visual` category. For CAD or 3D modeling
work, use `cad-spatial` for the evaluator lane that tests geometry or spatial
tools. These categories describe the work; they do not replace the
`ui-pressure-test` work label.

Lupin's classifier returns one category and checks CAD before UI. A mixed
issue with CAD and UI terms routes as `cad-spatial`. Split visual and CAD work
into evaluator lanes, or route each lane separately. Do not assume one
benchmark category covers both.

## Decide what is ready

- Check the dependency graph and the latest comments. Do not dispatch an issue
  with an open blocker.
- Check that the issue has a clear result. For a large issue, split it into
  small issues with separate acceptance checks before implementation.
- Check likely files before running issues in parallel. Treat Lupin's batch
  display as an estimate: it uses file paths written in issue text and does
  not confirm that a worker is running.
- Treat priority, size, and status labels as repository data. Common names are
  `P0` to `P3`, `size-xs` to `size-xl`, `bug`, `needs-expert-decision`,
  `completely-blocked-on-human`, and `epic`. A repository may use other names.
- `needs-expert-decision` is a routing signal, not an automatic stop. Ask a
  capable reviewer to resolve the decision. Stop for a person only when the
  task needs their credentials, money, hardware, or another approval that the
  repository rules reserve for them.
- Use `completely-blocked-on-human` only for a real human-only blocker. Record
  the exact missing action. Work on other issues while it is blocked.

## Route and reserve the issue

From the issue's repository checkout, `lupin place 123 --json` classifies the
issue, recommends a model and effort, and ranks available machines. It does
not claim the issue or start an agent. `lupin quota` displays the shared Redis
snapshot and refreshes it with local provider data when this machine has
credentials. `route()` reads quota from the local machine, so the two results
may differ.

If Lupin cannot place the task, use `lupin review-route` with the issue's
category and size. Do not ignore a reported quota wait by selecting the
blocked model yourself.

When Redis fleet claims are configured, reserve the issue before dispatch:

```sh
lupin claim OWNER/REPO#123 --holder SESSION
lupin renew-claim OWNER/REPO#123 --holder SESSION
lupin release-claim OWNER/REPO#123 --holder SESSION
```

A claim is stored in Redis. It does not add a GitHub label or comment, and it
does not store the worktree path. After the claim succeeds, the claim owner
adds the `claimed` label and comments with the machine, worktree, branch, and
holder. When the claim ends, remove the label and comment with the result.
Do not mark an issue as claimed if the Redis claim fails. Lupin has no local
claim fallback.

For a related set of issues, `lupin quest start --issue 123 --issue 124`
claims the issues and registers a quest on a selected machine. It does not
start an agent or disable quota pacing. Do not claim those issues again.

## Handoff

Give the worker a short brief:

- Issue number, goal, and acceptance checks.
- Current comments and open dependencies.
- Likely files and known parallel work.
- Lupin's model, effort, and machine recommendation.
- Claim or quest ID, machine, worktree path, and branch.
- Repository instructions and required verification.

Tell the worker to use `/ship`. Use the repository's handoff ledger if it has
one. Lupin reads `.loop/loop-state.json` for roadmap status, but does not write
dispatch or handoff entries.
