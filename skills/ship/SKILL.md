---
name: ship
description: >-
  Finish a delegated GitHub issue from its acceptance checks through verification
  and a clear report. Use this Lupin skill when implementing or finishing an issue,
  preparing its pull request, or reporting completed work.
compatibility: Requires the repository's tools and GitHub CLI. Lupin and Redis are used when fleet claims are configured.
---

# Ship a delegated issue

Follow the repository's `AGENTS.md`, issue workflow, and release rules. Start
every GitHub comment that you post with `Loop:`.

## Check the issue and checkout

1. Read the issue and its latest comments. For a Lupin-managed repo, run:

   ```sh
   lupin review-route --prefetch 123 --repo OWNER/REPO
   ```

   Use `lupin place 123 --json` for a model, effort, and machine suggestion. It
   does not claim or start the task.

2. Check `pwd`, the branch, and `git status`. Use the issue's worktree. Unless directed
   by the owner, avoid editing a shared main checkout that another loop may use.
3. Read the acceptance checks. If they are unclear, report the gap before
   coding. Do not replace the requested behavior with a smaller change.
4. For a review fix, read the PR's latest comments and reviews. Use the same
   branch and PR. Do not open a second PR.
5. If the orchestrator gave you a Lupin claim, use it. Do not claim again. If
   you own it, renew it during work. The claim owner updates the `claimed` label
   and comment (machine, worktree, branch). Post no duplicate claim comment.

## Implement and verify

- Before you edit, inspect the relevant code and tests. List every place the
  change must reach (callers, tests, fixtures, config, exports). Make the
  smallest change that meets the acceptance checks. Leave out no part the task
  needs.
- Take the first option that fully works, in this order:
  1. Does it need to exist? Add no feature, option, abstraction, wrapper,
     config, or "for later" code that nobody asked for.
  2. Does the codebase have it? Reuse it.
  3. Does the standard library or the platform have it? Use it.
  4. Is an installed dependency enough? Use it. Never add one for a few lines.
  5. Can it be one line? Write it. Otherwise, write the minimum code that works.
- For a bug fix, grep every caller first. Fix the root cause once, in shared
  code. Keep validation and error handling on moved code. Never cut validation
  where outside data enters, data-loss handling, security, or accessibility.
- Write a comment only for the why, on one line. Mark a shortcut with a known
  limit: `shortcut: <limit>, <upgrade trigger>`.
- Add or update a regression test for a behavior change. New non-trivial logic
  (a branch, a loop, a parser, money, or security) gets one small test or an
  assert. Trivial changes need none.
- Run the repo's documented gate, then run the changed feature and observe the
  result. Read the final diff. Recheck issue and PR comments before reporting.
- The final report says what you skipped, what you did not check, and any risk
  the user must know.

A visual change is any change a person can see. Open the real surface and
capture before and after. For a new screen, capture the new screen. Report if
you could not view it. If you decide a change is not visual, say so and why in
your report and in a PR comment.

For a 3D change, render a contact sheet with the useful views. Use top, bottom,
front, side or 90-degree, and 45-degree views when they help show the change.
Compare before and after from the same views. Attach the sheet to the PR.

## Save evidence

Save screenshots, renders, and other artifacts in `evidence/<issue-number>/`. If
`.gitignore` lacks `evidence/`, add it. Do not commit these copies.

## Commit and open the pull request

Commit the change. If the commit, the PR lookup, or the PR create fails, report
the error. Then go to "Report and release" below.

1. Read the repo's `AGENTS.md` for its push and PR rule. If it names one, follow
   it. Otherwise use the default: push to the fork, and open the PR on upstream
   (`origin`). The rest of this step and steps 2 to 4 are the default flow.
   After the PR opens, go to step 5.

   Run `git remote get-url fork`. The URL must not be the upstream repository.
   It must be `https://github.com/<owner>/<repo>.git`. If it is not, or there is
   no `fork` remote, stop and report. Never run `git remote add`. `FORK_OWNER`
   is `<owner>`. Workers never push to `origin` or `main`.

2. Find the feature branch with `git branch --show-current`. If the output is
   empty (detached, no branch name) or `main`, stop and report. Otherwise, run
   `git push -u fork <branch>`. Never push any other ref.
3. Open the PR on upstream. `<repo>` is the repo name, `OWNER` is the upstream
   owner, and `FORK_OWNER` is the fork owner from step 1. Look for an open PR:

   ```sh
   gh pr list --repo OWNER/<repo> --head <branch> --state open \
     --json number --jq '.[0].number // empty'
   ```

   If the lookup exits non-zero, do not create a PR. If the output is not empty,
   use it as `<PR_NUMBER>` and skip the create. If it exits 0 and is empty, run:

   ```sh
   gh pr create --repo OWNER/<repo> --base main \
     --head FORK_OWNER:<branch> --title "..." --body "Closes #<issue>. ..."
   ```

   Use `Closes #<issue>` when the PR finishes the issue. Use `Refs #<issue>`
   only when work stays open.

4. Request review with one comment on the PR:

   ```sh
   gh pr comment <PR_NUMBER> --repo OWNER/<repo> \
     --body "Loop: Review requested. Head: <sha>."
   ```

   `<PR_NUMBER>` is the number at the end of the PR URL that `gh pr create`
   prints. `<sha>` is the output of `git rev-parse HEAD`. If the comment fails,
   report the error and the PR number. Then go to step 5. Do not merge. Do not
   wait for approval. The owner merges the PR.

5. Attach the screenshots for every visual change. See "Attach screenshots to
   the PR" below. Then go to "Report and release" below.
6. If every push route fails, or PR creation fails, report the branch name and
   the commit range. If the repo does not allow fork PRs, also keep the local
   branch. A local commit is not a pull request and is not shipped. Then go to
   "Report and release" below.

## Attach screenshots to the PR

A visual PR must have screenshots. If the repo's `AGENTS.md` or a repo skill
covers screenshots, follow it. Otherwise follow this section.

1. Find the PR that holds the change. For the PR in the current branch, run the
   command `gh pr view --json number,url --jq '.number, .url'`. The URL is
   `https://github.com/<owner>/<repo>/pull/<number>`. Use that `<owner>/<repo>`,
   not the fork name, unless the PR is on the fork.
2. Open each screenshot. Look for a secret, token, private address, or personal
   data. After a crop or recapture, reopen the final image and look again before
   upload. Write the comment in `evidence/<issue-number>/report.md`. Show before
   and after, or the new screen if there is no before. Say what to look at. Use
   the same path in the body and in `--attach`:

   ```md
   Loop: Screenshots for PR #123.
   Before: ![before](./evidence/123/before.png)
   After: ![after](./evidence/123/after.png)
   ```

3. Attach the files and post the comment:

   ```sh
   gh pr comment <PR_NUMBER> --repo <owner>/<repo> \
     --attach './evidence/123/before.png#before' \
     --attach './evidence/123/after.png#after' \
     --body-file ./evidence/123/report.md
   ```

   Text after `#` is the image's alt text. A path that the body names is
   replaced with the uploaded image. An attached file that the body does not
   name goes at the end. One command takes up to 50 files.

4. Run `gh pr view <PR_NUMBER> --repo <owner>/<repo> --comments`. Each image in
   your comment must now be a `https://github.com/user-attachments/...` link. A
   `./evidence/...` path means the upload did not happen: follow the next
   paragraph. A 403 for the link is expected on a private repo. Do not report
   that you attached the files until you checked.

An upload needs write access to the repo that holds the PR. The error message
`attaching files requires write access to the repository` means your token has
none. A fork token can post a plain comment on an upstream PR but cannot upload.
If your default login is read-only on the repo, use the upload token that the
repo's `AGENTS.md` names. Do not skip the screenshots. Keep the image files in
`evidence/<issue-number>/`. In your report, say that the upload failed. Post
that report as a PR comment before `lupin stop`. Do not leave it only in
`evidence/<issue-number>/report.md`: the stop deletes it with the worktree.
Include the PR number, machine, and absolute path of each image. The
orchestrator must copy the image files before stopping the loop.

GitHub CLI 2.99.0 and newer has `--attach`. If it is missing, check the version
with `gh --version`. Upgrade, or report that you cannot attach. Do not skip it
silently. Attach only a screenshot you opened and checked.

If you could not capture a screenshot, post a PR comment that says so and why.
Say so in your report too. The orchestrator must escalate.

Do not attach a non-image file, such as a 3D model. Before `lupin stop`, copy it
to a durable location outside the worktree. In the PR comment, give the machine
and the absolute path of the durable copy.

## Report and release

Post a short report to the issue. Include:

- **Highlights:** what changed and the main files.
- **Evidence:** exact tests, commands, and observed output.
- **Decisions:** important choices or limits.
- **Next:** PR review, merge, owner action, or `None`.

Keep the issue open while the PR waits for review or merge. Do not say it is
shipped until the owner merges it. If you own the Lupin claim (a lock on the
issue), release it when your work ends. Remove the `claimed` label. Then comment
with the PR number or stop reason. If the label edit fails, report it and stop.
