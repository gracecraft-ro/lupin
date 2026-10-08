---
name: ship
description: >-
  Finish a delegated GitHub issue from its acceptance checks through verification
  and a clear report. Use this skill when implementing or finishing an issue,
  preparing its pull request, or reporting completed work.
compatibility: Requires the repository's tools and GitHub CLI. Lupin and Redis are used when fleet claims are configured.
---

# Ship a delegated issue

Follow the repository's `AGENTS.md`, issue workflow, and release rules. Those
rules decide the base branch, checkout type, test commands, and whether to open
a pull request.

## Check the task and checkout

1. Read the issue and current comments. For a Lupin-managed repo, fetch a short
   context bundle with:

   ```sh
   lupin review-route --prefetch 123 --repo OWNER/REPO
   ```

   This returns issue or pull request text and comments; it does not choose a
   model. Use `lupin place 123 --json` for an implementation model, effort, and
   machine recommendation. It does not claim or launch the task.
   Before a separate review, route the issue with:

   ```sh
   gh issue view 123 --repo OWNER/REPO --json title,body,labels > /tmp/lupin-issue-123.json
   lupin review-route --issue-json /tmp/lupin-issue-123.json --mode separate
   ```

   This recommends a reviewer model and lock. It does not start a review agent.
2. Check `pwd`, the current branch, and `git status`. Confirm this checkout is
   dedicated to the issue. Use the repository's worktree or clone instructions;
   do not edit a shared main checkout while another loop may use it.
3. Read the acceptance checks. If they are missing or unclear, state the gap
   before coding. Do not replace the requested behavior with a smaller change.
4. If fleet claims are configured, claim the issue with
   `lupin claim OWNER/REPO#123 --holder SESSION`. Renew and release it with
   `lupin renew-claim OWNER/REPO#123 --holder SESSION` and
   `lupin release-claim OWNER/REPO#123 --holder SESSION`. A quest already
   holds its issues; do not create a second claim for them.

## Implement and verify

- Inspect the relevant code and tests before editing. Make the smallest change
  that meets the acceptance checks.
- Add or update a regression test for a behavior change. Use the repository's
  documented gate. Then run the changed feature or command and observe the
  result. A passing test or build alone does not prove the task works.
- For a visual change, open the real surface and capture before and after
  screenshots. If the tools cannot show the surface, report that it was not
  visually verified.
- Read the final diff. Check the current issue and pull request comments again
  before merge or closure. Address new requests before reporting the task as
  done.

## Release and report

Commit and open a pull request only as the repository permits. If you cannot
push, keep the local branch and report its name and commit range. Do not say a
change is merged or shipped when it is only committed locally.

Post a short issue report when the repository allows it. Include:

- **Highlights:** what changed and the main files.
- **Evidence:** exact tests, commands, and observed output.
- **Decisions:** important choices or limits.
- **Next:** merge, owner action, or `None`.

Attach visual evidence when the repository's GitHub CLI supports it. Save a
local copy only in a path the repository ignores. Do not attach or report an
artifact that you did not inspect.

Release a direct issue claim with `lupin release-claim` when the work ends. Use
`lupin quest stop` for a quest that should release its remaining issue claims.
Leave the issue open until its acceptance checks and repository release rules
are met.
