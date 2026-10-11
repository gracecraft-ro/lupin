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
- Find the expected load first. Judge `scale` against it.
- Check each change through six lenses. A lens is one kind of check:
  - `bug`: wrong result, crash, or missed edge case. Edge cases include empty
    input, zero, the last item, rounding, and time zones. Grep every caller of a
    changed function.
  - `risk`: security hole, unsafe access, or data loss. Data loss includes a
    swallowed error and writes in the wrong order. It also includes a group of
    writes that must all succeed or all fail, but does not.
  - `scale`: fine for one user, wrong for many. Look for two requests that check
    a value, then write it, at the same time. Also look for repeated work, lists
    that only grow, and one query per item.
  - `missing test`: risky new logic has no test that fails when it breaks.
    Risky logic includes a branch, a parser, money, security, a data write, or
    a bug fix. Aim for one good test, not coverage.
  - `speed`: a big slowdown is a finding. A small win is a suggestion.
  - `lean`: code that should not exist or should be smaller. Label a lean finding
    `🟡 risk` unless it is a shorter form. Look for:
    - dead code
    - a helper the repo already has. Reuse: name the path of the existing helper.
    - a dependency for a few lines
    - an abstraction with one implementation
    - near-copies that must change together
    - split: one function that does several unrelated jobs. Split by job, not
      by line count.
    - excess code. Agents often rebuild helpers, write tests that check nothing
      useful, or add too many tests. When you fix behavior, update existing
      tests instead of adding new ones. If a PR is bloated, propose changes that
      make it smaller.
- Read relevant tests and code outside the diff when needed.
- Identify when docs should be revised to reflect updated functionality or architecture.
- Check CI status. Do not treat a green build as proof that the design is sound.
- For UI changes, inspect the real surface and attached screenshots.
- For 3D changes, inspect the contact sheet and compare useful views.
- Do not edit files, run linters, approve or request changes, or merge the PR.\*
  Return the verdict and findings to the orchestrator. It posts them on the PR,
  dispatches fixes, and runs the required gate.

## Check before you report

- Name the input or situation that goes wrong. No case, no finding.
- Re-read the lines. Confirm that the caller exists, that the value can be
  empty, or that the code is unused. Use grep to find callers.
- Propose the smallest fix that works. Prefer fixes that delete code. Do not
  add layers, frameworks, or config that the problem does not need.
- No style taste. Do not write vague advice, such as "consider this."
- A shortcut marked `shortcut:` (or older `ponytail:`) that names its limit is a
  decision, not a finding. Report it only if the expected load already crosses
  that limit.

## Report findings

Give one actionable line per finding. Include the exact file and PR line:

```text
src/file.py:L42: 🔴 bug: this path accepts an empty token. Reject it before use.
```

Use these labels when they help:

- `🔴 bug`: broken behavior or a security problem.
- `🟡 risk`: fragile behavior that can fail in a real case.
- `🔵 nit`: optional style or naming change.
- `❓ q`: a question that blocks a clear decision.

Map the lenses to these labels. Use `🔴 bug` for the `bug` lens, a security hole,
or data loss. Use `🟡 risk` for the `scale`, `missing test`, and `speed` lenses.
Use `🟡 risk` for a lean finding that adds or keeps code. Examples: excess code,
a duplicate helper, a one-implementation abstraction, or a dead function. Use
`🔵 nit` for a small speed win and for a shorter form.

Do not repeat what the code does. Name the problem and the fix. Use a short
paragraph only when a security or design issue needs more context.

Return one result for each PR:

```text
PR 123: APPROVED
No blocking findings.
```

or:

```text
PR 123: CHANGES REQUESTED
- src/file.py:L42: 🔴 bug: ... Fix ...
```

`🔴 bug` blocks. `🟡 risk` blocks when it breaks at the expected load. `🔵 nit` never blocks.

Use `APPROVED` only when no blocking finding remains. List optional nits as
non-blocking. The orchestrator posts the verdict and findings on the PR,
dispatches fixes, and runs this review again on the updated PR head. It merges
only after approval and required checks pass.

\*If the change you suggest is trivial to implement, you may commit the fix to the expedite the process.
