"""Quest reporting (issue #11) and `quest start`/`stop` (issue #13).

A quest is a GitHub issue labeled `quest`; its GitHub sub-issues are its
tasks. The reporting half of this module finds quests, works out each
task's done/not-done state, and (if Redis is reachable) reads which
machine is focused on each quest.

`start`/`stop` are a second kind of quest: a set of plain issue numbers the
caller names directly with `--issue`, instead of a GitHub label. `start`
validates them (not claimed, not closed, exists, no blocker outside the
set, target machine not draining), claims them all, picks a machine
(reusing `place.place`'s scoring unless `--machine` names one), and writes
`quest:<id>` (see `docs/redis-schema.md`). `stop` releases whatever that
quest still holds and deletes the record.

`quest focus`/`release` are issue #12 -- not built here.
"""

from __future__ import annotations

import json
import os
import re

import redis

from . import claims, machines as machines_mod, place as place_mod, roadmap, slots_redis
from .roadmap import CODE_DIR
from .slots import CoordinatorUnreachable

QUEST_LABEL = "quest"
PREFIX = "lupin:v1:"

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


# --------------------------------------------------------------------------
# quest start / stop (issue #13)
# --------------------------------------------------------------------------


class QuestError(Exception):
    """A `quest start`/`stop` validation failure. `str(exc)` is the message
    text as-is -- `cli.py` prints it as `error: {exc}` and exits with
    `.exit_code`. Default 1 ("bad input"); `IssueClaimed`/`MachineDraining`
    override it to 2 ("try again later"), the same code `claim` and `place`
    already use for that same shape of failure.
    """

    exit_code = 1


class IssueNotFound(QuestError):
    def __init__(self, number: int):
        super().__init__(f"#{number} does not exist in any enabled repo.")


class IssueClosed(QuestError):
    def __init__(self, number: int):
        super().__init__(f"#{number} is closed. Remove --issue {number}.")


class IssueClaimed(QuestError):
    exit_code = 2

    def __init__(self, number: int, session: str):
        super().__init__(f"#{number} is claimed by {session}. Wait for it or stop that loop.")


class IssueBlocked(QuestError):
    def __init__(self, number: int, blocker: int):
        super().__init__(
            f"#{number} is blocked by #{blocker}, which is not in this quest. "
            f"Add --issue {blocker} or wait for it."
        )


class MachineDraining(QuestError):
    exit_code = 2

    def __init__(self, machine: str):
        super().__init__(f"{machine} is draining and takes no new work. Pick another machine.")


class QuestNotFound(QuestError):
    def __init__(self, quest_id: str):
        super().__init__(f"no quest matches {quest_id!r}")


def _locate_issue(number: int, repos: list[str], code_dir: str):
    """Find which enabled repo holds issue `number`, open or closed.

    Tries each repo's own checkout in turn (same ambient-`gh`-repo
    convention as the rest of this codebase -- see `roadmap._run_json`).
    Returns `(repo, "owner/name", issue-json)` for the first match, or
    `None` if no enabled repo has it.
    """
    for repo in repos:
        repo_path = os.path.join(code_dir, repo)
        owner, name, error = roadmap._repo_identity(repo_path)
        if error:
            continue
        issue, error = roadmap._run_json(
            ["gh", "issue", "view", str(number), "--json", "number,state"], repo_path
        )
        if error or not isinstance(issue, dict):
            continue
        return repo, f"{owner}/{name}", issue
    return None


def _resolve_issues(issue_numbers: list[int], repos: list[str], code_dir: str) -> dict:
    """Existence + closed checks for every issue, in `--issue` order.
    Returns `{number: (repo, "owner/repo#number")}`. Raises `IssueNotFound`
    or `IssueClosed` on the first problem found.
    """
    resolved = {}
    for number in issue_numbers:
        found = _locate_issue(number, repos, code_dir)
        if found is None:
            raise IssueNotFound(number)
        repo, owner_repo, issue = found
        if issue.get("state") == "CLOSED":
            raise IssueClosed(number)
        resolved[number] = (repo, f"{owner_repo}#{number}")
    return resolved


def _check_claims(resolved: dict, issue_numbers: list[int], connection: dict) -> None:
    owner_repos = sorted({target.rpartition("#")[0] for _repo, target in resolved.values()})
    existing = claims.claims_for(owner_repos, **connection)
    for number in issue_numbers:
        _repo, target = resolved[number]
        held = existing.get(target)
        if held:
            raise IssueClaimed(number, held.get("session", "another loop"))


def _check_blocked_by(resolved: dict, issue_numbers: list[int], repos: list[str], code_dir: str) -> dict:
    """Raises `IssueBlocked` if any requested issue is blocked by an issue
    outside the set. Returns the dependency DAG, so `_dependency_order`
    below doesn't fetch it twice.
    """
    dag = roadmap.cached_dependency_dag(repos, code_dir)
    requested = set(issue_numbers)
    for number in issue_numbers:
        repo, _target = resolved[number]
        entries = {entry["number"]: entry for entry in dag.get("repos", {}).get(repo, [])}
        entry = entries.get(number, {})
        for blocker in entry.get("blockedBy", []):
            if blocker["number"] not in requested:
                raise IssueBlocked(number, blocker["number"])
    return dag


def _dependency_order(issue_numbers: list[int], resolved: dict, dag: dict) -> list[int]:
    """Topological order among just the requested issues, across repos --
    reuses `_task_order` above (the same sort `status_to_json` uses), fed a
    blockedBy map restricted to this quest's own issues instead of one
    repo's.
    """
    requested = set(issue_numbers)
    blockers = {}
    for number in issue_numbers:
        repo, _target = resolved[number]
        entries = {entry["number"]: entry for entry in dag.get("repos", {}).get(repo, [])}
        entry = entries.get(number, {})
        blockers[number] = {b["number"] for b in entry.get("blockedBy", []) if b["number"] in requested}
    ordered = _task_order([{"number": number} for number in issue_numbers], blockers)
    return [task["number"] for task in ordered]


def _resolve_machine(machine: str | None, issue_numbers: list[int], connection: dict) -> str:
    """`--machine` wins outright (checked only for draining). Otherwise
    reuse `place.place`'s scoring on the first named issue -- `place` picks
    one machine for one task, and a quest's issues are meant to run under
    one dedicated loop, so the first issue's routing stands in for the
    whole set.
    """
    if machine:
        records = {record["name"]: record for record in machines_mod.machines(connection)}
        record = records.get(machine)
        if record is not None and record["state"] == "draining":
            raise MachineDraining(machine)
        return machine
    placed = place_mod.place(str(issue_numbers[0]), connection)
    pick = placed.get("pick")
    if not pick:
        raise QuestError("no online machine can take this quest right now.")
    return pick


def _claim_all(resolved: dict, issue_numbers: list[int], holder: str, connection: dict) -> None:
    """Claim every issue in order. If any claim fails -- someone else got
    there first (a race past the earlier `_check_claims` pre-check), or
    Redis drops mid-way -- release everything this call already claimed,
    so a quest never half-starts.
    """
    claimed_targets = []
    try:
        for number in issue_numbers:
            _repo, target = resolved[number]
            try:
                claims.claim(target, holder, **connection)
            except claims.ClaimHeld:
                owner_repo = target.rpartition("#")[0]
                existing = claims.claims_for([owner_repo], **connection)
                held = existing.get(target, {})
                raise IssueClaimed(number, held.get("session", "another loop")) from None
            claimed_targets.append(target)
    except Exception:
        for target in claimed_targets:
            try:
                claims.release_claim(target, holder, **connection)
            except CoordinatorUnreachable:
                pass
        raise


_SEQ_KEY = f"{PREFIX}seq:quest"

# KEYS[1] = seq:quest. `INCR` isn't on this project's Redis ACL command
# list (see docs/redis-schema.md), so the counter is read and written with
# plain GET/SET inside one EVAL instead.
_SEQ_SCRIPT = """
local n = tonumber(redis.call('GET', KEYS[1]) or '0') + 1
redis.call('SET', KEYS[1], n)
return n
"""


def _redis_client(connection: dict):
    return slots_redis._client(
        connection.get("redis_host"),
        connection.get("redis_port"),
        connection.get("redis_username"),
        connection.get("redis_password"),
    )


def _next_quest_id(connection: dict) -> str:
    client = _redis_client(connection)
    try:
        n = slots_redis._call_with_retry(lambda: client.eval(_SEQ_SCRIPT, 1, _SEQ_KEY))
    except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as exc:
        raise CoordinatorUnreachable("quest id") from exc
    return f"q{n}"


def _quest_key(quest_id: str) -> str:
    return f"{PREFIX}quest:{quest_id}"


def _write_quest(quest_id: str, record: dict, connection: dict) -> None:
    client = _redis_client(connection)
    try:
        slots_redis._call_with_retry(lambda: client.set(_quest_key(quest_id), json.dumps(record)))
    except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as exc:
        raise CoordinatorUnreachable("quest record") from exc


def read_quest(quest_id: str, connection: dict) -> dict | None:
    """Read `quest:<id>`'s parsed JSON, or `None` if there is no such
    quest. Raises `CoordinatorUnreachable` if Redis can't be reached --
    unlike `read_focus`, a missing quest and an unreachable registry are
    not the same thing here: `stop` needs to tell them apart to know
    whether it has anything to release.
    """
    client = _redis_client(connection)
    try:
        raw = slots_redis._call_with_retry(lambda: client.get(_quest_key(quest_id)))
    except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as exc:
        raise CoordinatorUnreachable("quest record") from exc
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _delete_quest(quest_id: str, connection: dict) -> None:
    client = _redis_client(connection)
    try:
        slots_redis._call_with_retry(lambda: client.delete(_quest_key(quest_id)))
    except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as exc:
        raise CoordinatorUnreachable("quest record") from exc


def start(
    issue_numbers: list[int],
    repos: list[str],
    *,
    connection: dict,
    machine: str | None = None,
    platform: str | None = None,
    note: str | None = None,
    code_dir: str = CODE_DIR,
) -> dict:
    """Validate, claim, and register a quest for `issue_numbers`.

    Raises a `QuestError` subclass (message ready to print as `error:
    ...`) for any of the five documented validation failures, or
    `CoordinatorUnreachable` if Redis can't be reached. On success, writes
    `quest:<id>` (see docs/redis-schema.md) and returns its record plus
    `"id"`.
    """
    seen: set[int] = set()
    deduped = []
    for number in issue_numbers:
        if number not in seen:
            seen.add(number)
            deduped.append(number)
    issue_numbers = deduped

    resolved = _resolve_issues(issue_numbers, repos, code_dir)
    _check_claims(resolved, issue_numbers, connection)
    dag = _check_blocked_by(resolved, issue_numbers, repos, code_dir)
    chosen_machine = _resolve_machine(machine, issue_numbers, connection)
    order = _dependency_order(issue_numbers, resolved, dag)

    quest_id = _next_quest_id(connection)
    holder = f"quest:{quest_id}"
    _claim_all(resolved, issue_numbers, holder, connection)

    record = {
        "issues": issue_numbers,
        "targets": [resolved[number][1] for number in issue_numbers],
        "order": order,
        "machine": chosen_machine,
        "state": "running",
    }
    if platform:
        record["platform"] = platform
    if note:
        record["note"] = note
    _write_quest(quest_id, record, connection)
    return {"id": quest_id, **record}


def render_start(result: dict) -> str:
    issues_text = " ".join(f"#{n}" for n in result["issues"])
    order_text = ", ".join(f"#{n}" for n in result["order"])
    return (
        f"quest {result['id']} started on {result['machine']} · "
        f"{issues_text} claimed · order: {order_text}"
    )


def stop(quest_id: str, *, connection: dict) -> str:
    """Release every issue this quest still holds (one already merged or
    closed releases its own claim, so only what's still claimed needs
    releasing here) and delete `quest:<id>`. Raises `QuestNotFound` if
    `quest_id` doesn't match a running quest, or `CoordinatorUnreachable`
    if Redis can't be reached.
    """
    record = read_quest(quest_id, connection)
    if record is None:
        raise QuestNotFound(quest_id)
    holder = f"quest:{quest_id}"
    issues = record.get("issues", [])
    targets = record.get("targets", [])
    released = [
        number
        for number, target in zip(issues, targets)
        if claims.release_claim(target, holder, **connection)
    ]
    _delete_quest(quest_id, connection)
    if not released:
        return f"quest {quest_id} stopped · nothing left to release, branch kept"
    issue_list = " ".join(f"#{n}" for n in released)
    return f"quest {quest_id} stopped · {issue_list} released to the queue, branch kept"
