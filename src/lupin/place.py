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
fewer free slots).

Judgment call -- quest focus: C8/#12 (quest focus/release) has not landed.
`quest_focus_for` below is the hook that issue will replace -- it always
returns `None` (no quest concept exists yet), which makes every machine
tie on that dimension, same as the issue's instructions ask for.

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
import re
import subprocess
import time

from . import classify as classify_mod
from . import machines
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


def quest_focus_for(task_label: str) -> str | None:
    """Which machine, if any, is quest-focused for this task.

    Stub for C8/#12 (quest focus hasn't landed). Always `None` today -- no
    machine ever wins the quest-focus ranking tier, consistent with the
    issue's instruction to treat quest focus as always absent for now.
    Replace this function's body once `focus:<quest>` (docs/redis-schema.md)
    has a reader; nothing else in this module needs to change.
    """
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


def _slot_totals(slots: dict) -> tuple[int, int]:
    used = sum(int(entry.get("used", 0)) for entry in (slots or {}).values())
    max_ = sum(int(entry.get("max", 0)) for entry in (slots or {}).values())
    return used, max_


def _heartbeat_age(record: dict, now: float) -> float:
    stamp = record.get("heartbeat")
    if not stamp:
        return float("inf")
    try:
        return now - machines._parse_iso(stamp)
    except ValueError:
        return float("inf")


def _rank_key(record: dict, quest_focus: str | None, now: float) -> tuple:
    state_rank = 0 if record["state"] == "online" else 1
    quest_rank = 0 if quest_focus and record["name"] == quest_focus else 1
    used, max_ = _slot_totals(record.get("slots"))
    return (state_rank, quest_rank, -(max_ - used), _heartbeat_age(record, now))


def _reason(record: dict, pick: dict | None, quest_focus: str | None) -> str:
    if pick is not None and record["name"] == pick["name"]:
        return "pick"
    if record["state"] != "online":
        return "draining"
    if pick is None:
        return "draining"
    if quest_focus and pick["name"] == quest_focus and record["name"] != quest_focus:
        return "not quest focus"
    used_r, max_r = _slot_totals(record.get("slots"))
    used_p, max_p = _slot_totals(pick.get("slots"))
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

    quest_focus = quest_focus_for(task_label)
    now = time.time()
    ranked = sorted(matched, key=lambda r: _rank_key(r, quest_focus, now))
    online = [r for r in ranked if r["state"] == "online"]
    pick = online[0] if online else None

    candidates = [
        {
            "name": record["name"],
            "state": record["state"],
            "slots_used": _slot_totals(record.get("slots"))[0],
            "slots_max": _slot_totals(record.get("slots"))[1],
            "quest_focus": record["name"] if record["name"] == quest_focus else None,
            "version": record.get("version"),
            "result": _reason(record, pick, quest_focus),
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
