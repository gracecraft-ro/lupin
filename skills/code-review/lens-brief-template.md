# Lens brief template

Fill each `<...>` value. Send the filled text as the reviewer prompt. The
reviewer follows the `code-review` skill.

---

Review <OWNER/REPO> PR #<PR> at head <FULL SHA>. It answers issue #<ISSUE>.
Changed files: <FILES>. Post one GitHub review on that exact commit.

Read the repository's `AGENTS.md` and the issue acceptance checks first. Then
run the `code-review` skill. Your review starts with
`Round <N>. LEVEL: <LEVEL>. Target: <OWNER/REPO> PR #<PR> at <FULL SHA>.` and
names the model that did the review on the `Model:` line.

Before you review, run the visual evidence gate from the `visual-evidence`
skill:

```sh
<checkout>/skills/visual-evidence/scripts/pr-evidence-check.sh \
  --repo <OWNER/REPO> <PR>
```

A FAIL line is a BLOCKING finding. The pictures must be in a comment on the PR,
not on the linked issue.

Work in a scratch worktree, checked out at the PR head. Use the dependency
directory that the other worktrees share. Link it, and remove only the link at
the end. Run every check in the repository's own shell, as its `AGENTS.md`
says.

Checks, each with the command and its output in the review:

1. Read `git diff <base>...HEAD` in full.
2. Run the touched tests and the repository's type and lint checks on the
   changed files.
3. Show that a new test fails on the base commit and passes at the head.
4. Probe the changed behaviour on at least three inputs the author did not
   test: another shape, another unit, a second item, another theme.
5. `grep -rn` every caller of each changed function and name the callers that
   are affected.
6. Open every picture. Download it with
   `T=$(gh auth token); curl -sL -H "Authorization: Bearer $T" -o FILE URL`,
   then open the file and describe what you see. Run `cmp` on each before and
   after pair.
7. Name what the change does to features it does not mention. For each answer,
   give the search or the probe that shows it.
8. A failing test: compare it with the base commit and with `origin/main`
   before you call it a regression.

Label every finding BLOCKING (with the input, the output it gives, and the
output it must give) or FOLLOW-UP. A PR gets at most three review rounds. From
round three, report only BLOCKING findings. Name the rest as FOLLOW-UP.

Post with
`gh api repos/<OWNER/REPO>/pulls/<PR>/reviews -f event=<APPROVE|REQUEST_CHANGES>
-f commit_id=<FULL SHA> -F body=@FILE`. Then run
`<checkout>/skills/code-review/scripts/review-lint.sh --repo <OWNER/REPO> <PR>`
and fix the review if a line says FAIL.

Never merge. Remove the scratch worktree. Final message: the review id, the
verdict, each BLOCKING item with its input, and what you did not check.
