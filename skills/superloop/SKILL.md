---
name: superloop
description: >-
  Supervise Lupin loops on one machine, by hand or with lupin supervise. Use it when asked to supervise loops.
compatibility: >-
  Requires Lupin, Herdr, and the delegation-loop skill on the supervisor host. Redis is required for ledger records.
---

# Superloop

The superloop is the overseer _of the orchestrators_ of delegation loops for each project. Two parts do the work:

- `lupin supervise` runs fixed rules. It quickly checks each loop and sends the nudge. It uses no model.
- An agent with this skill does the steps that need judgment: a blocked loop, an alert, dynamic priciing, or an unclear reply.

Do not copy the rule text into other files. The text lives in code. Open `NUDGE_TEXT` in `<checkout>/src/lupin/supervise.py` and copy the text inside the quotes. Do not copy the Python.

## Which level uses this skill

This skill is for a loop supervisor. A loop supervisor is the orchestrator of one loop, acting with this skill. It does the loop steps below: blocks, nudges, and the hourly pass.

A session above the loop supervisors (the supervisor of supervisors) does not do the loop steps. It does only this:

- Check that each loop supervisor runs and records its passes in the ledger.
- Pass owner updates to the loop supervisors unchanged.
- Decide which questions need the owner. Record them and report them.

It does not review, approve, comment on, merge, or edit a pull request. It does not change code, issues, or loop settings. It does not do a loop's work.

### Quota

Owner rule: keep quota burn at a sustainable pace, and leave headroom for the owner.

On each check:

1. Run `lupin quota --json`. Read each window's `used_pct`, `resets_at`, and `fetched_by`. A row fetched by another machine is still valid. Check its `generated_at` time.
2. Compute the elapsed share of each window: `1 - (resets_at - now) / window length`. A window is on pace when `used_pct` is at or below its elapsed share.
3. A window at or above 75% used is at the owner reserve line. Do not start new work that uses it.
4. A window above 90% for Claude is a hard stop. Defer all work routed to Claude (see "Hourly pass").
5. A window that is over pace or at the reserve line: relay the owner rule to the loop supervisors, with the window name and its numbers. Do not change routing or quota settings yourself.
6. If a window cannot be read, treat it as blocked for new starts on that provider. Record the block.
7. Append each check to a sample log (UTC time, window, `used_pct`, `resets_at`). Compare with the last sample to get the burn rate. One sample alone does not show a rate.
8. Record each over-pace, reserve-line, or blocked state in the ledger with status `deferred`.

Terms:

- Loop: one Lupin run on one repo. It runs in one Herdr workspace.
- Orchestrator: the main agent of a delegation loop. It dispatches work. The owner merges every pull request.
- Research worker: an agent that answers one question. It reads and runs checks. It does not change code, issues, pull requests, or loops.
- Ledger: the shared Lupin event record. It is stored in Redis.
- Monitor log: the file that the hourly monitor writes. The owner gives its path.
- Owner: the person who owns the fleet.

## What the `lupin supervise` command does

For each loop on this machine, `lupin supervise` gives one simplified verdict. The rules
are `verdict` in `src/lupin/supervise.py`. It sends `NUDGE_TEXT` only when the
Herdr state is `idle` or `done`. It sends at most one nudge per repo each hour.
It writes one `supervise` ledger event per repo. A `stopped` verdict does not
block a nudge. An idle loop is `stopped` if its unit is inactive or if it asks
to continue. A `blocked` or `needs_attention` pane gets no text. Its verdict is
`stopped` with the reason `100% blocked` ONLY if there is no possible work of value that remains,
when considering the backlog, QA, experimental, and other tasks of value, as appropriate (translations, blog posts).

The Debriefs page must include the daily owner-blocked report as well as an up-to-date executive summary of developments related to the project.

## Decide: owner decision or not

Ask: can only a person supply or decide this?

| Owner decision: wait for the owner                 | Not an owner decision: research, then act                      |
| -------------------------------------------------- | -------------------------------------------------------------- |
| Credentials: a token, password, login, or key      | A rate limit, timeout, or other transient error                |
| Money: a paid plan, purchase, or budget            | A failed test, gate, push, or merge conflict                   |
| Hardware: a machine or device                      | A missing label or tool that the repo docs give a fallback for |
| A critical product or policy choice                | A technical choice between options                             |
| An action that the repo rules reserve for a person | A model or quota limit (see "Hourly pass")                     |

If you are not sure, dispatch researchers first. A research worker answers one question.
It gathers information and writes an informed report. You are strongly encouraged to have an additional agent review a researcher's work for anything important.
If an agent has the ability to access to everything it needs to complete a task, it should not be marked as blocked.

The Superloop leader does not change code, issues, or pull requests. It oversees Lupin loop orchestrators in order to maximize their productivity and impact.

## Blocked loop

1. Read the pane:

   ```sh
   herdr --session SESSION agent read PANE_ID --source recent-unwrapped --lines 120
   ```

2. If the pane shows `Awaiting owner input`, read the question. Apply "Decide".
3. Owner decision: write the question in an issue comment that starts with
   `Loop:` and names its options. Record `owner-decision` and put the question
   in the handoff.
4. For any other block, do not prompt the pane. The supervisor reports
   `blocked, research needed`. Record the question, pane ID, and time. Dispatch a
   research worker on the next pass and act on its result.

## Sending the rule by hand

Use this only when `lupin supervise` did not nudge the loop.

1. Run `lupin loops --json`. Take `pane_id` (PANE_ID) and `session` (SESSION)
   from the object whose `repo` is the loop's repo. If the loop is not listed,
   do not send. Report it in the handoff.
2. Send the text of `NUDGE_TEXT` as one prompt:

   ```sh
   herdr --session SESSION agent prompt PANE_ID "TEXT" --wait --timeout 120000
   ```

3. Check the reply with the read command above. The reply must name the block,
   the research result, and the next action.
4. If the reply is missing, send the rule once more. If the second reply also
   fails, record `blocked` and report it in the handoff.
5. If the send fails, times out, or returns `agent_prompt_stalled`, do not send
   again at once. Read the pane first.

## Hourly pass

Each hourly block in the monitor log starts with `=== hourly check`.
Check the burn rate.

1. Read the last 40 lines:

   ```sh
   tail -n 40 <monitor-log>
   ```

2. For each loop, the block must show `running`.
   Each `ALERT` line is a block. Apply "Decide".
3. For an `ALERT`, read the pane with `lupin peek REPO 25`.
4. Check the burn rate with `lupin quota`; if a loop is burning through usage faster than the daily budget, adjust accordingly. In general, evenly spread out usage across days of the quota, and budget 10% of weekly headroom for the owner to use at their discretion, _unless_ the weekly limit is just hours from resetting. Always try to use free models as needed to triage, etc.
5. Inspect the transcripts to identify areas where ANY process could be streamlined or improved; proactively ticket anything you see that seems less than optimal. E.g. unpushed local changes, confusing or brittle instructions, wasteful spend, failure to trigger ui-pressure-tests, doc bloat, etc. Message them with corrections as needed.
6. Ensure that all open PRs are production ready (with as many code-review and fix rounds as needed); for approved PRs, make sure they do not have merge conflicts with master and ensure they pulled in all relevant changes based on the changing state of the repo.
   Loop orchestrators occasionally need to be reminded to pull the latest from origin and to do another pass of triage or PR management. Use your discretion.
7. Ensure the spend is

Do not start a model run unless you can read
`~/.local/state/lupin/model-snapshot.json`. If you cannot read it, record the
block.

## Record

Record each block, research result, deferred item, major status update, and missed pass briefly:

```sh
lupin ledger append OWNER/REPO --event supervisor --status STATUS \
  --summary "TEXT" --evidence "TEXT" --decisions "TEXT" --next "TEXT"
```

- STATUS is one of: `blocked`, `research-done`, `owner-decision`, `deferred`,
  `missed-pass`, `stopped`, `ongoing`.
- Add `--issue N` only for an item with an issue number (positive whole number).
- If the command exits with code 3, Redis is not reachable. Put the same text in
  the handoff and report it.

Each record must make sense alone. Read the record back:

```sh
lupin ledger read OWNER/REPO --limit 20
```

## Stop

There will always be work to do. The superloop ends when the owner tells you to stop.

However, after 24 hours, if you haven't been stopped, you may pass your session's superloop torch to the next superloop.

When you are wrapping up, record the end with status `stopped`. Run the `handoff` skill.
Include the last hourly pass time, each open block, and the executive summary.
