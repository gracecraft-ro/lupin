---
name: handoff
description: >-
  Write a handoff to the shared Lupin ledger and the GitHub issue before a
  loop session ends. Use this skill when a loop session goes idle or is
  stopped, when the owner or orchestrator asks for a handoff, or when you
  must pass work to the next agent.
compatibility: Requires Lupin and GitHub CLI. The ledger needs Redis.
---

# Hand off a loop session

Run this skill when you stop work. The next agent reads the ledger and the
GitHub issue. It does not read this conversation.

1. Decide what happened in this session: what you finished, what you
   dispatched, what is still running, and which decisions the next agent must
   not repeat.

2. Append one entry to the ledger. Give each digest option one short line
   (under 120 characters). Repeat an option for more than one item:

   ```sh
   lupin ledger append OWNER/REPO --event handoff --issue N \
     --status "in progress: watchdog script written, docs pending" \
     --branch BRANCH \
     --summary "Watchdog script written and verified. Docs still to do." \
     --highlights "Added hosts/jesus/scripts/watchdog.sh" \
     --evidence "systemd-analyze verify watchdog.service: no errors" \
     --decisions "Poll every 60s. A 30s poll doubled journalctl load." \
     --next "Owner: document the timer in docs/delegation-loops.md"
   ```

   - `--highlights`: what changed. Two to five items.
   - `--evidence`: the command you ran and its result.
   - `--decisions`: a choice the next agent must not make again, and why.
   - `--next`: what is left, and who owns it. Name the owner.
   - `--issue`: omit it if the work has no issue. Add `--child N` for each
     issue you split.

   Lupin records the time and host. Do not edit or remove old entries.

3. Run step 2 on a machine that can reach Redis. If `lupin` is not found
   (exit 127), or the command cannot connect, you are probably on the wrong
   machine. Do not use the file yet. Run the command on a fleet machine, for
   example over ssh. Set the `LUPIN_REDIS_*` variables there if its shell
   does not have them.

   Use the file only when `lupin ledger append` runs and exits 3. That exit
   code means Redis is not available. Then add the same entry to
   `.loop/loop-state.json` in the repo root:

   - If the file exists, add the entry to its `entries` list. Copy the keys of
     the entries that are already there.
   - If the file does not exist, create `{"entries": []}` and add the entry.
   - Use these keys: `ts` (UTC, ISO 8601), `event` (`"handoff"`), `issue`,
     `branch`, `status`, `summary`, `highlights`, `evidence`, `decisions`,
     `next`.

   Say in the issue comment (step 4) that the ledger was not available. Append
   the entry to the ledger when Redis is back. The next orchestrator reads both
   places.

4. Comment on the GitHub issue: what is done, what is left, and that this is
   a handoff. Use `gh issue comment N --repo OWNER/REPO --body-file -`. If the
   work has a PR, put its number in the comment.

5. If the work is not finished, say "handoff, not finished" in the ledger
   status and in the comment. The next agent believes an unqualified "done"
   and does not check it again.

6. Remove the worktrees that you made in this session. Do not leave them for
   the next agent.

7. Stop. Do not take new work. The loop can stop this session at any time.
