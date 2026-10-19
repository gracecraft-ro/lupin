"""`lupin supervise`: check the loops on this machine with fixed rules.

No model runs. For each Lupin loop, it reads the Herdr state, the systemd
unit, the last pane lines and the latest handoff. It gives each loop one
verdict (see `verdict`). It sends a fixed nudge to an idle loop with
`herdr agent prompt`. It writes one `supervise` ledger event per repo.
It only reads, except for the nudge, the ledger event and its own hash file.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

import redis

from . import claims, ledger, loop_runtime, roadmap, roadmap_cli
from .slots import CoordinatorUnreachable

NUDGE_TEXT = (
    "Supervisor: Work the queue until nothing is left. Nothing is left when no "
    "open issue that you may take is unclaimed, every pull request that you own "
    "is ready for production, and no pull request waits for a review. Write each "
    "question for the owner as an issue comment that starts with 'Loop:'. Then "
    "take the next item. Do not ask if you must continue."
)
# Text that shows a loop asks a person before it continues (AGENTS.md rule 8).
ASK_PHRASES = ("tell me if you want", "if you want me to")
PANE_LINES = 20
LEDGER_LINES = 20
REPEAT_COUNT = 3
_LIVE = {"running", "starting", "needs_attention"}
STATE_FILE = Path(
    os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state"))
) / "lupin" / "supervise.json"


def verdict(
    *,
    state: str,
    unit: str,
    work_left: int | None,
    same_tail: bool = False,
    repeated_item: bool = False,
    asks_to_continue: bool = False,
) -> str:
    """The rules, in order. The first rule that matches gives the verdict.

    `work_left` is None when it is not known. Then no stop or idle verdict is given.
    """
    work = bool(work_left)
    if repeated_item:
        return "repeated"
    if state == "working" and same_tail:
        return "busywork"
    if work and (unit in {"inactive", "failed"} or state in {"blocked", "needs_attention"}):
        return "stopped"
    if work and asks_to_continue:
        return "stopped"
    if work and state in {"idle", "done"}:
        return "idle"
    return "ok"


NUDGE_VERDICTS = {"idle", "stopped"}


def _tail_hash(text: str | None) -> str | None:
    if text is None:
        return None
    return hashlib.sha256(" ".join(text.split()).encode()).hexdigest()


def _repeated(events: list[dict] | None) -> bool:
    """True when the last 3 loop events are for one issue with one status."""
    if not events:
        return False
    rows = [e for e in events if e.get("event") != "supervise"][-REPEAT_COUNT:]
    if len(rows) < REPEAT_COUNT:
        return False
    issues = {e.get("issue") for e in rows}
    statuses = {e.get("status") for e in rows}
    return len(issues) == 1 and None not in issues and len(statuses) == 1


def _load_hashes(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    panes = value.get("panes") if isinstance(value, dict) else None
    return panes if isinstance(panes, dict) else {}


def _save_hashes(path: Path, panes: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".part")
    temporary.write_text(json.dumps({"panes": panes}), encoding="utf-8")
    os.replace(temporary, path)


def run_pass(
    *,
    list_loops: Callable[[], list[dict]],
    read_pane: Callable[[dict], str | None],
    read_handoff: Callable[[dict], str],
    repo_name: Callable[[str], str | None],
    work_left: Callable[[str, str], int | None],
    recent_events: Callable[[str], list[dict] | None],
    send_nudge: Callable[[dict, str], None],
    append_event: Callable[[str, dict], None],
    state_path: Path = STATE_FILE,
    dry_run: bool = False,
) -> dict:
    """Check each loop once. Return a report. Never raises for a failed nudge."""
    old_hashes = _load_hashes(state_path)
    new_hashes: dict[str, str] = {}
    names: dict[str, str | None] = {}
    work: dict[str, int | None] = {}
    events: dict[str, list[dict] | None] = {}
    rows = []
    for loop in list_loops():
        repo = loop["repo"]
        base = loop_runtime.base_repo(repo)
        if base not in names:
            names[base] = repo_name(base)
            full_name = names[base]
            work[base] = work_left(full_name, base) if full_name else None
            events[base] = recent_events(full_name) if full_name else None
        pane = read_pane(loop)
        tail = _tail_hash(pane)
        if tail:
            new_hashes[repo] = tail
        text = f"{pane or ''}\n{read_handoff(loop)}".lower()
        result = verdict(
            state=loop.get("state", "unknown"),
            unit=loop.get("unit", "unknown"),
            work_left=work[base],
            same_tail=tail is not None and old_hashes.get(repo) == tail,
            repeated_item=_repeated(events[base]),
            asks_to_continue=any(phrase in text for phrase in ASK_PHRASES),
        )
        nudge = "none"
        if result in NUDGE_VERDICTS:
            if not (loop.get("agent") and loop.get("pane_id")):
                nudge = "no-pane"
            elif dry_run:
                nudge = "dry-run"
            else:
                try:
                    send_nudge(loop, NUDGE_TEXT)
                    nudge = "sent"
                except loop_runtime.LoopError:
                    nudge = "failed"
        rows.append({
            "repo": repo, "base": base, "state": loop.get("state", "unknown"),
            "unit": loop.get("unit", "unknown"), "verdict": result, "nudge": nudge,
        })

    repos = {}
    ledger_unreachable = False
    for base, full_name in names.items():
        mine = [row for row in rows if row["base"] == base]
        if any(row["nudge"] == "sent" for row in mine):
            status = "nudged"
        elif any(row["verdict"] != "ok" for row in mine):
            status = "flagged"
        elif work[base] == 0:
            status = "idle"
        else:
            status = "ok"
        left = "unknown" if work[base] is None else str(work[base])
        event = {
            "event": "supervise",
            "status": status,
            "summary": f"{len(mine)} loop(s) checked; ready issues left: {left}",
            "highlights": [
                f"{row['repo']}: state={row['state']}, unit={row['unit']}, "
                f"verdict={row['verdict']}, nudge={row['nudge']}"
                for row in mine
            ],
        }
        written = "dry-run" if dry_run else "no-repo-name"
        if full_name and not dry_run:
            try:
                append_event(full_name, event)
                written = "written"
            except CoordinatorUnreachable:
                ledger_unreachable = True
                written = "redis-unavailable"
        repos[base] = {"name": full_name, "work_left": work[base], "event": event, "ledger": written}

    if not dry_run:
        _save_hashes(state_path, new_hashes)
    report = {
        "loops": [{k: v for k, v in row.items() if k != "base"} for row in rows],
        "repos": repos,
        # Unknown work counts as work, so the supervisor does not stop too early.
        "work_left": any(value != 0 for value in work.values()),
        "ledger_unreachable": ledger_unreachable,
    }
    if not rows:
        report["note"] = "no loops on this machine"
    return report


def _print_text(report: dict) -> None:
    if not report["loops"]:
        print(report["note"])
        return
    for row in report["loops"]:
        print(
            f"{row['repo']}: state={row['state']} unit={row['unit']} "
            f"verdict={row['verdict']} nudge={row['nudge']}"
        )
    for base, value in report["repos"].items():
        print(f"{base}: {value['event']['status']}, ledger {value['ledger']}")


def supervise(
    *,
    every: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
    emit: Callable[[dict], None] = _print_text,
    **sources,
) -> int:
    """Run one pass, or one pass every `every` seconds until no work is left.

    Returns 0, or 3 when Redis is unavailable for a ledger write.
    """
    while True:
        report = run_pass(**sources)
        emit(report)
        if report["ledger_unreachable"]:
            return 3
        if every is None or not report["loops"] or not report["work_left"]:
            return 0
        sleep(every)


# Real sources. Each one only reads, except `_send_nudge` and `_append_event`.

def _unit_state(repo: str) -> str:
    try:
        _, output = loop_runtime._run(
            ["systemctl", "is-active", f"{loop_runtime.unit_name(repo)}.service"],
            timeout=loop_runtime.SYSTEMCTL_CHECK_TIMEOUT,
        )
    except loop_runtime.LoopError:
        return "unknown"
    return output.strip() or "unknown"


def _list_loops() -> list[dict]:
    """The Lupin loops that `local_loops` finds, with their own Herdr state.

    `local_loops` also lists other Herdr agents in a repo. Those are skipped.
    """
    try:
        rows = loop_runtime.local_loops()
    except loop_runtime.LoopError:
        return []
    loops = []
    for row in rows:
        repo = row.get("repo")
        try:
            metadata = loop_runtime._read_metadata(repo)
            if not metadata or metadata.get("state") not in _LIVE:
                continue
            state = loop_runtime.loop_state(repo)
        except loop_runtime.LoopError:
            continue
        loops.append({
            "repo": repo,
            "session": state.get("session"),
            "state": state.get("state", "unknown"),
            "unit": _unit_state(repo),
            "agent": state.get("agent"),
            "pane_id": state.get("pane_id"),
            "started_at": metadata.get("started_at"),
            "worktree": metadata.get("worktree"),
        })
    return loops


def _read_pane(loop: dict) -> str | None:
    try:
        return loop_runtime.peek_loop(loop["repo"], PANE_LINES)
    except loop_runtime.LoopError:
        return None


def _read_handoff(loop: dict) -> str:
    """The newest handoff file written after the loop started, or ""."""
    paths = [loop_runtime.STATE_DIR / "handoffs" / f"{loop['repo']}.HANDOFF.md"]
    if loop.get("worktree"):
        paths.append(Path(loop["worktree"]) / "HANDOFF.md")
    try:
        started = datetime.fromisoformat(str(loop.get("started_at")).replace("Z", "+00:00")).timestamp()
    except ValueError:
        started = 0.0
    found = []
    for path in paths:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime >= started:
            found.append((mtime, path))
    if not found:
        return ""
    try:
        return max(found)[1].read_text(encoding="utf-8", errors="replace")[-65536:]
    except OSError:
        return ""


def _repo_name(base: str) -> str | None:
    owner, name, _error = roadmap._repo_identity(str(loop_runtime.CODE_DIR / base))
    return f"{owner}/{name}" if owner and name else None


def _claims_or_unreachable(connection: dict, targets: list[str]) -> dict:
    """Read claims. A Redis refusal (for example, no MULTI permission) counts as unreachable.

    `build_roadmap` then shows each issue as not claimed, so the count can be too high, not too low.
    """
    try:
        return claims.claims_for(targets, strict=True, **connection)
    except redis.exceptions.RedisError as exc:
        raise CoordinatorUnreachable(str(exc)) from exc


def _work_left(connection: dict, full_name: str, base: str) -> int | None:
    """Count the open issues that are not claimed and not blocked (`lupin roadmap`'s "ready").

    Returns None when the issue data cannot be read.
    """
    try:
        model = roadmap_cli.build_roadmap(
            [base],
            code_dir=str(loop_runtime.CODE_DIR),
            claims_lookup=functools.partial(_claims_or_unreachable, connection),
            connection=connection,
        )
    except Exception:  # noqa: BLE001 - a failed read means "not known", not a crash
        return None
    nodes = model["repos"].get(base, [])
    if not nodes and model["warnings"]:
        return None
    return sum(1 for node in nodes if node["status"] == "ready")


def _recent_events(connection: dict, full_name: str) -> list[dict] | None:
    try:
        return ledger.read_events(full_name, limit=LEDGER_LINES, **connection)
    except Exception:  # noqa: BLE001 - without the ledger, the repeat rule does not apply
        return None


def _send_nudge(loop: dict, text: str) -> None:
    loop_runtime._herdr(
        loop["session"], "agent", "prompt", loop["agent"], text,
        timeout=loop_runtime.HERDR_TIMEOUT,
    )


def _append_event(connection: dict, full_name: str, event: dict) -> None:
    ledger.append_event(full_name, event, **connection)


def real_sources(connection: dict) -> dict:
    """The sources that `run_pass` uses on a real machine."""
    return dict(
        list_loops=_list_loops,
        read_pane=_read_pane,
        read_handoff=_read_handoff,
        repo_name=_repo_name,
        work_left=functools.partial(_work_left, connection),
        recent_events=functools.partial(_recent_events, connection),
        send_nudge=_send_nudge,
        append_event=functools.partial(_append_event, connection),
    )
