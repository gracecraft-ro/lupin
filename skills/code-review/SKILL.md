---
name: code-review
description: >-
  Review GitHub pull requests for correctness, safety, and maintainability.
  Use for every change before the orchestrator merges it.
compatibility: Requires GitHub CLI, a pull request, and access to the repository.
---

# Review a pull request

Review the pull request, not only the issue or a local diff. Read the repo's
`AGENTS.md`, the linked issue, and the latest PR comments and reviews.

Use Lupin for issue context when it is configured:

```sh
lupin review-route --prefetch 123 --repo OWNER/REPO
gh pr view 123 --repo OWNER/REPO --json title,body,comments,reviews
gh pr diff 123 --repo OWNER/REPO
gh pr checks 123 --repo OWNER/REPO
```

Review the current PR head against its base. If the PR changes after review,
review the new head again. If there is no PR, ask the orchestrator to open one.
Do not replace a PR review with a review of an unsubmitted branch.

## Check the change

- Check the diff against the issue's acceptance checks and repo rules.
- Look for wrong behavior, missing boundaries, unsafe access, error cases,
  broken callers, weak tests, and unnecessary complexity.
- Read relevant tests and code outside the diff when needed.
- Identify when docs should be revised to reflect updated functionality or architecture.
- Check CI status. Do not treat a green build as proof that the design is sound.
- For UI changes, inspect the real surface and attached screenshots.
- For 3D changes, inspect the contact sheet and compare useful views.
- Do not edit files, run linters, approve or request changes, or merge the PR.\*
  Return the verdict and findings to the orchestrator. It posts them on the PR,
  dispatches fixes, and runs the required gate.

## Report findings

Give one actionable line per finding. Include the exact file and PR line:

```text
src/file.py:L42: 🔴 bug: this path accepts an empty token. Reject it before use.
```

Use these labels when they help:

- `🔴 bug` — broken behavior or a security problem.
- `🟡 risk` — fragile behavior that can fail in a real case.
- `🔵 nit` — optional style or naming change.
- `❓ q` — a question that blocks a clear decision.

Do not repeat what the code does. Name the problem and the fix. Use a short
paragraph only when a security or design issue needs more context.

Return one result for each PR:

```text
PR 123 — APPROVED
No blocking findings.
```

or:

```text
PR 123 — CHANGES REQUESTED
- src/file.py:L42: 🔴 bug: ... Fix ...
```

Use `APPROVED` only when no blocking finding remains. List optional nits as
non-blocking. The orchestrator posts the verdict and findings on the PR,
dispatches fixes, and runs this review again on the updated PR head. It merges
only after approval and required checks pass.

It is CRITICAL to check that excessive code isn't being added. There is a tendency for agents to reinvent the wheel, so to speak, create things that already exists, write tests that test nothing meaningful, or write too many tests. Whenever possible, we need to update existing tests instead of adding new tests when fixing behavior and not adding new features. If you see that a PR is bloated, you need to propose the changes to streamline it, or else you risk being buried in an avalanche of tech debt one PR at a time.

\*If the change you suggest is trivial to implement, you may commit the fix to the expedite the process.
