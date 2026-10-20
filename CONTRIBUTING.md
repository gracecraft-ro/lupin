# Contribute to Lupin

Lupin is a Python program with a Nix flake. This guide is for a person who
changes the code. It is not the agent loop rules; those are in `AGENTS.md`.

## Set up

1. Install Nix with flakes enabled.
2. Enter the development shell from the checkout:

   ```sh
   nix develop
   ```

   The shell has Python, `pytest`, `redis-server`, and the runtime tools.
3. Run one test file:

   ```sh
   pytest tests/test_route.py -q
   ```

## Run the gate

Every pull request must pass the repo gate:

```sh
nix flake check
nix shell nixpkgs#python3Packages.pytest -c pytest -v
```

`nix flake check` builds the package and runs the tests in the Nix sandbox.
It checks only the host system. Put the exact result in the pull request.

## Branch and pull request

- Make a branch from `origin/main`.
- Open the pull request against `main` on the upstream repository.
- The loop starts new work from `origin/release/next` when that branch exists
  on the remote. Otherwise it uses the default branch.
- Put `Closes #N` in the pull request body when the change closes an issue.
- Use a Conventional Commit subject, for example
  `fix: rank quota logins by their most-used window`.

## Review

- One reviewer who is not the author approves the current head commit.
  An approval of an older commit does not count.
- The branch must contain the current `main`. Merge `origin/main` into the
  branch; do not rebase.
- A reviewer never merges. The owner merges.
- Fix each blocking finding and ask for a new review at the new head.

## Style

- Match the style of the file you edit. Do not reformat unrelated lines.
- Keep each change tied to its issue.
- Write documentation and comments in ASD-STE100 style: short sentences,
  simple words, one instruction per sentence.
- Add a test only for a behavior, a boundary, an error, or an invariant that
  a consumer can see. Do not test wiring or copied text.
