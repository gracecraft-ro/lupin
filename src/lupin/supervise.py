"""`lupin supervise`: check the loops on this machine with fixed rules.

No model runs. For each Lupin loop, it reads the Herdr state, the systemd
unit, the last pane lines and the newest handoff. It gives each loop one
verdict (see `verdict`). It sends a fixed nudge to an idle loop with
`herdr agent prompt`. It writes one `supervise` ledger event per repo.
It writes only the nudge, the ledger event and its own state file.
`loop_runtime.local_loops` can also update a loop's metadata file
(`needs_attention` when its Herdr workspace is gone).
"""

from __future__ import annotations

import fcntl
import functools
import hashlib
import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable

import redis

from . import claims, gh_cache, ledger, loop_runtime, roadmap, roadmap_cli
from .slots import CoordinatorUnreachable
from .slots_redis import CoordinatorAuthFailed

NUDGE_TEXT = (
    "Supervisor: Work the queue until nothing is left. Nothing is left when no "
    "open issue that you may take is unclaimed, every pull request that you own "
    "is ready for production, no pull request waits for a review, and no research "
    "or docs item is left. Write each question for the owner as an issue comment "
    "that starts with 'Loop:'. Then take the next item. Do not ask if you must "
    "continue."
)
# Text that shows a loop asks a person before it continues (AGENTS.md rule 8).
ASK_PHRASES = ("tell me if you want", "if you want me to")
PANE_LINES = 20
LEDGER_LINES = 20
REPEAT_COUNT = 3
# A loop gets at most one nudge in this time.
NUDGE_INTERVAL_S = 3600
# `--every` cannot be shorter than this.
MIN_EVERY_S = 600
# Herdr states where the agent waits for the owner. The supervisor never answers for the owner.
WAITING_STATES = {"blocked", "needs_attention"}
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

    `work_left` is None when it is not known. Then no idle verdict is given,
    and a loop with an inactive unit is not called stopped.
    """
    work = bool(work_left)
    if state in WAITING_STATES:
        return "stopped"
    if work and unit in {"inactive", "failed"}:
        return "stopped"
    if state == "working" and same_tail:
        return "busywork"
    if work and state in {"idle", "done"}:
        # `idle` wins over `repeated`: the ledger is shared, so other machines can repeat an item.
        return "stopped" if asks_to_continue else "idle"
    if repeated_item:
        return "repeated"
    return "ok"


def _may_nudge(result: str, state: str) -> bool:
    return result in {"idle", "stopped"} and state not in WAITING_STATES


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


def _load_state(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"panes": {}, "nudged": {}}
    if not isinstance(value, dict):
        value = {}
    return {
        key: value[key] if isinstance(value.get(key), dict) else {}
        for key in ("panes", "nudged")
    }


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.part")
    temporary.write_text(json.dumps(state), encoding="utf-8")
    os.replace(temporary, path)


def run_pass(
    *,
    list_loops: Callable[[], list[dict]],
    read_pane: Callable[[dict], str | None],
    read_handoff: Callable[[dict], str],
    repo_name: Callable[[str], str | None],
    work_left: Callable[[str, str], tuple[int | None, list[str]]],
    recent_events: Callable[[str], list[dict] | None],
    send_nudge: Callable[[dict, str], None],
    lock_loop: Callable[[str], Callable[[], None] | None],
    append_event: Callable[[str, dict], None],
    state_path: Path = STATE_FILE,
    now: Callable[[], float] = time.time,
    dry_run: bool = False,
) -> dict:
    """Check each loop once. Return a report. Never raises for a failed nudge.

    `work_left` returns (count or None, notes for the event).
    `lock_loop` returns a release function, or None when `lupin stop` holds the loop.
    """
    saved = _load_state(state_path)
    panes: dict[str, str] = {}
    nudged = dict(saved["nudged"])
    names: dict[str, str | None] = {}
    work: dict[str, int | None] = {}
    notes: dict[str, list[str]] = {}
    events: dict[str, list[dict] | None] = {}
    rows = []
    for loop in list_loops():
        repo = loop["repo"]
        base = loop_runtime.base_repo(repo)
        if base not in names:
            names[base] = repo_name(base)
            full_name = names[base]
            work[base], notes[base] = work_left(full_name, base) if full_name else (None, [])
            events[base] = recent_events(full_name) if full_name else None
        state = loop.get("state", "unknown")
        pane = read_pane(loop)
        tail = _tail_hash(pane)
        if tail:
            panes[repo] = tail
        text = f"{pane or ''}\n{read_handoff(loop)}".lower()
        result = verdict(
            state=state,
            unit=loop.get("unit", "unknown"),
            work_left=work[base],
            same_tail=tail is not None and saved["panes"].get(repo) == tail,
            repeated_item=_repeated(events[base]),
            asks_to_continue=any(phrase in text for phrase in ASK_PHRASES),
        )
        nudge = "none"
        if _may_nudge(result, state):
            last = nudged.get(repo)
            if not loop.get("pane_id") and not loop.get("agent"):
                nudge = "no-pane"
            elif isinstance(last, (int, float)) and now() - last < NUDGE_INTERVAL_S:
                nudge = "recent"
            elif dry_run:
                nudge = "dry-run"
            else:
                release = lock_loop(repo)
                if release is None:
                    nudge = "busy"
                else:
                    try:
                        send_nudge(loop, NUDGE_TEXT)
                        nudge = "sent"
                        nudged[repo] = now()
                    except loop_runtime.LoopError:
                        nudge = "failed"
                    finally:
                        release()
        row = {
            "repo": repo, "base": base, "state": state,
            "unit": loop.get("unit", "unknown"), "verdict": result, "nudge": nudge,
        }
        if result == "stopped" and state in WAITING_STATES:
            row["reason"] = "waiting for the owner"
        rows.append(row)

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
            "summary": f"{len(mine)} loop(s) checked; work left: {left}",
            "highlights": [
                f"{row['repo']}: state={row['state']}, unit={row['unit']}, "
                f"verdict={row['verdict']}, nudge={row['nudge']}"
                + (f", reason={row['reason']}" if "reason" in row else "")
                for row in mine
            ] + notes[base],
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
        _save_state(state_path, {"panes": panes, "nudged": nudged})
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
        reason = f" reason={row['reason']}" if "reason" in row else ""
        print(
            f"{row['repo']}: state={row['state']} unit={row['unit']} "
            f"verdict={row['verdict']} nudge={row['nudge']}{reason}"
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


# Real sources.

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
    The state is the Herdr state of the loop's own pane.
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
        except loop_runtime.LoopError:
            continue
        if not metadata or metadata.get("state") not in _LIVE:
            continue
        pane_id = row.get("pane_id")
        own = next(
            (p for p in row.get("panes", []) if pane_id and p.get("pane_id") == pane_id),
            None,
        )
        loops.append({
            "repo": repo,
            "session": row.get("session"),
            "state": own.get("agent_status", "unknown") if own else row.get("state", "unknown"),
            "unit": _unit_state(repo),
            "pane_id": pane_id,
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


class _ClaimsUnknown(Exception):
    """Claims could not be read for a reason other than a missing permission."""


def _open_prs(connection: dict, base: str) -> int | None:
    """Count open pull requests that are not drafts, through the shared `gh` cache."""
    path = str(loop_runtime.CODE_DIR / base)
    owner, name, error = roadmap._repo_identity(path)
    if error or not owner or not name:
        return None
    data, error = gh_cache.cached_gh_json(
        owner, name, "open-prs",
        lambda: roadmap._run_json(
            ["gh", "pr", "list", "--state", "open", "--limit", "200", "--json", "number,isDraft"],
            path,
        ),
        connection=connection,
    )
    if error or not isinstance(data, list):
        return None
    return sum(1 for pr in data if isinstance(pr, dict) and not pr.get("isDraft"))


def _work_left(connection: dict, full_name: str, base: str) -> tuple[int | None, list[str]]:
    """Ready issues (open, not claimed, not blocked) plus open pull requests that are not drafts.

    Returns (None, notes) when either count cannot be read.
    """
    notes: list[str] = []

    def read_claims(targets: list[str]) -> dict:
        try:
            return claims.claims_for(targets, strict=True, **connection)
        except redis.exceptions.NoPermissionError as exc:
            # On some machines the Redis user cannot run MULTI. Then each issue counts as not claimed.
            notes.append("claims: unreadable")
            raise CoordinatorUnreachable(str(exc)) from exc
        except CoordinatorUnreachable as exc:
            raise _ClaimsUnknown(str(exc)) from exc

    try:
        model = roadmap_cli.build_roadmap(
            [base],
            code_dir=str(loop_runtime.CODE_DIR),
            claims_lookup=read_claims,
            connection=connection,
        )
    except CoordinatorAuthFailed:
        raise
    except Exception:  # noqa: BLE001 - a failed read means "not known", not a crash
        return None, notes
    nodes = model["repos"].get(base, [])
    if not nodes and model["warnings"]:
        return None, notes
    prs = _open_prs(connection, base)
    if prs is None:
        return None, notes + ["pull requests: unreadable"]
    return sum(1 for node in nodes if node["status"] == "ready") + prs, notes


def _recent_events(connection: dict, full_name: str) -> list[dict] | None:
    try:
        return ledger.read_events(full_name, limit=LEDGER_LINES, **connection)
    except CoordinatorAuthFailed:
        raise
    except Exception:  # noqa: BLE001 - without the ledger, the repeat rule does not apply
        return None


def _lock_loop(repo: str) -> Callable[[], None] | None:
    """Take the loop's start/stop lock without a wait. None when `lupin stop` holds it."""
    lock = loop_runtime._lock_file(repo)
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return None
    return lock.close


def _send_nudge(loop: dict, text: str) -> None:
    """Send `text` to the loop's pane. Use the agent name only when there is no pane ID."""
    loop_runtime._herdr(
        loop["session"], "agent", "prompt", loop.get("pane_id") or loop["agent"], text,
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
        lock_loop=_lock_loop,
        append_event=functools.partial(_append_event, connection),
    )
