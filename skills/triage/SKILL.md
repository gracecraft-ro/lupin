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

Use the repository's label map if it has one. Otherwise, keep its existing
label names. Do not create or rename labels as part of triage. Lupin does not
apply labels for you.

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
not claim the issue or start an agent. `lupin quota` shows the latest shared
quota readings. Routing uses the quota data available to the local process;
do not assume that the shared quota display is also the router's input.

If Lupin cannot place the task, use `lupin review-route` with the issue's
category and size to get a model and effort recommendation. Do not ignore a
reported quota wait by selecting the blocked model yourself.

When Redis fleet claims are configured, reserve an issue before dispatch. Use
the same target and holder to renew or release it:

```sh
lupin claim OWNER/REPO#123 --holder SESSION
lupin renew-claim OWNER/REPO#123 --holder SESSION
lupin release-claim OWNER/REPO#123 --holder SESSION
```

A claim needs Redis; Lupin has no local claim fallback. If the claim fails, do
not report the issue as reserved. Follow the repository's local coordination
rules instead.

For a related set of issues, `lupin quest start --issue 123 --issue 124`
claims the issues and registers a quest on a selected machine. It does not
start an agent or disable quota pacing.

## Handoff

Give the worker only the context it needs:

- Issue number, goal, and acceptance checks.
- Current comments and open dependencies.
- Files or components likely to change; known parallel work.
- Lupin's model, effort, and machine recommendation.
- Claim or quest ID, repository instructions, and required verification.

Use the repository's handoff ledger if it has one. Lupin reads
`.loop/loop-state.json` for roadmap status, but it does not write dispatch or
handoff entries.
