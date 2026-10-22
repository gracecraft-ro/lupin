---
name: superloop
description: >-
  Supervise Lupin loops on one machine, by hand or with lupin supervise. Use it when a loop is blocked, when the monitor shows an alert, or when you must decide if the owner is needed.
compatibility: >-
  Requires Lupin, Herdr, and the delegation-loop skill on the supervisor host. Redis is required for ledger records.
---

# Superloop

`lupin supervise` runs fixed rules and uses no model. An agent with this skill
handles steps that need judgment: a blocked loop, an alert, or an unclear reply.
Do not copy the rule text into other files. Copy the text inside the quotes of
`NUDGE_TEXT` in `<checkout>/src/lupin/supervise.py`. Do not copy the Python.

## What lupin supervise does

For each loop on this machine, `lupin supervise` gives one verdict. The rules
are `verdict` in `src/lupin/supervise.py`. It sends `NUDGE_TEXT` only when the
Herdr state is `idle` or `done`. It sends at most one nudge per repo each hour.
It writes one `supervise` ledger event per repo. A `stopped` verdict does not
block a nudge. An idle loop is `stopped` if its unit is inactive or if it asks
to continue. It still gets the nudge. A `blocked` or `needs_attention` pane gets
no text. Its verdict is `stopped` with the reason "waiting for the owner".

## Decide: owner decision or not

Ask: can only a person supply or decide this?

| Owner decision: wait for the owner | Not an owner decision: research, then act |
|---|---|
| Credentials: a token, password, login, or key | A rate limit, timeout, or other transient error |
| Money: a paid plan, purchase, or budget | A failed test, gate, push, or merge conflict |
| Hardware: a machine or device | A missing label or tool that the repo docs give a fallback for |
| A product or policy choice | A technical choice between options |
| An action that the repo rules reserve for a person | A model or quota limit (see "Hourly pass") |

If you are not sure, the orchestrator (the main agent of a loop) researches
first. A research worker answers one question. It reads and runs checks. It does
not change code, issues, pull requests, or loops.

## Blocked loop

1. Read the pane:

   ```sh
   herdr --session SESSION agent read PANE_ID --source recent-unwrapped --lines 120
   ```

2. If the pane shows `Awaiting owner input`, read the question. Apply "Decide".
3. Owner decision: record `owner-decision`. Put one short question, with its
   options, in the handoff.
4. Not an owner decision: Herdr rejects a prompt to a blocked pane with
   `agent_blocked`. Record the question, PANE_ID, and time. Retry on the next
   pass. Issue #271 covers this gap.

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

Each hourly block in the monitor log starts with `=== hourly check`. The owner
gives the path of the monitor log.

1. Read the last 40 lines:

   ```sh
   tail -n 40 <monitor-log>
   ```

2. Check the time of the newest header, in Los Angeles time. If it is more than
   70 minutes old, record `missed-pass`.
3. For `roundsmith`, `lupin`, and `bodysmith`, the block must show `running`.
   Each `ALERT` line is a block. Apply "Decide".
4. Each loop must show the GPT-6-Luna header. For an `ALERT`, read the pane with
   `lupin peek REPO 25`.
5. Check Claude's 7-day quota:

   ```sh
   lupin quota --json | jq '.claude.rows[] | select(.duration == "P7D") | .used_pct'
   ```

   If `used_pct` is more than 90, defer all work routed to Claude. Keep the
   GPT-6-Luna work running. If you cannot read `used_pct`, record the block and
   treat the quota as blocked.

6. If you cannot finish a pass, record `missed-pass` with the reason.

Do not start a model run unless you can read
`~/.local/state/lupin/model-snapshot.json`. If you cannot read it, record the
block. Do not start a loop for the repos `field-trip` or `ghostbook.nix`.

## Record

Record each block, research result, deferred item, and missed pass:

```sh
lupin ledger append OWNER/REPO --event supervisor --status STATUS \
  --summary "TEXT" --evidence "TEXT" --decisions "TEXT" --next "TEXT"
```

- STATUS is one of: `blocked`, `research-done`, `owner-decision`, `deferred`,
  `missed-pass`, `stopped`.
- Add `--issue N` only for an item with an issue number (positive whole number).
- If the command exits with code 3, Redis is not reachable. Put the same text in
  the handoff and report it.

Each record must make sense alone. Read the record back:

```sh
lupin ledger read OWNER/REPO --limit 20
```

## Stop

The watch ends when the owner tells you to stop, or when `lupin loops --json`
shows no loop with `"state": "running"` for `roundsmith`, `lupin`, or
`bodysmith`. Record the end with status `stopped`. Run the `handoff` skill.
Include the last hourly pass time, each open block, and each waiting owner
decision.
