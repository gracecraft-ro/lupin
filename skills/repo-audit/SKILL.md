---
name: repo-audit
description: >-
  Audit a whole repository for bugs, security holes, scale problems, missing
  tests, and code to delete, merge, or split. The result is a one-shot report
  that changes no code.
compatibility: Requires read access to the repository.
---

# Audit a repository

Use this skill when the user asks for a review of a whole repo, a folder, or a
package. This skill reports only. It changes no code.

## Map first

- Audit what the user names: a folder, a package, or the whole repo. If the
  user names nothing, audit the whole repo.
- Read `AGENTS.md`, the README, the build and deploy config, and the dependency
  list. Read the entry points: main, routes, handlers, jobs, and CLI commands.
  Read the tests too.
- Find the expected load. Decide whether one person runs a script, or many users
  and processes run at once. Judge scale against that load. Say which load you
  assumed.
- Trace the main flows from start to end: where data comes in, what is stored,
  and what goes out. Read these paths fully: user input, money, auth, data
  writes, background jobs, and anything shared between processes.
- For a big repo, go deep where a mistake costs the most. Do not read file by
  file. Say which parts you did not read.

## Look for

1. **Bug:** wrong result, crash, or missed edge case (empty, zero, last item,
   rounding, time zone). Also look for callers that disagree with what a
   function returns, and for one rule applied two different ways.
2. **Risk:** security holes (injection, weak randomness, secrets in code,
   missing checks on user input). Also check for data loss, such as swallowed
   errors and writes in the wrong order. Also check for a group of writes that
   must all succeed or all fail, but does not.
3. **Scale:** fine for one user, wrong for many. Look for two requests that check
   a value, then write it, at the same time. Also look for work that every
   process repeats, and lists or memory that only grow. Also look for one query
   per item and for work that grows with the square of the input size. Also look
   for per-process state that must be shared.
4. **Missing test:** risky logic (a branch, parser, money, security, data
   writes) with no test that fails when it breaks. One good test is enough. Do
   not chase coverage.
5. **Speed:** big slowdowns are findings. Small wins, such as work repeated in a
   hot loop, are suggestions.
6. **Lean:** code that should not exist or should be smaller.
   - delete: dead code, unused options, flags and config, speculative features
   - reuse: two helpers that do the same thing. Keep one and name its path.
   - the standard library / native: the standard library or the platform already
     does it. A dependency does work that a few lines could do.
   - build only what is needed now. Look for:
     - an interface with one implementation
     - a factory that builds only one kind of object
     - a wrapper that only passes calls through
   - merge: near-copies that must change together
   - split: one function or class that does several unrelated jobs, so it is hard
     to read or test. Split by job, never by line count, and never into helpers
     that exist only to shorten a function.

## Check before you report

- Every finding needs a concrete case: this input or situation gives this wrong
  result. No case, no finding.
- Before you call code unused, search the whole tree. Include tests, fixtures,
  config, and string or dynamic references.
- A `shortcut:` comment that names its limit is a decision, not a finding. The
  exception: the expected load already crosses that limit. The `shortcut-debt`
  skill defines the marker.
- Propose the smallest fix that works. Prefer fixes that delete code. Do not add
  layers, frameworks, or config that the problem does not need.
- Do not report style taste, opinions, or vague worries.

## Report

Use short sentences and everyday words. Explain each technical term the first
time you use it. The reader may never have seen this code.

Start with `What this repo does:`. Write two or three sentences after it. Add the
load you assumed.

Then list the findings in three groups, most important first. Skip an empty
group.

- **Must fix:** bug, security, data loss, or a break at the expected load.
- **Should fix:** risky code with no test, real slowness, duplication, a
  function that mixes jobs, or extra code.
- **Nice to have:** small speed-ups and shorter forms.

Number the findings across all groups, so the user can say "fix 2 and 5". Report
at most 20 findings. If you leave out smaller ones, say how many.

Write each finding with four parts. Each part has one or two short sentences.
In the heading, give the file, the line, and one kind of check. The kind is one
of: bug, risk, scale, missing test, speed, or lean.

```text
2. **Orders land on the wrong day** (`billing/close_day.py:L40-52`, bug)
   - **What this is:** At midnight this job closes the day and bills all orders of that day.
   - **Problem:** It takes "today" from the server clock, which runs in UTC. An order placed at 00:30 in Berlin is billed on the day before.
   - **Fix:** Compute the day once in the shop's time zone: `datetime.now(ZoneInfo("Europe/Berlin")).date()`. One line, nothing else changes.
   - **If we skip it:** Late orders show the wrong date, and accounting fixes them by hand.
```

End with:

- `Verdict:` one line. Say healthy, or name the first thing to fix.
- `Lean: -<N> lines, -<M> dependencies possible.` Add this line when lean
  findings exist.
- `Not checked:` one line. List the parts you did not read or could not run.

If nothing is wrong, write `What this repo does:`, then `Healthy. Nothing to fix.`,
and one line on what you checked.
