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
Start every GitHub comment that you post with `Loop:`.

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
- Take the first option that fully works, in this order:
  1. Does it need to exist? Skip features and options that nobody asked for.
  2. Does the codebase already have it? Reuse it.
  3. Does the standard library or the platform have it? Use it.
  4. Is an installed dependency enough? Use it. Never add a dependency for a few lines.
  5. Can it be one line? Write one line.
  6. Otherwise, write the minimum code that works.
- Keep the solution small. Do not leave out any part the task needs.
- Do not add an abstraction, wrapper, option, config, or "for later" code that nobody asked for.
- Write a comment only for the why behind the code. Keep it to one line.
- Add or update a regression test for a behavior change.
- Run the repo's documented gate. Then run the changed feature and observe
  the result. A passing build or test alone does not prove it works.
- Read the final diff. Check issue and PR comments again before reporting.
- List every place your change must reach (callers, tests, fixtures, config, exports) before you edit.
- For a bug fix, grep every caller first. Fix the root cause once, in the
  shared code.
- When you move code, keep its error handling and validation.
- New non-trivial logic (a branch, a loop, a parser, money, or security) gets one
  small test or an assert. Trivial changes need none.
- A shortcut with a known limit gets a comment: `shortcut: <limit>, <upgrade trigger>`.
- Never cut validation at points where outside data enters the system,
  data-loss handling, security, or accessibility.
- The final report says what you skipped, what you did not check, and any risk
  the user must know.

For a visual change, open the real surface and capture before and after. For a
new screen with no before, capture the new screen. A visual change is any change
that a person can see. Examples: a screen, a page, a control, a style, a
rendered file, or a 3D model. Report if you could not view it. A PR for a
visual change needs the screenshots. See "Attach screenshots to the PR". If you
decide that a change is not visual, say so in your report and in a PR comment.
Give the reason.

For a 3D change, render a contact sheet with the useful views. Use top, bottom,
front, side or 90-degree, and 45-degree views when they help show the change.
Compare before and after from the same views. Attach the sheet to the PR.

## Save evidence

Save every screenshot, render, and other artifact under
`evidence/<issue-number>/`. Use `evidence/<issue-number>/` for every issue.
Check `.gitignore`. Add `evidence/` if it is not ignored. Keep these local
copies out of commits. You attach the images to the PR after you open it. See
"Attach screenshots to the PR".

## Commit and open the pull request

Commit the change. If the commit fails, report the error. Then go to "Report and
release" below.

The repo's `AGENTS.md` may have a rule for where to push and open the PR. In
the Lupin loop it is rule 1 under "Pull requests, review, and handoff". If it
exists, follow it, including its fallback to the fork when upstream denies the
push. It takes precedence over the next paragraph and over steps 1 to 4. In
the Lupin loop, it opens the PR on upstream, the repo that the PR belongs to. A
PR on a fork does not get reviewed. After the PR opens, go to step 5.

Workers never push to `origin` or `main`. Push the feature
branch to the fork. Open the PR on the fork. Then request review.

1. Run `git remote get-url fork` (the fork is the GitHub copy you push to). It
   must print `https://github.com/gracecraft-ro/<repo>.git`. If it does not, stop
   and report. Never run `git remote add`.
2. Push only the feature branch. Find it with `git branch --show-current`.
   If the branch is empty, detached (no branch name), or `main`, stop. Report
   it.
   Then run `git push -u fork <branch>`. Never push any other ref.
3. Open the PR on the fork. Set `<repo>` to the repo name in the fork URL
   from step 1. Look for an open PR on the branch first. Run:

   ```sh
   gh pr list --repo gracecraft-ro/<repo> --head <branch> --state open \
     --json number --jq '.[0].number // empty'
   ```

   If the lookup exits non-zero, report the error. Then go to "Report and
   release" below. Do not create a PR.

   If the output is not empty, skip the create step. Use that number as
   `<PR_NUMBER>`. If it exits 0 and is empty, run:

   ```sh
   gh pr create --repo gracecraft-ro/<repo> --base main \
     --head <branch> --title "..." --body "Closes #<issue>. ..."
   ```

   The orchestrator closes the issue by hand if the merge does not close it.

   Never use an upstream `--repo` in this flow. If it fails, report the error.
   Then go to "Report and release" below.
4. Request review with one comment on the PR. Run:

   ```sh
   gh pr comment <PR_NUMBER> --repo gracecraft-ro/<repo> \
     --body "Loop: Review requested. Head: <sha>."
   ```

   If it fails, report the error and the PR number. Then go to step 5.

   `<PR_NUMBER>` is the number at the end of the PR URL that `gh pr create`
   prints. If step 3 skipped `gh pr create`, use the number from the lookup in
   step 3. `<sha>` is the output of `git rev-parse HEAD`. Then go to step 5. Do
   not merge. Do not wait for approval. The orchestrator dispatches a reviewer.
5. Attach the screenshots. See "Attach screenshots to the PR" below. Do this
   for every visual change. Then go to "Report and release" below.
6. If every push route fails, or PR creation fails, report the branch name and
   the commit range. Then go to "Report and release" below.

If the repo does not allow fork PRs, keep the local branch and report its name
and commit range. A local commit is not a pull request and is not shipped.
Do not merge the PR. The orchestrator reviews and merges it.

## Attach screenshots to the PR

Do this after the PR exists, for every visual change. A reviewer cannot judge a
visual change from the diff. A visual PR with no screenshots is not ready. If
the repo's `AGENTS.md` or a repo skill says how to capture or attach
screenshots, follow it. Otherwise follow this section.

1. Get the repo and the number of the PR that holds the change. Use the PR URL.
   For the PR in the current branch, run
   `gh pr view --json number,url --jq '.number, .url'`. The URL is
   `https://github.com/<owner>/<repo>/pull/<number>`. Use that `<owner>/<repo>`.
   Do not use the fork name unless the PR is on the fork.
2. Open each screenshot. Look for a secret, token, private address, or personal
   data. If you crop or recapture an image, reopen the final image.
   Check it again for secrets, tokens, private addresses, and personal data
   before upload.
   A public repo shows the image to everyone. Write the comment in
   `evidence/<issue-number>/report.md`. Show before and after. For a new screen
   with no before, show the new screen. Say what the reader should look at. Use
   the same path in the body and in `--attach`:
   ```md
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
4. Run `gh pr view <PR_NUMBER> --repo <owner>/<repo> --comments`. In your
   comment, each image must now be a `https://github.com/user-attachments/...`
   link. A `./evidence/...` path means the upload did not happen. Then follow
   the paragraph below. A 403 for the link is expected on a private repo. Do not
   report that you attached the files until you checked.

An upload needs write access to the repo that holds the PR. If the command
fails with `attaching files requires write access to the repository`, your
token cannot upload there. A token for a fork can post a plain comment on an
upstream PR, but it cannot upload. Do not skip the screenshots. Keep the image
files in `evidence/<issue-number>/`. In your report, say that the upload failed.
Post that report as a PR comment before `lupin stop`; do not leave it only in
`evidence/<issue-number>/report.md`, which the stop deletes with the worktree.
Include the PR number, machine, and absolute path of each image so the
orchestrator can copy and attach the files with its own token. The orchestrator
must copy the image files before stopping the loop.

GitHub CLI 2.99.0 and newer has `--attach`. Run `gh --version` if the option is
missing. Upgrade, or report that you cannot attach. Do not skip it silently.
Attach only a screenshot you opened and checked.

If you could not capture a screenshot, post a PR comment that says so and why.
Say so in your report too. The orchestrator then treats the PR as blocked.

Do not attach a non-image file, such as a 3D model. Before `lupin stop`, copy
it to a durable location outside the worktree. Do not leave its only copy
under an ignored worktree path. In the PR comment, give the machine and the
absolute path of the durable copy.

## Report and release

Post a short report to the issue. Include:

- **Highlights:** what changed and the main files.
- **Evidence:** exact tests, commands, and observed output.
- **Decisions:** important choices or limits.
- **Next:** PR review, merge, owner action, or `None`.

Keep the issue open while the PR waits for review or merge. Do not say it is
shipped until the orchestrator approves and merges it. If you own the Lupin
claim (a lock on the issue), release it when your work ends. Remove the
`claimed` label. Then comment with the PR number or stop reason. If the label
edit fails, report it and stop. The orchestrator owns the final merge.
