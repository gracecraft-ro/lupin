# Sync brief template

Fill each `<...>` value. Send the filled text as the worker prompt.

---

A sync for <OWNER/REPO> PR #<PR>. Upstream `main` moved, and GitHub now reports
this PR as CONFLICTING. Merge `origin/main` into the PR branch. Resolve only
conflicts that are mechanical. Never rebase. Never merge the PR. Never change a
label, an issue, or a review. No force push.

Work in a scratch worktree. Use the repository's own commands for the checks:

1. From `<checkout>`:
   `git fetch origin main`, `git fetch origin pull/<PR>/head`, then
   `git worktree add <path> -b sync-<PR> FETCH_HEAD`.
2. Dependencies: if the worktree has no dependency directory, link the shared
   one. Remove the link at the end with `test -L node_modules && unlink
   node_modules`. Never run the package manager install through a link. Never
   run `rm -rf` on a link.
3. `git merge origin/main`. For each conflicted file, read both sides with
   `git diff` and `git log origin/main -3 -- FILE`. Keep both intents. When main
   already has the change in another form, keep main's version and say so.
   Stop and run `git merge --abort` when more than four files conflict, or when
   a conflict in core code is not clearly additive. Report the files and the
   reason.
4. Run the repository's gate from its `AGENTS.md`, limited to the files the
   merge changed and the tests that cover them. Show the command and the counts.
   A test that also fails on `origin/main` is not this merge's failure. Say so
   and name the `origin/main` SHA you compared with.
5. Commit message: `merge origin/main into <branch>`, one line that lists the
   resolved files, and the trailer for the model that did the work.
6. Push fast-forward to the same fork branch:
   `git push fork sync-<PR>:<head-branch>`. If the push is refused, report the
   exact error. Do not force.
7. Check the result: `gh pr view <PR> --repo OWNER/REPO --json
   mergeable,mergeStateStatus,headRefOid`. When the state is UNKNOWN, wait 30
   seconds and ask again, up to three times.
8. Remove the scratch worktree. Unlink the dependency directory first.

Final message: the new head SHA, the files you resolved and how, the check
counts, the mergeable state, what you did not verify, and whether the PR had an
approval before your push. A sync is a push, so the PR needs a new review.
Post no review and no comment.
