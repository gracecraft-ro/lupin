---
name: shortcut-debt
description: >-
  List every shortcut comment in the repository as a debt ledger. The result is
  a one-shot report that changes nothing.
compatibility: Requires a shell with grep and access to the repository.
---

# List shortcut debt

## The marker

A deliberate shortcut gets a code comment. The comment names the limit and the
trigger that says when to upgrade:

```text
# shortcut: <limit>, <upgrade trigger>
```

Example:

```python
# shortcut: one worker only, move to a shared queue when a second worker starts
```

## Scan

Grep the repo for comment markers. Skip `.git`, `node_modules`, and build output:

```sh
grep -rnE --exclude-dir=.git --exclude-dir=node_modules --exclude-dir=dist --exclude-dir=build '(#|//|/[*]) ?(shortcut|ponytail):' .
```

Add other comment prefixes if your stack uses them. The grep also matches
`ponytail:`, an older marker name.

If the user names a different marker word, grep for that word instead.

Each hit is one ledger row. The comment prefix excludes prose that only
mentions the marker. Skip a hit that is not a deferral, such as a note
about a keyboard shortcut.

## Output

One row per marker, grouped by file:

`<file>:<line>, <what was simplified>. limit: <the limit named>. upgrade: <the trigger to revisit>.`

Take the limit and the upgrade trigger from the comment. To add an
owner to a row, run `git blame -L<line>,<line> <file>`.

Tag any marker that names no upgrade trigger with `no-trigger`. Such markers
are easy to forget.

End with `<N> markers, <M> with no trigger.` If nothing is found, write
`No shortcut debt. Clean ledger.`

## Boundaries

This skill reads and reports only. It changes nothing. Write the ledger to a
file only when the user asks for one.
