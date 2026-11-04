# Contribute to Lupin

Lupin is a Python program with a Nix flake. Agent loop rules are in `AGENTS.md`.

## Set up

1. Install Nix with flakes enabled.
2. In the checkout, run `nix develop` for Python, `pytest`, and `redis-server`.
3. Run one test file: `pytest tests/test_route.py -q`.

## Run the gate

Every pull request must pass `nix flake check` and `nix develop -c python3 -m
pytest -v`. `nix flake check` checks only the host system. Put the exact result
in the pull request.

## Branch and pull request

- Branch from `origin/main`.
- Open the pull request against `main` on upstream (`origin`).
- Put `Closes #N` in the body when the change closes an issue.
- Use a Conventional Commit subject, for example `fix: rank quota logins by
  their most-used window`.

## Review

- One reviewer who is not the author approves the current head commit. An
  approval of an older commit does not count.
- If GitHub reports a conflict with `main`, merge `origin/main` into the branch
  and resolve the conflicts in that merge. Do not rebase.
- A reviewer never merges. The owner merges. A loop never merges.
- Fix each blocking finding and ask for a new review at the new head.

## Style

- Match the style of the file you edit. Do not reformat unrelated lines.
- Keep each change tied to its issue.
- Write documentation and comments in ASD-STE100 style: short sentences, simple
  words, one instruction per sentence.
- Add a test only for a behavior, a boundary, an error, or an invariant that a
  consumer can see. Do not test wiring or copied text.
