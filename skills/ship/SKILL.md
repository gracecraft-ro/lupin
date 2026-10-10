---
name: ship
description: >-
  Finish a delegated GitHub issue from its acceptance checks through verification
  and a clear report. Use this skill when implementing or finishing an issue,
  preparing its pull request, or reporting completed work.
compatibility: Requires the repository's tools and GitHub CLI. Lupin and Redis are used when fleet claims are configured.
---

# Ship a delegated issue

Use this skill to implement an issue or fix a finding on an open pull request.
Follow the repository's `AGENTS.md`, issue workflow, and release rules.

## Check the issue and checkout

1. Read the issue and its latest comments. For a Lupin-managed repo, get the
   current context with:

   ```sh
   lupin review-route --prefetch 123 --repo OWNER/REPO
   ```

   Use `lupin place 123 --json` for a model, effort, and machine suggestion.
   It does not claim or start the task.
2. Check `pwd`, the branch, and `git status`. Use the issue's worktree. Do not
   edit a shared main checkout while another loop may use it.
3. Read the acceptance checks. If they are unclear, report the gap before
   coding. Do not replace the requested behavior with a smaller change.
4. For a review fix, read the PR's latest comments and reviews. Work on the
   same branch and PR. Do not open a second PR for the fix.
5. If the orchestrator gave you a Lupin claim, use it. Do not claim the issue
   again. If you own the claim, renew it while work continues. The claim owner
   updates the GitHub `claimed` label and comment with machine, worktree, and
   branch. Do not post a duplicate claim comment.

## Implement and verify

- Inspect the relevant code and tests before editing.
- Make the smallest change that meets the acceptance checks.
- Add or update a regression test for a behavior change.
- Run the repo's documented gate. Then run the changed feature and observe
  the result. A passing build or test alone does not prove it works.
- Read the final diff. Check issue and PR comments again before reporting.

For a visual change, open the real surface and capture before and after.
Report if you could not view it.

For a 3D change, render a contact sheet with the useful views. Use top, bottom,
front, side or 90-degree, and 45-degree views when they help show the change.
Compare before and after from the same views. Attach the sheet to the PR.

## Save and attach evidence

Save every screenshot, render, and other artifact under
`evidence/<issue-number>/`. Use `evidence/<issue-number>/` for every issue.
Check `.gitignore`. Add `evidence/` if it is not ignored. Keep these local
copies out of commits.

GitHub CLI 2.99.0 and newer supports `gh issue comment --attach`. Check
`gh version` if the option is missing; upgrade instead of silently skipping
an attachment. Attach evidence to the PR conversation:

```sh
gh issue comment <PR_NUMBER> --repo OWNER/REPO \
  --attach evidence/123/contact-sheet.png \
  --body-file report.md
```

Attach each relevant screenshot or contact sheet. Do not attach an artifact you
did not inspect. For a non-image artifact, include a link to its approved
repository or artifact-store location.

## Commit and open the pull request

Commit the change.

Choose the push flow. First, check push rights on the upstream repo.
The upstream repo is the one `origin` points to.

1. Find the upstream repo name with `git remote get-url origin`. Replace
   `OWNER/REPO` with its owner and repo name. Then run:

   ```sh
   gh api repos/OWNER/REPO --jq .permissions.push
   ```

2. If the result is `true`, use the direct flow below, unchanged.
3. If the result is `false`, use the fork flow below.
4. If the command fails or prints anything else, stop. Report the output.
   Do not guess.

If direct push is allowed, push the branch and open or update
its PR. Link the issue with `Closes #123`.

Fork flow. Use it only when the result is `false`. Never push to `origin`
in this flow.

1. Check that a `fork` remote exists. Run `git remote get-url fork`. If it
   fails, stop. Report the missing remote. Never run `git remote add`.
2. Push the branch to the fork. Find the current branch with
   `git branch --show-current`. Then run `git push -u fork <branch>`.
3. Open the PR on the fork. Set `<repo>` to the repo name in the fork URL
   from step 1. Run:

   ```sh
   gh pr create --repo gracecraft-ro/<repo> --base release/next \
     --head <branch> --title "..." --body "..."
   ```

   Never use an upstream `--repo` in this flow.
4. If the push fails, report the branch name and the commit range. Do not
   merge.

If the repo does not allow fork PRs, keep the local branch and report its name
and commit range. A local commit is not a pull request and is not shipped.
Do not merge the PR. The orchestrator reviews and merges it.

## Report and release

Post a short report to the issue. Include:

- **Highlights:** what changed and the main files.
- **Evidence:** exact tests, commands, and observed output.
- **Decisions:** important choices or limits.
- **Next:** PR review, merge, owner action, or `None`.

Keep the issue open while the PR waits for review or merge. Do not say it is
shipped until the orchestrator approves and merges it. If you own the Lupin
claim, release it when your work ends, remove the `claimed` label, and comment
with the PR number or stop reason. The orchestrator owns the final merge.
