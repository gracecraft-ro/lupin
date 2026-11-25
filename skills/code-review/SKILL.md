---
name: code-review
description: >-
  Review GitHub pull requests for correctness, safety, and maintainability.
  Use for every change before the owner merges it.
compatibility: Requires GitHub CLI, a pull request, and access to the repository.
---

# Review a pull request

Review the pull request (PR), not a local diff. Pass `--repo OWNER/REPO` to every
command.

## Read

```sh
lupin review-route --prefetch 123 --repo OWNER/REPO && gh pr diff 123 --repo OWNER/REPO
```

Review the current head again each time the PR
changes, if needed. (Never necessary for docs changes, etc)

## Check

Check the diff against the issue's acceptance checks, the repo rules, and these
lenses. Passing CI does not prove the design is sound. Before you start writing your review, please merge in the latest master if the PR is lagging behind so that your review is as accurate as possible.

Review critical RFCs and research as thoroughly as major logic or UI changes.
Use a full independent review process. You may dispatch subagents to check sources, do supplementary online research, and evaluate evidence for key claims. State assumptions. Compare alternatives. Assess risks. Check that conclusions follow from evidence. Report unsupported claims or missing analysis as findings.

| Lens             | Look for                                                                                                                                                                                                                                                                                                                                                          | Label                                                  |
| ---------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------ |
| `bug`            | Wrong result, crash, or missed edge case (empty input, zero, last item, time zones). Grep every caller of a changed function.                                                                                                                                                                                                                                     | `🔴 bug`                                               |
| `risk`           | Security hole, unsafe access, data loss, an ignored error, or writes that must all succeed together but do not.                                                                                                                                                                                                                                                   | `🔴 bug`                                               |
| `scale`          | Fine for one user, wrong for many. Read the expected load in the README and deploy config first.                                                                                                                                                                                                                                                                  | `🟡 risk`                                              |
| `missing test`   | Risky new logic has no test that fails when the logic breaks.                                                                                                                                                                                                                                                                                                     | `🟡 risk`                                              |
| `speed`          | An unnecessary slowdown. (missed async opportunities, memory leaks, clumsy algorithms, etc)                                                                                                                                                                                                                                                                       | `🟡 risk` (big), `🔵 nit` (small)                      |
| `lean`           | Dead code, a helper the repo already has (name its path), a dependency for a few lines, an abstraction with one use, near-copies, or tests that check nothing. Unnecessary complexity is unacceptable.                                                                                                                                                            | `🟡 risk` if it adds code, `🔵 nit` for a shorter form |
| `test bloat`     | A new test that fails in the same cases as an existing test. A new test file where one exists for the area. An issue number in a test title or file name. Table rows that run one code path. Duplicate tests are `🟡 risk`, never `🔵 nit`.                                                                                                                       | `🟡 risk`                                              |
| `slop`           | Visual evidence with ANY sign of slop, bugs, failure, bad UI/UX, or lack of polish. Look EXTREMELY closely and be ruthless; 50% of critical bugs and failed implementations can be caught from CAREFUL review of the images. Upload your own images and add them before reviewing them in cases where the author failed to follow protocol in uploading evidence. | `🟡 risk` (big)                                        |
| `missed-comment` | Look carefully for owner comments on the PR that were ignored or not addressed. If the owner took the time to comment, it's something important to address and a blocker until it is addressed.                                                                                                                                                                   | `🟡 risk` (big)                                        |

Also:

- Identify docs that the change makes wrong.
  Check visual, spatial, and gif evidence against the `visual-evidence` skill. _Immediately dispatch an economical subagent to add missing evidence when needed! It is almost entirely pointless to review the PR without a comprehensive evaluation of the visual evidence._
- An owner comment that names a problem is a finding. A PR comment is never an
  approval.
- A `shortcut:` comment that names its limit is a decision. Report it only if
  the expected load passes that limit.

Before you report a finding, name the input that fails. No input, no finding.
Re-read the lines and grep for callers. Propose the smallest fix, and prefer
fixes that delete code. Give no style preferences and no vague advice.

## Output format

Use short sentences and everyday words. Explain each technical term once.

1. First line: `Round N. LEVEL: <LEVEL>. Target: OWNER/REPO PR #<number> at <full head SHA>.` Diff: `+#<lines added> lines, -#<lines removed>`. `Verdict: <Approved or Not Approved>`
2. Second line: `Summary:` and two or three sentences explaining the PR, its potential impact, and any observations. Write enough for a reader who has no context to get the need-to-know facts on the proposed changes.
3. Findings, numbered across all groups so the author can say "fix 2 and 5".
   Skip an empty group.
   - **Must fix:** each `🔴 bug`, and each `🟡 risk` that breaks at the expected
     load. Start by describing the impact of each bug.
   - **Should fix:** other `🟡 risk`.
   - **Nice to have:** `🔵 nit`, and `❓ q` (a question; it blocks only if the
     answer changes what to do).
4. `Verdict: Ship.` or `Verdict: fix 1 and 3 first.` Use `Ship` only when no
   blocking finding remains.
5. `Not checked: <one line>` if any corners were cut during review - try to be thorough.

With no findings, write lines 1 and 2, then `Ship.` and one line on
what you checked.

One finding looks like this:

```text
2. **Empty token passes the check** (`src/auth.py:L42`, `🔴 bug`)
   - **Problem:** The check accepts an empty token, so a request with no token passes.
   - **Fix:** Reject an empty token before the check runs.
   - **If we skip it:** Anyone who sends an empty token can use the endpoint.
```

## Run the fix cycle

Post each verdict as a GitHub review bound to the exact SHA you read. Use
`APPROVE` for `Ship` and `REQUEST_CHANGES` for findings:

```sh
gh api repos/OWNER/REPO/pulls/<number>/reviews \
  -f event=APPROVE -f commit_id=<full SHA> -F body=@verdict.md
```

Do not use `gh pr review`; it may bind the review to a newer head.

Use `-f event=REQUEST_CHANGES` when the review asks for changes. If your login
opened the PR, post `COMMENT` and ask the owner for a formal review in a `Loop:`
PR comment.

1. After a blocking verdict, start a fix subagent. Most tasks can be handled by Luna or Haiku. Be explicit about what level of effort you want.
2. Give it a brief with the PR, branch, remote, findings, and gate output. Tell
   it to follow `/ship` for the fix, gate, and screenshots. Use the PR-branch
   worktree steps in the `delegation-loop` skill; it pushes to the same branch.
3. Watch the process in the background for updates to the branch or a 45 minute timeout. Monitor & intervene when necessary.
4. If/When the SHA changes, review it as the next round. Repeat until the PR is production-ready.

Report the final verdict to the orchestrator.
