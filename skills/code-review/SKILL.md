---
name: code-review
description: >-
  Review GitHub pull requests for correctness, safety, and maintainability.
  Use for every change before the owner merges it.
compatibility: Requires GitHub CLI, a pull request, and access to the repository.
---

# Review a pull request

Review the pull request, not only the issue or a local diff. Read the repo's
`AGENTS.md`, the linked issue, and the latest PR comments and reviews.

## Name the target

- Pass the repo from your brief as `--repo OWNER/REPO` to every command.
- The orchestrator holds the Lupin claim on `OWNER/REPO#N` for your review and
  its fix cycle. Do not claim the PR yourself. If the brief names no claim, stop
  and ask the orchestrator before you read the diff.
- Review the current PR head against its base, and again when the PR changes. If
  there is no PR, ask the orchestrator to open one. Do not review an unsubmitted
  branch instead.

Use Lupin for issue context when it is configured:

```sh
lupin review-route --prefetch 123 --repo OWNER/REPO
gh pr view 123 --repo OWNER/REPO --json title,body,comments,reviews
gh pr diff 123 --repo OWNER/REPO
gh pr checks 123 --repo OWNER/REPO
```

## Check the change

- Check the diff against the issue's acceptance checks, repo rules, and the six
  lenses below. Read tests and code outside the diff as needed.
- Find the expected load first, in the README and the deploy config. Judge
  `scale` against it.
- Find docs that need a revision to match new functionality or architecture.
- Check CI status. Passing CI checks do not prove that the design is sound.
- For visual changes, inspect the real surface and attached screenshots. A
  visual change with no screenshots on the PR is a `🔴 bug`. Tell the author to
  attach before and after screenshots. A new screen needs no before. "Attach
  screenshots to the PR" in the `ship` skill defines a visual change.
- For 3D changes, inspect the contact sheet and compare useful views.
- An owner comment on the PR that names a problem is a finding. The
  `delegation-loop` skill defines an owner comment and the `Loop:` prefix. A PR
  comment is never an approval.
- Do not edit files or run linters. Do not merge the PR.

| Lens | Look for | Label |
| --- | --- | --- |
| `bug` | Wrong result, crash, or missed edge case (empty input, zero, the last item, rounding, time zones). Grep every caller of a changed function. | `🔴 bug` |
| `risk` | Security hole, unsafe access, or data loss: an error caught and ignored, writes in the wrong order, or a group of writes that must all succeed or all fail but does not. | `🔴 bug` for a security hole or data loss |
| `scale` | Fine for one user, wrong for many: two requests that check a value, then write it, at the same time; repeated work; lists that only grow; one query per item. | `🟡 risk` |
| `missing test` | Risky new logic (a branch, a parser, money, security, a data write, a bug fix) has no test that fails when it breaks. Aim for one good test, not coverage. | `🟡 risk` |
| `speed` | A big slowdown is a finding. A small speed gain is a suggestion. | `🟡 risk` for a big slowdown. `🔵 nit` for a small gain. |
| `lean` | Code that should not exist or should be smaller. Look for: dead code; a helper the repo already has (Reuse: name its path); a dependency for a few lines; an abstraction with one implementation; near-copies that must change together; one function with several unrelated jobs (split by job, not by line count); excess code (rebuilt helpers, tests that check nothing useful, or too many tests). When you fix behavior, update existing tests instead of adding new ones. If a PR has too much code, propose changes that make it smaller. | `🟡 risk` when it adds or keeps code. `🔵 nit` for a shorter form. |

## Check before you report

- Name the input or situation that goes wrong. No case, no finding.
- Re-read the lines. Confirm that the caller exists, the value can be empty, or
  the code is unused. Use grep to find callers.
- Propose the smallest fix that works. Prefer fixes that delete code. Do not add
  layers, frameworks, or config that the problem does not need.
- No style preferences. Do not write vague advice, such as "consider this."
- A shortcut marked `shortcut:` (or older `ponytail:`) that names its limit is a
  decision, not a finding. Report it only if the expected load crosses it.

## Report findings

| Label | Meaning | Blocks |
| --- | --- | --- |
| `🔴 bug` | Broken behavior or a security problem. | Always. |
| `🟡 risk` | Fragile behavior that can fail in a real case. | When it breaks at the expected load. |
| `🔵 nit` | Optional style or naming change. | Never. |
| `❓ q` | A question you need answered before you decide. | Only when its answer changes what to do. |

## Output format

| Part | Rule |
| --- | --- |
| Style | Use very simple English: short sentences and everyday words. Explain each technical term the first time you use it. |
| First line | `Round N. LEVEL: <LEVEL>. Target: OWNER/REPO PR #<number> at head SHA.` Use the full repo name and the full head SHA. |
| Second line | Start it with `What this change does:`. Write two or three sentences after it. |
| Findings | Use three groups, in this order. Skip an empty group. **Must fix:** each blocking finding. **Should fix:** other `🟡 risk`, including lean findings. **Nice to have:** `🔵 nit`, and any other `❓ q`. Number the findings across all groups, so the author can say "fix 2 and 5". |
| One finding | It has four parts, as in the example below. Each part is one or two short sentences. Do not repeat what the code does. Name the problem and the fix. |
| `Verdict:` | End with these three lines, in this order. First: `Verdict: Ship.` or `Verdict: fix 1 and 3 first.` Name each blocking finding by number. Use `Ship` only when no blocking finding remains. |
| `Lean:` | `Lean: -<N> lines possible.` Add this line when lean findings exist. |
| `Not checked:` | One line. Add it if something mattered and you could not check it. |
| No findings | Write the `Round N. LEVEL: <LEVEL>. Target: ...` line, then `What this change does:`, then `Looks good. Ship.`, then one line on what you checked. |

```text
2. **Empty token passes the check** (`src/auth.py:L42`, `🔴 bug`)
   - **What this is:** This check decides who may call the endpoint.
   - **Problem:** The check accepts an empty token, so a request with no token passes.
   - **Fix:** Reject an empty token before the check runs. The change is one line.
   - **If we skip it:** Anyone who sends an empty token can use the endpoint.
```

## Run the fix cycle

- Post each verdict as a review. Use the API command and the `COMMENT` rule in
  "Pull requests come first" in the `delegation-loop` skill. Use the head SHA
  that you read. `Ship` is `APPROVE`. A blocking finding is `REQUEST_CHANGES`.
- Run the fix cycle only when your brief says that the loop owns the PR. For any
  other PR, post the verdict, report it to the orchestrator, and stop.
- In the first line, N is 1 more than the N in the latest `Round` review on the
  PR. N is 1 if there is none. LEVEL is the next fix worker's LEVEL, from step 2
  of "Fix worker models" in the `delegation-loop` skill. Check the stall rule
  before you post. If it raises LEVEL, write `LEVEL: size-l.` in this verdict.
- If the latest `Round` review is at the current head SHA, do not review again.
  Start the fix worker with its findings.

1. If the stall rule says the PR is blocked, do not start a fix worker. Go to
   item 6. After a verdict with a blocking finding, start a fix worker. If LEVEL
   is `size-l` and you are `size-m`, do not start it. Stop and report. Use your
   runner's agent tool, or run `herdr agent start` in a new pane. See "Talk to
   an agent in a Herdr pane" in the `delegation-loop` skill. If you cannot start
   an agent, post the verdict, tell the orchestrator, and stop.
2. Give the fix worker a brief with the PR, branch, remote, numbered findings,
   and failing gate output. It makes its worktree and pushes to the same branch,
   as in "Pull requests come first" in the `delegation-loop` skill.
3. Wait for the push. Every 60 seconds, run this command to read the head SHA:
   `gh pr view <number> --repo OWNER/REPO --json headRefOid -q .headRefOid`.
   Stop when it differs from the reviewed SHA, or the fix worker ends. With no
   push, do steps 4 and 5 of "Fix worker models" in the `delegation-loop` skill.
4. If the SHA changed, review the new head SHA. That review is the next round.
5. Repeat until `Verdict: Ship`. Do not stop between rounds, except as items 1
   and 6 and step 2 of "Fix worker models" say.
6. Stall rule: follow step 3 of "Fix worker models". For a blocked PR, post the
   `Loop:` comment that step 3 names, then report it to the orchestrator.

Report the final verdict to the orchestrator. The owner merges. You never merge.
