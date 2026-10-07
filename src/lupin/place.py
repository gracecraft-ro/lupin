"""`lupin place <task>` (issue #9, part of #2's fleet-CLI split): which
machine should run a task already routed to a model by `route`/`classify`.

Three steps, each reusing an already-landed module rather than
reimplementing it:

1. Classify + route (`classify.classify`, `route.route`) -- unchanged logic,
   just called from here with a synthetic issue dict when `<task>` is free
   text instead of a real GitHub issue.
2. Map the routed model to a provider (`_PROVIDER_BY_MODEL` below -- a small
   lookup table kept in this module, not model-tiers.json, since it is a
   `place`-only concern and model-tiers.json's schema is about tiers, not
   accounts).
3. Score every machine the registry (`machines.py`, issue #7) knows about
   that reports quota (`quota.py`, issue #8) for that provider, and rank the
   candidates.

Judgment call -- "runs the provider": `machines.py`'s `providers` field is
still an unpopulated stub (see its own docstring -- nothing writes it yet).
The thing that *is* populated, per machine, every heartbeat, is `quota`
(`quota.snapshot()`, keyed by provider). A machine reporting a quota entry
for a provider is the only real signal this repo has today that the machine
can serve it, so that is what `_runs_provider` below checks. When something
starts writing `providers` for real, switch to that instead -- it is the
more direct signal, this is a stand-in.

Judgment call -- ranking and the `result` column: the issue's `--explain`
example ranks by (state, quest focus, free slots, heartbeat age) and labels
every non-picked machine with *why* it lost. This module sorts candidates
by that same tuple, then reports the first dimension where a machine's
tuple differs from the picked machine's -- that reproduces the example
exactly (a draining machine always reads "draining"; an online machine
beaten only on quest focus reads "not quest focus", even if it also has
fewer free slots). Issue #30 adds quota burn margin (`pct_left / hours to
reset`, see `_burn_margin_rank`) as a new dimension between quest focus and
free slots, with a matching "worse burn margin" reason.

Judgment call -- quest focus: `quest_focus_for` below maps a task's issue
number to its quest (if `quest.load_quests` -- issue #11 -- finds one in
the current repo whose sub-issues include it) and reads that quest's
`focus:<quest>` machine (docs/redis-schema.md, written by `lupin quest
focus`/`release`, issue #12). Free text has no issue number, an unresolved
issue number has no quest membership to check, and an issue outside every
quest's sub-issues matches nothing -- all three return `None`, same as an
unfocused quest, so every machine ties on that dimension exactly like an
ordinary (non-quest) task.

Judgment call -- no `--repo` flag on `place`: `<task>` is just an issue
number or free text, nothing else. A numeric task is looked up with
`gh issue view <N> --json ...`, which resolves against the *current
directory's* repo the same way every other `gh` call in this codebase does
(see `roadmap.py`) -- there is nowhere else to get a repo from. If `gh`
fails (no repo, not authed, issue missing) this falls back to classifying
on the bare number alone rather than erroring out -- a placement decision
is still useful even with a generic category/size.

Judgment call -- the printed "run" command: #2's own design notes say
loop control (`run`/`once`/...) stays in `loopctl`, not `lupin` -- but this
issue still asks `place` to print a `lupin run ...` line. There is no repo
to put in it (see above), so the line names the picked machine and the
task, not a repo: `lupin run --machine <name> <task>`. Whatever actually
consumes this line decides how to turn "run this task on this machine"
into its own invocation; this module does not execute anything.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import time

from . import classify as classify_mod
from . import machines
from . import quest as quest_mod
from . import route as route_mod

CoordinatorUnreachable = machines.CoordinatorUnreachable

# route()'s models, mapped to the quota-tracked provider that serves them.
# "bmo:"/"local:" models run on local/shared-GPU inference, not a cloud
# account with a quota reading -- see `_provider_for_model`.
_PROVIDER_BY_MODEL = {
    "sonnet": "claude",
    "opus": "claude",
    "fable": "opencode-go",
}

_CATEGORY_LABELS = {
    "coding": "code edit",
    "cad-spatial": "CAD/spatial",
    "frontend-ui": "frontend/UI",
    "translation": "translation",
    "prose": "prose",
    "general": "general",
}

_SIZE_LABELS = {
    "size-xs": "tiny",
    "size-s": "small",
    "size-m": "medium",
    "size-l": "large",
    "size-xl": "extra large",
    "size-?": "unknown size",
}

_ISSUE_NUMBER_RE = re.compile(r"^#?(\d+)$")


def provider_for_model(model: str) -> str:
    """Map a routed model to the provider `quota.snapshot()` tracks it
    under. Falls back to the model name itself for anything this table
    doesn't know yet, rather than raising -- an unrecognized provider
    simply matches no machine, which `place()` reports honestly via its
    skip count.
    """
    if model.startswith("bmo:"):
        return "bmo"
    if model.startswith("local:"):
        return "local"
    return _PROVIDER_BY_MODEL.get(model, model)


def quest_focus_for(issue_number: str | None, connection: dict) -> str | None:
    """Which machine, if any, is quest-focused for this task.

    `issue_number` is the task's bare issue number (e.g. `"418"`), or
    `None` for a free-text task -- free text has nothing to match a quest's
    sub-issues against, so it always returns `None`. The quest search is
    over the current repo only (this module's docstring already resolves
    `gh issue view` the same ambient way -- `place` takes no `--repo` flag).
    """
    if not issue_number:
        return None
    try:
        number = int(issue_number)
    except ValueError:
        return None
    repo = os.path.basename(os.getcwd())
    quests, _warnings = quest_mod.load_quests([repo])
    for quest in quests:
        if any(task["number"] == number for task in quest["tasks"]):
            focus = quest_mod.read_focus(quest["name"], **connection)
            return focus.get("machine") if focus else None
    return None


def _fetch_issue(number: str) -> tuple[dict, str | None]:
    """`gh issue view <number> --json title,body,labels` in the current
    repo (same ambient-repo convention `roadmap.py`'s `gh` calls use).
    Returns `({}, error)` on any failure -- `classify()` still produces a
    (generic) category/size from an empty issue, so a placement decision
    is still possible without a working `gh`.
    """
    argv = ["gh", "issue", "view", number, "--json", "title,body,labels"]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=15)
    except (FileNotFoundError, OSError) as exc:
        return {}, str(exc)
    except subprocess.TimeoutExpired:
        return {}, "gh issue view timed out"
    if proc.returncode != 0:
        return {}, (proc.stderr or proc.stdout or "gh issue view failed").strip()
    try:
        return json.loads(proc.stdout), None
    except json.JSONDecodeError as exc:
        return {}, f"invalid JSON from gh: {exc}"


def _resolve_task(task: str) -> tuple[dict, str]:
    """Return (issue dict for classify(), display label)."""
    match = _ISSUE_NUMBER_RE.match(task.strip())
    if not match:
        return {"title": task}, task
    number = match.group(1)
    issue, _error = _fetch_issue(number)
    title = issue.get("title") if issue else None
    label = f"#{number} {title}" if title else f"#{number}"
    return issue or {}, label


def _burn_margin_rank(record: dict, provider: str, now: float) -> tuple[int, float]:
    """How safe this machine's quota is: `pct_left / hours_until_reset`, as a
    sort key where a *smaller* tuple is better (matches every other
    dimension in `_rank_key`, which this feeds into).

    Three tiers, since not every machine has enough data for a real margin:

    0. A real reading -- `pct_left` and a `resets_at` are both present.
       `resets_at` is epoch milliseconds (`quota.quota_reset_timestamp`);
       `now` (from `time.time()`) is epoch seconds, so this converts before
       subtracting. If `resets_at` has already passed (a stale heartbeat --
       the window likely reset for real since the last report), treat the
       machine as safe rather than dividing by a near-zero or negative
       `hours_left`, which would otherwise blow the ranking up or flip its
       sign: `-math.inf` sorts ahead of every real (finite) margin.
    1. `pct_left` alone -- no `resets_at` to compute a rate from. Ranks
       after every tier-0 machine regardless of how high `pct_left` is;
       among themselves, more `pct_left` is still better.
    2. Nothing usable (no quota entry for this provider, or `pct_left` is
       `None`) -- ranks last, with no further ordering to apply.
    """
    entry = (record.get("quota") or {}).get(provider)
    pct_left = entry.get("pct_left") if entry else None
    if pct_left is None:
        return (2, 0.0)
    resets_at = entry.get("resets_at")
    if not isinstance(resets_at, (int, float)):
        return (1, -pct_left)
    hours_left = (resets_at / 1000 - now) / 3600
    if hours_left <= 0:
        return (0, -math.inf)
    return (0, -(pct_left / hours_left))


def _rank_key(
    record: dict, quest_focus: str | None, now: float, provider: str
) -> tuple:
    state_rank = 0 if record["state"] == "online" else 1
    quest_rank = 0 if quest_focus and record["name"] == quest_focus else 1
    margin_rank = _burn_margin_rank(record, provider, now)
    used, max_ = machines._slot_totals(record.get("slots"))
    return (
        state_rank,
        quest_rank,
        margin_rank,
        -(max_ - used),
        machines._heartbeat_age(record, now),
    )


def _reason(
    record: dict, pick: dict | None, quest_focus: str | None, provider: str, now: float
) -> str:
    if pick is not None and record["name"] == pick["name"]:
        return "pick"
    if record["state"] != "online":
        return "draining"
    if pick is None:
        return "draining"
    if quest_focus and pick["name"] == quest_focus and record["name"] != quest_focus:
        return "not quest focus"
    if _burn_margin_rank(record, provider, now) != _burn_margin_rank(pick, provider, now):
        return "worse burn margin"
    used_r, max_r = machines._slot_totals(record.get("slots"))
    used_p, max_p = machines._slot_totals(pick.get("slots"))
    if (max_r - used_r) < (max_p - used_p):
        return "fewer slots free"
    return "staler heartbeat"


def format_duration(seconds: float) -> str:
    """`3h 10m`-style duration, for the `--explain` quota line. Pure
    presentation helper -- kept here (not cli.py) so it's covered the same
    way as the rest of this module's logic.
    """
    if seconds <= 0:
        return "now"
    total_minutes = int(seconds // 60)
    hours, minutes = divmod(total_minutes, 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def _quota_for_provider(records: list[dict], provider: str) -> dict | None:
    """One quota reading to show in the header -- #2's design note says
    quota is "usually per-account, shared by every machine on that
    account", so one line is enough unless machines disagree (not handled
    here; see this module's docstring). Prefers a record with a real
    percentage over one with `pct_left: None` (unavailable), but returns
    whatever is there rather than nothing.
    """
    fallback = None
    for record in records:
        entry = (record.get("quota") or {}).get(provider)
        if entry is None:
            continue
        if entry.get("pct_left") is not None:
            return entry
        fallback = fallback or entry
    return fallback


def place(task: str, connection: dict, *, tiers: dict | None = None) -> dict:
    """Classify, route, and pick a machine for `task`. Raises
    `CoordinatorUnreachable` if the fleet registry can't be reached (same
    exception `machines.machines()` raises).
    """
    issue, task_label = _resolve_task(task)
    category, size = classify_mod.classify(issue)
    choice = route_mod.route(category, size, tiers=tiers)
    model, effort = choice["model"], choice["effort"]
    provider = provider_for_model(model)

    records = machines.machines(connection)
    quota = _quota_for_provider(records, provider)

    skipped = {"offline": 0, "other_provider": 0}
    matched = []
    for record in records:
        if provider not in (record.get("quota") or {}):
            skipped["other_provider"] += 1
            continue
        if record["state"] == "offline":
            skipped["offline"] += 1
            continue
        matched.append(record)

    issue_number_match = _ISSUE_NUMBER_RE.match(task.strip())
    quest_focus = quest_focus_for(
        issue_number_match.group(1) if issue_number_match else None, connection
    )
    now = time.time()
    ranked = sorted(matched, key=lambda r: _rank_key(r, quest_focus, now, provider))
    online = [r for r in ranked if r["state"] == "online"]
    pick = online[0] if online else None

    candidates = [
        {
            "name": record["name"],
            "state": record["state"],
            "slots_used": machines._slot_totals(record.get("slots"))[0],
            "slots_max": machines._slot_totals(record.get("slots"))[1],
            "quest_focus": record["name"] if record["name"] == quest_focus else None,
            "version": record.get("version"),
            "result": _reason(record, pick, quest_focus, provider, now),
        }
        for record in ranked
    ]

    run_command = f"lupin run --machine {pick['name']} {task}" if pick else None

    return {
        "task": task,
        "task_label": task_label,
        "category": category,
        "size": size,
        "category_label": _CATEGORY_LABELS.get(category, category),
        "size_label": _SIZE_LABELS.get(size, size),
        "model": model,
        "effort": effort,
        "provider": provider,
        "quota": quota,
        "candidates": candidates,
        "pick": pick["name"] if pick else None,
        "run_command": run_command,
        "skipped": skipped,
    }
