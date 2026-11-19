---
name: ship
description: >-
  Always use this to ship a new feature or fix.
compatibility: Requires the repository's tools and GitHub CLI. Lupin and Redis are used when fleet claims are configured.
---

# Shipping it!

## Check the issue and checkout

1. Read the issue (when available) and its latest comments. For a Lupin-managed repo, try running this:

   ```sh
   lupin review-route --prefetch 123 --repo OWNER/REPO
   ```

   In the absence of an issue, quickly create a brief issue and claim it. Keep it concise.

2. Check `pwd`, the branch, and `git status`. Use the issue's worktree. Unless directed
   by the owner, avoid editing a shared main checkout that another loop may use. Work in a dedicated worktree if you are not on one already.
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
- For ALL visual / spatial changes, open a draft PR as soon as you have any screenshots or gifs to share of the proposed final result (that you have inspected and confirm match expectations)! It is critical to do this as early as is feasible in the process; do not wait for the full gate to finish running to start your draft, otherwise your work risks being lost. By sticking to this policy, you may also receive valuable early feedback from the owner from occasion. Of course, update the PR and promote it from a draft as soon as it is fully polished and ready for review.
- Add a test only if no test for that behavior fails when you undo your change. Check:
  undo the change, run the tests for that area, and read which tests fail. If
  an existing test for the same behavior fails, add no test; unrelated failures
  do not count. If no test for that behavior fails, add the smallest test to
  the existing file for that area.
  Do not create a test file when one exists for the area.
  Do not write an issue number in a test title or a test file name. Put the
  test count in both the PR and the issue report as `tests: +A -R`.
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
Titles and any captions should be baked into the contact sheet. 
Compare before and after from the same views. Attach the sheet to the PR. 
For 3d changes that involve multiple related objects, such as one new object designed for an existing object, include any reference object.

## Save evidence

Store screenshots, renders, and other artifacts in `evidence/<issue-number>/`. If`.gitignore` lacks `evidence/`, add it. _Do not commit these copies._

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

   Use `Closes #<issue>` when the PR completes the issue's acceptance checks.
   Use `Refs #<issue>` only when work remains after this PR merges, and name
   that work in the PR body.

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

## Attach screenshots and gifs to the PR

A visual PR must have visual evidence in order to be approved. If UI elements are modified in a way that changes user interactions at all, such as adding new functionality to the interface, attach a gif. If you only changed the appearance of a button, screenshots suffice. If your repo has specific screenshot / gif guidance, that can be a helpful starting point. 

1. Find the PR that holds the change. For the PR in the current branch, run the
   command `gh pr view --json number,url --jq '.number, .url'`. The URL is
   `https://github.com/<owner>/<repo>/pull/<number>`. Use that `<owner>/<repo>`,
   not the fork name, unless the PR is on the fork.
2. Open each screenshot and sanity check it before commenting; you may detect embarrassing mistakes this way.
   Attach it along with a detailed comment in GitHub on the PR and issue. Show the before
   and after, or the new screen if there is no before. Say what to look at. Use
   the same path in the body and in `--attach`:

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
Do not skip the evidence. Post that report as a PR comment before `lupin stop`.
Include the PR number, machine, and absolute path of each image and gif for redundancy.

If you could not share a screenshot or gif, post a PR comment that says so and why.
Say so in your report too. The orchestrator must escalate.

Do not attach a non-image file, such as a 3D model. Before `lupin stop`, copy it
to a durable location outside the worktree. In the PR comment, give the machine
and the absolute path of the durable copy.

## Report and release

Post a short report to the issue. Include:

- **Highlights:** what changed and the main files.
- **Evidence:** exact tests, commands, and observed output.
- **Decisions:** important choices or limits, with full context for anyone new to the codebase.
- **Next:** PR review, merge, owner action, or `None`.

A merged PR with `Closes #N` closes its issue. The owner's review and merge are
not work left, so do not comment that an issue is open for owner review. Do not
say it is shipped before the merge. If you own the Lupin claim (a lock on the
issue), release it when your work ends. Remove the `claimed` label. Then comment
with the PR number or stop reason. If the label edit fails, report it and stop.
