"""Quest reporting (issue #11) and quest focus/release (issue #12).

A quest is a GitHub issue labeled `quest`; its GitHub sub-issues are its
tasks. This module finds quests, works out each task's done/not-done state,
and reads and writes which machine is focused on each quest
(`focus:<quest>`, docs/redis-schema.md).

`quest start`/`stop` (issue #13) are not built here.
"""

from __future__ import annotations

import json
import os
import re
import time

import redis

from . import machines, roadmap, slots_redis
from .roadmap import CODE_DIR

QUEST_LABEL = "quest"
PREFIX = "lupin:v1:"

CoordinatorUnreachable = slots_redis.CoordinatorUnreachable
_REDIS_ERRORS = (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError)


class QuestNotFound(Exception):
    """No quest matches a given name or issue number."""

    def __init__(self, quest_id: str):
        self.quest_id = quest_id
        super().__init__(f"no quest matches {quest_id!r}")


class NoReadyTasks(Exception):
    """A quest has no task ready to route to a focus machine."""

    def __init__(self, quest_name: str, blocker: int | None):
        if blocker is not None:
            message = (
                f"{quest_name} has no ready tasks to focus on. "
                f"#{blocker} is blocking; see lupin roadmap --dag"
            )
        else:
            message = f"{quest_name} has no ready tasks to focus on. All tasks are done."
        super().__init__(message)


class MachineNotFound(Exception):
    """`--machine` named a machine that isn't registered."""

    def __init__(self, machine: str):
        super().__init__(f"no machine named {machine!r}")


class MachineDraining(Exception):
    """The target machine is draining and cannot take a focus."""

    def __init__(self, machine: str):
        super().__init__(f"{machine} is draining and cannot take a focus. Pick another machine.")


class NoMachineAvailable(Exception):
    """No online machine exists to auto-pick for a focus."""

    def __init__(self):
        super().__init__("no online machine can take a focus")


class NoFocus(Exception):
    """A quest has no focus to release."""

    def __init__(self, quest_name: str):
        super().__init__(f"{quest_name} has no focus to release")


# Same query style as roadmap.py's DEPENDENCY_GRAPHQL: a literal connection
# filter (labels:["quest"]) instead of a variable, same as that query's own
# hardcoded states:OPEN. closedByPullRequestsReferences carries each closing
# PR's state (OPEN/CLOSED/MERGED) -- a MERGED one here means the issue is
# done even before GitHub auto-closes it. subIssues is the field this
# module adds; roadmap.py's DEPENDENCY_GRAPHQL does not fetch it.
QUEST_GRAPHQL = (
    "query($owner:String!,$name:String!,$cursor:String){repository(owner:$owner,name:$name){"
    f'issues(first:50,labels:["{QUEST_LABEL}"],states:OPEN,after:$cursor){{'
    "nodes{number title url state "
    "closedByPullRequestsReferences(first:5){nodes{state}} "
    "subIssues(first:100){nodes{number title url state "
    "closedByPullRequestsReferences(first:5){nodes{state}}}}"
    "} pageInfo{hasNextPage endCursor}}}}"
)


def _is_done(issue: dict) -> bool:
    """An issue is done once it is closed, or a PR that closes it merged --
    checking the PR state directly means a merged-but-not-yet-closed issue
    still counts, instead of waiting on GitHub's own auto-close.
    """
    if issue.get("state") == "CLOSED":
        return True
    prs = issue.get("closedByPullRequestsReferences") or {}
    nodes = prs.get("nodes") or []
    return any(isinstance(pr, dict) and pr.get("state") == "MERGED" for pr in nodes)


def _slug(title: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", str(title or "").lower()).strip("-")
    return text or "quest"


def _read_quests(repo_path: str, owner: str, name: str):
    """Return (quest_nodes, error): every open issue labeled `quest` in this
    repo, each with its subIssues nodes attached. Same pagination shape as
    roadmap._read_dependencies.
    """
    quests = []
    cursor = None
    while True:
        args = [
            "gh", "api", "graphql", "-f", f"query={QUEST_GRAPHQL}",
            "-F", f"owner={owner}", "-F", f"name={name}",
        ]
        if cursor:
            args.extend(["-F", f"cursor={cursor}"])
        data, error = roadmap._run_json(args, repo_path)
        if error:
            return quests, error
        if not isinstance(data, dict):
            return quests, "GitHub returned invalid quest data"
        errors = data.get("errors") or []
        if errors:
            messages = [
                str(item.get("message") or "GraphQL error")
                for item in errors
                if isinstance(item, dict)
            ]
            return quests, "; ".join(messages) or "GraphQL error"
        response = data.get("data")
        repository = response.get("repository") if isinstance(response, dict) else None
        page = repository.get("issues") if isinstance(repository, dict) else None
        if not isinstance(page, dict):
            return quests, "GitHub returned no quest data"
        nodes = page.get("nodes")
        page_info = page.get("pageInfo")
        if not isinstance(nodes, list) or not isinstance(page_info, dict):
            return quests, "GitHub returned incomplete quest data"
        quests.extend(node for node in nodes if isinstance(node, dict))
        if not page_info.get("hasNextPage"):
            return quests, None
        cursor = page_info.get("endCursor")
        if not cursor:
            return quests, "GitHub returned incomplete pagination data"


def _build_quest(repo: str, node: dict) -> dict:
    tasks = []
    sub = node.get("subIssues") or {}
    for task_node in sub.get("nodes") or []:
        if not isinstance(task_node, dict) or not isinstance(task_node.get("number"), int):
            continue
        tasks.append(
            {
                "number": task_node["number"],
                "title": task_node.get("title") or "",
                "done": _is_done(task_node),
            }
        )
    done_count = sum(1 for task in tasks if task["done"])
    return {
        "repo": repo,
        "number": node["number"],
        "title": node.get("title") or "",
        "name": _slug(node.get("title") or f"quest-{node['number']}"),
        "url": node.get("url") or "",
        "done": _is_done(node),
        "tasks": tasks,
        "doneCount": done_count,
        "total": len(tasks),
    }


def load_quests(repos: list[str], code_dir: str = CODE_DIR):
    """Return (quests, warnings): every open issue labeled `quest` across
    `repos`, each with its sub-issue tasks. A repo lupin can't read from
    adds a warning, not a failure -- no quest-labeled issue anywhere is a
    valid, expected empty result, not an error.
    """
    quests = []
    warnings = []
    for repo in repos:
        repo_path = os.path.join(code_dir, repo)
        owner, name, error = roadmap._repo_identity(repo_path)
        if error:
            warnings.append(f"{repo}: {error}")
            continue
        nodes, error = _read_quests(repo_path, owner, name)
        if error:
            warnings.append(f"{repo}: {error}")
            continue
        for node in nodes:
            if isinstance(node, dict) and isinstance(node.get("number"), int):
                quests.append(_build_quest(repo, node))
    return quests, warnings


def find_quest(quests: list[dict], quest_id: str) -> dict | None:
    """Match `quest_id` against a quest's slug name or its issue number
    (with or without a leading `#`)."""
    text = str(quest_id).lstrip("#")
    for quest in quests:
        if quest["name"] == quest_id or str(quest["number"]) == text:
            return quest
    return None


def read_focus(
    quest_name: str,
    *,
    redis_host: str | None = None,
    redis_port: int | None = None,
    redis_username: str | None = None,
    redis_password: str | None = None,
) -> dict | None:
    """Read `focus:<quest_name>` and return its parsed JSON, or None if
    there is no focus -- or Redis can't be reached. A read-only report has
    nothing to retry or fall back to, so "unreachable" and "no focus" are
    reported the same way (issue #11).
    """
    client = slots_redis._client(redis_host, redis_port, redis_username, redis_password)
    try:
        raw = slots_redis._call_with_retry(lambda: client.get(f"{PREFIX}focus:{quest_name}"))
    except redis.exceptions.RedisError:
        return None
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _run(op):
    """Same convention as `machines.py`'s `_run`: a connection failure
    becomes `CoordinatorUnreachable`. Only `focus`/`release` use this --
    they write to Redis and have nothing to fall back to (same as the fleet
    registry). `read_focus` above stays lenient on purpose: a read-only
    report has nothing to retry either way, so it folds "unreachable" into
    "no focus" instead.
    """
    try:
        return slots_redis._call_with_retry(op)
    except _REDIS_ERRORS as exc:
        raise CoordinatorUnreachable("quest focus registry") from exc


def _read_focus_strict(
    quest_name: str,
    *,
    redis_host: str | None = None,
    redis_port: int | None = None,
    redis_username: str | None = None,
    redis_password: str | None = None,
) -> dict | None:
    """Same key `read_focus` reads, but raises `CoordinatorUnreachable`
    instead of reporting "no focus" when Redis can't be reached. `release`
    needs to tell the two apart -- it must not print "nothing to release"
    just because the registry is down.
    """
    client = slots_redis._client(redis_host, redis_port, redis_username, redis_password)
    raw = _run(lambda: client.get(f"{PREFIX}focus:{quest_name}"))
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _release_when_hook() -> str | None:
    """`release_when` (docs/redis-schema.md) is a cached guess -- idle,
    stalled, done -- that automatic release (#14) computes. `focus` has no
    such logic yet, so every record it writes leaves this `None`; #14 fills
    it in without anything else here needing to change.
    """
    return None


def write_focus(
    quest_name: str,
    machine: str,
    *,
    pinned: bool,
    redis_host: str | None = None,
    redis_port: int | None = None,
    redis_username: str | None = None,
    redis_password: str | None = None,
) -> dict:
    """Write `focus:<quest>` (docs/redis-schema.md). No TTL -- `release`
    deletes the key outright instead of letting it expire.
    """
    client = slots_redis._client(redis_host, redis_port, redis_username, redis_password)
    record = {
        "machine": machine,
        "pinned": pinned,
        "since": machines._now_iso(),
        "release_when": _release_when_hook(),
    }
    _run(lambda: client.set(f"{PREFIX}focus:{quest_name}", json.dumps(record)))
    return record


def delete_focus(
    quest_name: str,
    *,
    redis_host: str | None = None,
    redis_port: int | None = None,
    redis_username: str | None = None,
    redis_password: str | None = None,
) -> bool:
    client = slots_redis._client(redis_host, redis_port, redis_username, redis_password)
    return bool(_run(lambda: client.delete(f"{PREFIX}focus:{quest_name}")))


def pick_focus_machine(records: list[dict], *, now: float | None = None) -> str | None:
    """The online machine with the most free slots, ties broken by the
    freshest heartbeat -- the same two dimensions `place.py`'s ranking uses
    for "free slots" and "heartbeat age" (`machines._slot_totals`/
    `_heartbeat_age`, shared rather than reimplemented). `place.py`'s third
    dimension, machine state, doesn't need reuse here: this function only
    ever considers `state == "online"` machines in the first place.
    """
    now = now if now is not None else time.time()
    online = [record for record in records if record.get("state") == "online"]
    if not online:
        return None
    ranked = sorted(
        online,
        key=lambda record: (
            -(machines._slot_totals(record.get("slots"))[1] - machines._slot_totals(record.get("slots"))[0]),
            machines._heartbeat_age(record, now),
        ),
    )
    return ranked[0]["name"]


def _blocker_resolved(blocker: dict, by_number: dict[int, dict]) -> bool:
    """A blocker only drops out once it is one of this quest's own tasks
    and that task is done. A blocker this module has no state for -- a
    different quest, a different repo, or no quest at all -- counts as
    still blocking instead of being guessed away (same "report it, don't
    guess" rule `roadmap.py` follows for a broken dependency link).
    """
    task = by_number.get(blocker.get("number"))
    return bool(task and task["done"])


def ready_tasks(quest: dict, dag: dict) -> list[dict]:
    """This quest's not-done tasks with no unresolved blocker, in
    dependency order (ties keep GitHub's own order -- the same ordering
    `status_to_json` uses, via `_task_order`/`_blockers_within_quest`).

    Stricter than `status_to_json`'s per-task "waits on #N" label, which
    only looks at blockers inside the same quest: `quest focus` needs to
    know a task is genuinely runnable right now, including a blocker
    outside the quest entirely (see `_blocker_resolved`).
    """
    in_quest_blockers = _blockers_within_quest(quest["tasks"], quest["repo"], dag)
    ordered = _task_order(quest["tasks"], in_quest_blockers)
    by_number = {task["number"]: task for task in quest["tasks"]}
    entries = {entry["number"]: entry for entry in dag.get("repos", {}).get(quest["repo"], [])}
    ready = []
    for task in ordered:
        if task["done"]:
            continue
        blockers = entries.get(task["number"], {}).get("blockedBy", [])
        if any(not _blocker_resolved(blocker, by_number) for blocker in blockers):
            continue
        ready.append(task)
    return ready


def _first_blocker(quest: dict, dag: dict) -> int | None:
    """The lowest-numbered unresolved blocker across this quest's not-done
    tasks -- named in the "no ready tasks" error so a model knows what to
    check next (an issue inside the quest or, just as often, outside it
    entirely). `None` when every task is already done.
    """
    by_number = {task["number"]: task for task in quest["tasks"]}
    entries = {entry["number"]: entry for entry in dag.get("repos", {}).get(quest["repo"], [])}
    blocking_numbers = {
        blocker["number"]
        for task in quest["tasks"]
        if not task["done"]
        for blocker in entries.get(task["number"], {}).get("blockedBy", [])
        if isinstance(blocker.get("number"), int) and not _blocker_resolved(blocker, by_number)
    }
    return min(blocking_numbers) if blocking_numbers else None


def focus(
    quest_name: str,
    connection: dict,
    repos: list[str],
    *,
    machine: str | None = None,
    pin: bool = False,
    code_dir: str = CODE_DIR,
) -> dict:
    """Pin `quest_name` to a machine so its ready tasks route there, in
    dependency order. Without `machine`, picks the online machine with the
    most free slots (`pick_focus_machine`).

    Raises `QuestNotFound`, `NoReadyTasks`, `MachineNotFound`,
    `MachineDraining`, `NoMachineAvailable`, or `CoordinatorUnreachable` --
    `cli.py` turns each into the copy doc's exact text (lupin-ctl-copy.md
    section 5).
    """
    quests, _warnings = load_quests(repos, code_dir=code_dir)
    quest = find_quest(quests, quest_name)
    if quest is None:
        raise QuestNotFound(quest_name)

    dag = roadmap.cached_dependency_dag(repos, code_dir=code_dir)
    ready = ready_tasks(quest, dag)
    if not ready:
        raise NoReadyTasks(quest["name"], _first_blocker(quest, dag))

    records = machines.machines(connection)
    by_name = {record["name"]: record for record in records}

    if machine:
        record = by_name.get(machine)
        if record is None:
            raise MachineNotFound(machine)
        if record["state"] == "draining":
            raise MachineDraining(machine)
        picked = machine
    else:
        picked = pick_focus_machine(records)
        if picked is None:
            raise NoMachineAvailable()

    write_focus(quest["name"], picked, pinned=pin, **connection)
    return {"quest": quest["name"], "machine": picked, "ready_count": len(ready)}


def release(
    quest_name: str,
    connection: dict,
    repos: list[str],
    *,
    code_dir: str = CODE_DIR,
) -> dict:
    """End `quest_name`'s focus.

    Raises `QuestNotFound` if the name matches no quest, `NoFocus` if it
    has none to release, or `CoordinatorUnreachable`.
    """
    quests, _warnings = load_quests(repos, code_dir=code_dir)
    quest = find_quest(quests, quest_name)
    if quest is None:
        raise QuestNotFound(quest_name)

    current = _read_focus_strict(quest["name"], **connection)
    if not current or not current.get("machine"):
        raise NoFocus(quest["name"])

    delete_focus(quest["name"], **connection)
    return {"quest": quest["name"], "machine": current["machine"]}


def _focus_text(focus: dict | None) -> str:
    if not isinstance(focus, dict) or not focus.get("machine"):
        return "no focus"
    return f"focus: {focus['machine']}"


def _blockers_within_quest(tasks: list[dict], repo: str, dag: dict) -> dict[int, set[int]]:
    """For each task, the subset of its blockedBy links (from
    roadmap.cached_dependency_dag) that point at another task in the same
    quest -- a link to an issue outside the quest doesn't affect this
    quest's own task order.
    """
    numbers = {task["number"] for task in tasks}
    entries = {entry["number"]: entry for entry in dag.get("repos", {}).get(repo, [])}
    return {
        task["number"]: {
            blocker["number"]
            for blocker in entries.get(task["number"], {}).get("blockedBy", [])
            if blocker.get("repo") == repo and blocker["number"] in numbers
        }
        for task in tasks
    }


def _task_order(tasks: list[dict], blockers: dict[int, set[int]]) -> list[dict]:
    """Stable topological sort: a blocker always comes before the task it
    blocks; ties keep their original (GitHub) order. A cycle inside the
    quest's own tasks would otherwise loop forever, so once no task is ready
    the rest are appended as-is instead.
    """
    remaining = list(tasks)
    done_numbers: set[int] = set()
    ordered = []
    while remaining:
        ready = [task for task in remaining if blockers[task["number"]] <= done_numbers]
        if not ready:
            ordered.extend(remaining)
            break
        chosen = ready[0]
        ordered.append(chosen)
        done_numbers.add(chosen["number"])
        remaining = [task for task in remaining if task["number"] != chosen["number"]]
    return ordered


def _task_status(task: dict, blockers: set[int], by_number: dict[int, dict]) -> str:
    if task["done"]:
        return "done"
    undone = sorted(number for number in blockers if not by_number[number]["done"])
    return f"waits on #{undone[0]}" if undone else "ready"


def _symbol(status: str) -> str:
    if status == "done":
        return "✓"  # check mark
    if status.startswith("waits on"):
        return "○"  # open circle
    return "●"  # filled circle


def status_to_json(quest: dict, dag: dict, focus: dict | None) -> dict:
    """One quest's task breakdown, in dependency order."""
    blockers = _blockers_within_quest(quest["tasks"], quest["repo"], dag)
    ordered = _task_order(quest["tasks"], blockers)
    by_number = {task["number"]: task for task in quest["tasks"]}
    tasks_out = [
        {
            "number": task["number"],
            "title": task["title"],
            "done": task["done"],
            "status": _task_status(task, blockers[task["number"]], by_number),
        }
        for task in ordered
    ]
    return {
        "name": quest["name"],
        "repo": quest["repo"],
        "number": quest["number"],
        "doneCount": quest["doneCount"],
        "total": quest["total"],
        "focus": focus,
        "tasks": tasks_out,
    }


def render_status(data: dict) -> str:
    """Format `status_to_json`'s output as text."""
    header = f"{data['name']} · {_focus_text(data['focus'])} · {data['doneCount']} of {data['total']} done"
    if not data["tasks"]:
        return header + "\n  no tasks"
    parts = [
        f"#{task['number']} {_symbol(task['status'])} {task['status']}"
        for task in data["tasks"]
    ]
    return header + "\n  " + "   ".join(parts)


NAME_WIDTH = 18


def to_json(quest: dict, focus: dict | None) -> dict:
    return {
        "name": quest["name"],
        "repo": quest["repo"],
        "number": quest["number"],
        "title": quest["title"],
        "url": quest["url"],
        "done": quest["done"],
        "doneCount": quest["doneCount"],
        "total": quest["total"],
        "tasks": quest["tasks"],
        "focus": focus,
    }


def render_list(quests: list[dict], focuses: dict[str, dict | None]) -> str:
    if not quests:
        return "No quests found."
    lines = []
    for quest in quests:
        open_count = quest["total"] - quest["doneCount"]
        focus_text = _focus_text(focuses.get(quest["name"]))
        lines.append(
            f"{quest['name']:<{NAME_WIDTH}}{quest['doneCount']}/{quest['total']} done   "
            f"{quest['total']} tasks, {open_count} open   {focus_text}"
        )
    return "\n".join(lines)
