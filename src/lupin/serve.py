#!/usr/bin/env python3
"""lupin serve - a mostly read-only web dashboard for the loopctl delegation loops.

The server binds a loopback or tailnet address (100.64.0.0/10); it refuses
to start on any other address. A tailnet bind relies on the headscale ACL
and the host firewall as its boundary (same model as this project's Redis
deployment, docs/redis-schema.md) -- the DNS-rebinding check below still
only accepts the Host header matching what was actually bound. The POST
routes are /quest/start, /quest/stop (both only write to Redis via
quest.py), /machines/slot-max (changes one Redis slot's max holder count --
a validated slot name and a positive integer, nothing else), /loops/close,
/loops/start (issue #21: stop or (re)start one loop), /schedule/timer
(issue #22: start or stop delegation-loop.timer -- always local, this
process never controls another machine's timer), /schedule/run (issue #22:
"Run now" -- dispatch one or more `loop.run`s to a chosen machine or spread
of machines), and /repos/add, /repos/generate-docs, /repos/remove,
/repos/doc/save, /repos/slot-max, /repos/schedule (issue #23: the Repos
page -- add or remove a repo from the local schedule file, view or edit
its delegation doc, raise or lower its loop concurrency cap, and run
`loopctl once` for a one-off, local-only schedule). /loops/start,
/loops/close, /schedule/run, and /repos/schedule run `loopctl
stop|run|once <repo>` as a fixed argv list -- repo name checked against a
strict pattern first, never a shell string -- but only when the loop is on
this machine. For a loop on another fleet machine /loops/start,
/loops/close, and /schedule/run enqueue a signed command instead
(commands.py, issue #28); /repos/schedule has no remote form (agent.py's
ACTIONS table has no `loop.once`) and always runs locally. This process
never touches another machine's loopctl directly. Read probes use fixed
argv lists too, run without a shell. GitHub attachment images use an
authenticated, fixed-host proxy; it sends the GitHub token only to
github.com and strips it before a validated storage redirect. None of
these write routes carry auth of their own -- a reverse proxy in front of
this server is expected to gate write access before a request reaches here.

Every other page reads loop state but does not change it. The installed
CLI can be older than this dashboard; direct reads of tmux and systemd
avoid version skew.

Python standard library only. GitHub image bytes are fetched only when the
browser requests a validated attachment ID.
"""

from __future__ import annotations

import html
import ipaddress
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from importlib import resources
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse, urlsplit

from . import benchmark_fetch, claims, commands, loops, machines, model_fetch, quest, roadmap, slots_redis
from .slots import CoordinatorUnreachable
from .quota import (
    QuotaDuration,
    epoch_ms_to_local,
    quota_source_label,
)

ATTACHMENT_ID = roadmap.ATTACHMENT_ID
ATTACHMENT_REDIRECT_HOST = re.compile(
    r"github-production-user-asset-[a-z0-9-]+\.s3\.amazonaws\.com"
)
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_FORM_BYTES = 8 * 1024
IMAGE_TYPES = {"image/gif", "image/jpeg", "image/png", "image/webp"}
FAVICON = b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><path fill="#e11d48" d="M16 28S3 20.4 3 11.5A7.5 7.5 0 0 1 16 7.4a7.5 7.5 0 0 1 13 4.1C29 20.4 16 28 16 28Z"/></svg>"""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def github_attachment(attachment_id: str) -> tuple[bytes, str] | None:
    try:
        auth = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    token = auth.stdout.strip()
    auth.stdout = ""
    if auth.returncode != 0 or not token:
        return None

    opener = urllib.request.build_opener(NoRedirect())
    request = urllib.request.Request(
        f"https://github.com/user-attachments/assets/{attachment_id}",
        headers={"Authorization": f"token {token}", "Accept": "image/*"},
    )
    token = ""
    try:
        try:
            response = opener.open(request, timeout=15)
        except urllib.error.HTTPError as redirect:
            if redirect.code not in (301, 302, 303, 307, 308):
                redirect.close()
                return None
            location = redirect.headers.get("Location")
            redirect.close()
            try:
                parsed = urlsplit(location or "")
            except ValueError:
                return None
            try:
                port = parsed.port
            except ValueError:
                return None
            if (
                parsed.scheme != "https"
                or parsed.username is not None
                or parsed.password is not None
                or port not in (None, 443)
                or not ATTACHMENT_REDIRECT_HOST.fullmatch(parsed.hostname or "")
            ):
                return None
            response = opener.open(
                urllib.request.Request(location, headers={"Accept": "image/*"}),
                timeout=15,
            )
        with response:
            content_type = response.headers.get_content_type()
            if content_type not in IMAGE_TYPES:
                return None
            data = response.read(MAX_IMAGE_BYTES + 1)
            if len(data) > MAX_IMAGE_BYTES:
                return None
            return data, content_type
    except (OSError, TimeoutError, urllib.error.URLError):
        return None


STATE_DIR = "/var/lib/delegation-loop"
REPOS_FILE = os.path.join(STATE_DIR, "repos")
CODE_DIR = "/code"
LOOP_DOC = "docs/delegation-loop.md"
SESSION_PREFIX = "loop-"

# Tailscale's CGNAT range (100.64.0.0/10). A bind address in this range is a
# tailnet interface, gated by the headscale ACL and the host firewall -- the
# same trust boundary this project's Redis deployment already relies on
# (docs/redis-schema.md). Any other non-loopback address is still refused.
TAILNET_RANGE = ipaddress.ip_network("100.64.0.0/10")


def bind_allowed(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Loopback, or a tailnet address -- see TAILNET_RANGE above."""
    return addr.is_loopback or (addr.version == 4 and addr in TAILNET_RANGE)

# model-tiers.json -- the same file route.py routes a (category, size) pair
# with. It ships as package data, so read it the same way route.py does.
MODEL_TIERS_PATH = str(resources.files("lupin").joinpath("model-tiers.json"))
# The tier keys the data file uses, cheapest first. A category may be
# missing any of them; route.py escalates a missing tier to the next one up.
MODEL_TIER_ORDER = ("tier0", "tier1", "tier2")

# --------------------------------------------------------------------------
# running read-only probes
# --------------------------------------------------------------------------


def run(argv: list[str], timeout: float = 10.0) -> tuple[int, str]:
    """Run a fixed read-only probe. Never a shell, never browser input."""
    return loops.run_subprocess(argv, timeout=timeout)


# --------------------------------------------------------------------------
# reading state
# --------------------------------------------------------------------------


def enabled_repos() -> list[str]:
    try:
        with open(REPOS_FILE, encoding="utf-8") as fh:
            return [line.strip() for line in fh if line.strip()]
    except OSError:
        return []


def write_enabled_repos(names: list[str]) -> None:
    """Replace REPOS_FILE's contents -- the one write path for the
    schedule `enabled_repos()` reads (issue #23; before this, nothing in
    `serve.py` ever wrote this file). One name per line, same format
    `enabled_repos()` already parses.
    """
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(REPOS_FILE, "w", encoding="utf-8") as fh:
        fh.writelines(f"{name}\n" for name in names)


# A per-repo loop concurrency cap (the Repos page's "Loops - max" stepper,
# issue #23) is new ground: docs/redis-schema.md's only slot today is
# `bmo`, fleet-wide, not per repo. slots_redis.py already supports any slot
# name (`status()` reports whichever `slot:<name>:max` keys exist), so this
# reuses that mechanism under a new name instead of inventing a second
# schema -- `repo:<repo>`, not a bare repo name, so it can never collide
# with `bmo` or a future machine-scoped slot. Nothing acquires this slot
# yet (loopctl does not check it before starting a loop), so the number is
# a declared cap only, not an enforced one -- the same kind of gap issue
# #22 documented for "Run now"'s note field.
REPO_SLOT_PREFIX = "repo:"


def _repo_slot_name(repo: str) -> str:
    return f"{REPO_SLOT_PREFIX}{repo}"


def _delegation_doc_template(repo: str) -> str:
    """A minimal starting doc for a repo that has none yet ("Generate docs
    and add", issue #23). No existing doc generator was found in this repo
    (grepped for "generate"/"scaffold"/LOOP_DOC outside serve.py and
    docs/) -- this is a plain fill-in-the-blanks skeleton, not a smart
    generator.
    """
    return (
        f"# {repo} -- the delegation loop\n\n"
        "This file is the loop's entry point for this repo. Fill it in\n"
        "before the first loop runs.\n\n"
        "## What this repo is for\n\n"
        "TODO: say what this repo does, and what the first loops should build.\n\n"
        "## Rules\n\n"
        "TODO: anything a loop must know before it starts -- how to test,\n"
        "what not to touch, where to push its work.\n"
    )


def code_repos() -> list[dict]:
    """Every directory under /code, with whether it can run a loop."""
    out = []
    enabled = set(enabled_repos())
    try:
        names = sorted(
            d.name for d in os.scandir(CODE_DIR) if d.is_dir(follow_symlinks=True)
        )
    except OSError:
        names = []
    for name in names:
        loopable = os.path.isfile(os.path.join(CODE_DIR, name, LOOP_DOC))
        if not loopable:
            state = "no-doc"
        elif name in enabled:
            state = "enabled"
        else:
            state = "disabled"
        out.append({"repo": name, "state": state, "loopable": loopable})
    return out


def tmux_sessions() -> list[dict]:
    fmt = "#{session_name}\t#{session_created}\t#{session_attached}\t#{session_windows}\t#{session_activity}"
    rc, out = run(["tmux", "ls", "-F", fmt])
    sessions = []
    if rc != 0:
        return sessions
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        name, created, attached, windows, activity = parts[:5]
        sessions.append(
            {
                "name": name,
                "repo": name[len(SESSION_PREFIX):] if name.startswith(SESSION_PREFIX) else None,
                "created": int(created) if created.isdigit() else 0,
                "attached": attached == "1",
                "windows": int(windows) if windows.isdigit() else 0,
                "activity": int(activity) if activity.isdigit() else 0,
            }
        )
    return sorted(sessions, key=lambda s: (s["repo"] is None, s["name"]))


def session_tail(session: str, lines: int) -> str:
    rc, out = run(["tmux", "capture-pane", "-pt", session, "-S", f"-{lines}"])
    return out if rc == 0 else f"(could not read pane: {out.strip()})"


def _repo_platforms() -> dict[str, str]:
    """repo -> platform, read straight from `REPOS_FILE` -- the same file
    loopctl.nix's own `repo_platform` reads, second whitespace-separated
    token per line (`"claude"` if a line has none). Not `enabled_repos()`,
    which only keeps the first token -- this is a separate, narrow read of
    the same file, for the one new field that needs the second column.
    """
    result: dict[str, str] = {}
    try:
        with open(REPOS_FILE, encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if parts:
                    result[parts[0]] = parts[1] if len(parts) > 1 else "claude"
    except OSError:
        pass
    return result


def local_loops() -> list[dict]:
    """This machine's live loops, for the fleet heartbeat (issue #2 phase
    A, `machines.py`'s `loops` field): one entry per live tmux loop
    session, `{"repo", "platform", "state", "since"}`. `state` is always
    `None` -- there's no Herdr/agent-state signal on the tmux backend yet.
    """
    platforms = _repo_platforms()
    out = []
    for s in tmux_sessions():
        if not s["repo"]:
            continue
        since = (
            datetime.fromtimestamp(s["created"], tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if s["created"]
            else None
        )
        out.append(
            {
                "repo": s["repo"],
                "platform": platforms.get(s["repo"], "claude"),
                "state": None,
                "since": since,
            }
        )
    return out


def timers() -> list[dict]:
    """Delegation timers, from systemd's own JSON. Times are epoch seconds."""
    rc, out = run(["systemctl", "list-timers", "--all", "--no-pager", "--output=json"])
    if rc != 0:
        return []
    try:
        rows = json.loads(out)
    except json.JSONDecodeError:
        return []
    result = []
    for row in rows:
        unit = row.get("unit") or ""
        if unit != "delegation-loop.timer" and not unit.startswith("delegation-loop-once-"):
            continue
        nxt = row.get("next")
        last = row.get("last")
        result.append(
            {
                "unit": unit,
                "activates": row.get("activates") or "",
                "next": nxt / 1e6 if isinstance(nxt, (int, float)) and nxt else None,
                "last": last / 1e6 if isinstance(last, (int, float)) and last else None,
            }
        )
    return sorted(result, key=lambda t: (t["next"] is None, t["next"] or 0))


def oneoff_repositories(unit: str) -> str:
    """Read repo arguments from a one-off timer's launch command."""
    rc, output = run(
        ["systemctl", "show", unit, "--property=ExecStart", "--value"]
    )
    if rc != 0:
        return unit
    match = re.search(r"(?:^|\s)argv\[\]=(.+?)(?:\s*;\s*[^;{}]+=|}\s*$)", output)
    if not match:
        return unit
    try:
        argv = shlex.split(match.group(1))
    except ValueError:
        return unit
    launcher = next(
        (index for index, arg in enumerate(argv) if arg.endswith("/delegation-launch")),
        None,
    )
    if launcher is None:
        return unit
    args = argv[launcher + 1 :]
    repos = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in ("--note", "--platform"):
            index += 2
        elif arg.startswith("--"):
            return unit
        else:
            repos.extend(args[index:])
            break
    return ", ".join(repos) if repos else unit


def timer_repository(unit: str) -> str:
    if unit == "delegation-loop.timer":
        return "all enabled repos"
    return oneoff_repositories(unit)


def timer_active() -> bool:
    rc, _ = run(["systemctl", "is-active", "--quiet", "delegation-loop.timer"])
    return rc == 0


def fleet_state(connection: dict) -> dict:
    """Machines and claims from the cross-machine Redis registry (issue
    #15). Degrades the same way the local readers above do: a Redis outage
    returns empty data and an error string instead of raising -- claims has
    no local fallback either way (see claims.py), so a claims-only failure
    just leaves that part empty without blanking the machines list.

    `repos` for `claims.claims_for` is built the same way
    `roadmap_cli.build_roadmap` already does: `enabled_repos()` for the
    short names, `roadmap._repo_identity()` to resolve each one's GitHub
    owner from its local checkout. A repo with no local checkout (or no
    `gh` access) is silently skipped, same as `/roadmap` already tolerates.
    """
    try:
        machine_list = machines.machines(connection)
    except machines.CoordinatorUnreachable as exc:
        return {"machines": [], "claims": {}, "fleet_error": str(exc)}

    full_names = {}
    for repo in enabled_repos():
        owner, _name, _warning = roadmap._repo_identity(os.path.join(CODE_DIR, repo))
        if owner:
            full_names[repo] = f"{owner}/{repo}"

    claims_data: dict = {}
    if full_names:
        try:
            claims_data = claims.claims_for(
                list(full_names.values()),
                redis_host=connection.get("redis_host"),
                redis_port=connection.get("redis_port"),
                redis_username=connection.get("redis_username"),
                redis_password=connection.get("redis_password"),
            )
        except claims.CoordinatorUnreachable:
            pass
    return {"machines": machine_list, "claims": claims_data, "fleet_error": None}


# Mirrors agent.py's own _REPO_RE. Kept as a separate copy, not imported --
# this is a second, independent check (defense in depth, same reasoning
# agent.py gives for checking a repo name even though loopctl checks its
# own too): this process should refuse a bad repo name before it ever
# reaches loopctl or the command queue, not rely on the far end alone.
REPO_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


def _valid_repo_name(repo: str) -> bool:
    return bool(REPO_NAME_RE.match(repo))


def _repo_full_names() -> dict[str, str]:
    """short repo name -> "owner/repo", for each enabled repo this host can
    resolve from its own local checkout. Same mapping `fleet_state()` builds
    for its own claims lookup -- built again here, not shared, to keep each
    function a small, self-contained read.
    """
    full_names = {}
    for repo in enabled_repos():
        owner, _name, _warning = roadmap._repo_identity(os.path.join(CODE_DIR, repo))
        if owner:
            full_names[repo] = f"{owner}/{repo}"
    return full_names


def remote_loop_hosts(claims_data: dict, full_names: dict[str, str], local_host: str) -> dict[str, str]:
    """repo -> host, for a repo whose issue is claimed by a machine other
    than this one.

    This is a guess, not a real signal (issue #2 phase A): a claim's
    `host` field is not proof a loop is still running there, just the
    best guess available before `_loop_hosts_from_heartbeat`'s real
    signal existed. `gather_loops` merges this result with that one --
    for a repo `_loop_hosts_from_heartbeat` already has an answer for,
    this guess is ignored; for any other repo, this guess fills in.
    """
    hosts: dict[str, str] = {}
    for repo, owner_repo in full_names.items():
        for target, info in claims_data.items():
            if target.rpartition("#")[0] != owner_repo:
                continue
            host = info.get("host")
            if host and host != local_host:
                hosts[repo] = host
                break
    return hosts


def _loop_hosts_from_heartbeat(machine_records: list[dict], local_host: str) -> dict[str, str]:
    """repo -> host, read straight from every other machine's own
    heartbeat `loops` list (issue #2 phase A). This is the real signal,
    once a machine runs a build that publishes `loops`. A machine that
    doesn't publish it yet contributes nothing here -- `gather_loops`
    merges this result with `remote_loop_hosts`'s guess to cover those
    repos too.
    """
    hosts: dict[str, str] = {}
    for record in machine_records:
        if record.get("name") == local_host:
            continue
        for loop in record.get("loops", []):
            repo = loop.get("repo")
            if repo:
                hosts[repo] = record["name"]
    return hosts


def gather_loops(connection: dict) -> dict:
    """Every loopable repo, with where (if anywhere) its loop looks like it
    is running.

    `status` is `"running"` (a live tmux session on this machine),
    `"remote"` (claimed by a different fleet machine -- see
    `remote_loop_hosts`), or `"stopped"` (neither). `machine` is this
    host's name for `"running"`/`"stopped"`, or the claiming host for
    `"remote"`.
    """
    local_host = machines.hostname()
    sessions_by_repo = {s["repo"]: s for s in tmux_sessions() if s["repo"]}
    enabled = set(enabled_repos())
    state = fleet_state(connection)
    # Merge, not all-or-nothing: a heartbeat-reported loop wins for the
    # repo it names, but a repo the heartbeat set says nothing about still
    # falls back to the claims-based guess (e.g. a machine running an
    # older `lupin` that doesn't publish `loops` yet).
    claims_hosts = remote_loop_hosts(state.get("claims", {}), _repo_full_names(), local_host)
    heartbeat_hosts = _loop_hosts_from_heartbeat(state.get("machines", []), local_host)
    remote_hosts = {**claims_hosts, **heartbeat_hosts}

    entries = []
    for r in code_repos():
        if not r["loopable"]:
            continue
        repo = r["repo"]
        session = sessions_by_repo.get(repo)
        if session is not None:
            status, machine = "running", local_host
        elif repo in remote_hosts:
            status, machine = "remote", remote_hosts[repo]
        else:
            status, machine = "stopped", local_host
        entries.append(
            {
                "repo": repo,
                "enabled": repo in enabled,
                "status": status,
                "machine": machine,
                "session": session,
            }
        )
    entries.sort(key=lambda e: e["repo"])
    return {
        "entries": entries,
        "machines": state.get("machines", []),
        "fleet_error": state.get("fleet_error"),
        "local_host": local_host,
    }


def gather(peek_lines: int, connection: dict | None = None) -> dict:
    sessions = tmux_sessions()
    for s in sessions:
        s["tail"] = session_tail(s["name"], peek_lines) if s["repo"] else ""
    state = {
        "now": time.time(),
        "enabled": enabled_repos(),
        "repos": code_repos(),
        "sessions": sessions,
        "timers": timers(),
        "timer_active": timer_active(),
    }
    state.update(fleet_state(connection or {}))
    return state


def gather_repos(connection: dict) -> dict:
    """Everything the Repos page (issue #23) reads: every /code directory
    tagged enabled/disabled/no-doc (`code_repos()`), each one's live loop
    status folded in from `gather_loops()` (one fleet read, not a second
    one), and each loopable repo's concurrency cap from the generic slot
    registry (see `_repo_slot_name`'s docstring for why that's a new slot
    name, not a new schema).
    """
    loops = gather_loops(connection)
    by_repo = {e["repo"]: e for e in loops["entries"]}
    slot_status = slots_redis.status(**connection)
    repos = []
    for r in code_repos():
        entry = by_repo.get(r["repo"])
        slot = slot_status.get(_repo_slot_name(r["repo"]), {})
        repos.append(
            {
                **r,
                "running": entry is not None and entry["status"] in ("running", "remote"),
                "machine": entry["machine"] if entry else loops["local_host"],
                "max": slot.get("max"),
            }
        )
    return {
        "repos": repos,
        "machines": loops["machines"],
        "fleet_error": loops["fleet_error"],
        "local_host": loops["local_host"],
    }


def gather_schedule(connection: dict) -> dict:
    """Everything the Schedule page (issue #22) reads: the timer list (same
    `timers()` the Overview page already uses), and the fleet machine list
    for the "Run now" placement picker (same `fleet_state()` /machines and
    /loops already read -- one reading of the registry, not a new one)."""
    state = fleet_state(connection)
    return {
        "now": time.time(),
        "enabled": enabled_repos(),
        "timers": timers(),
        "timer_active": timer_active(),
        "machines": state.get("machines", []),
        "fleet_error": state.get("fleet_error"),
        "local_host": machines.hostname(),
    }


def _rank_candidates(records: list[dict], local_host: str) -> list[dict]:
    """Online machines for "Run now" placement, most free loop capacity
    first (ties broken by name, for a deterministic "spread out" rotation).

    Judgment call -- not `place.py`'s `_rank_key`: that ranking scores one
    quota-routed task against a provider's remaining quota (claude/opencode
    usage windows). Starting a recurring loop has no such task to classify
    or provider to route to -- a loop picks its own model per-task once it
    is running. The only dimension that still applies here is free slot
    capacity (`machines._slot_totals`, the same helper `place.py` itself
    uses for its own free-slots tiebreak), so that is all this uses.

    Includes `local_host` only when the registry has no record for it at
    all (or can't be reached, in which case `records` is already `[]`) --
    same "the local machine is always a valid target" rule `do_loops_start`
    applies. If the registry *does* have a record for `local_host`, that
    record's own state wins instead: a `local_host` that has drained itself
    is excluded here exactly like any other draining machine, not silently
    treated as available just because it happens to be where this process
    runs.
    """
    online = [r for r in records if r["state"] == "online"]
    if local_host not in {r["name"] for r in records}:
        online.append({"name": local_host, "state": "online", "slots": {}})

    def free_slots(record: dict) -> int:
        used, max_ = machines._slot_totals(record.get("slots"))
        return max_ - used

    return sorted(online, key=lambda r: (-free_slots(r), r["name"]))


def _machine_available(name: str, records: list[dict], local_host: str) -> bool:
    """Whether `name` is a legal "Run now" placement target: a known
    machine must report `state == "online"`; an unregistered name is only
    accepted when it is `local_host` itself (same fallback `_rank_candidates`
    uses). This is the single check both the placement <select> (via
    `_rank_candidates`, for the generic "spread"/"any" choices) and a
    user-pinned specific machine name go through -- a pinned name does not
    get a looser rule than an automatic pick would.
    """
    record = next((r for r in records if r["name"] == name), None)
    if record is None:
        return name == local_host
    return record["state"] == "online"


def _timer_loop_count(unit: str, repo_label: str, enabled: list[str]) -> int:
    """How many separate loops one timer's next firing starts -- the
    Schedule table's "Loops" column. `delegation-loop.timer` runs every
    enabled repo; a one-off timer runs whatever repos were passed on its
    command line, already resolved into `repo_label` by
    `timer_repository()`/`oneoff_repositories()`.

    `repo_label == unit` is `oneoff_repositories()`'s own "could not parse
    this unit's ExecStart" fallback (it returns the unit name itself) -- a
    real repo list is never equal to its timer's unit name, so this is a
    safe way to detect that case and report "at least one" instead of
    guessing a count from an unparsed string.
    """
    if unit == "delegation-loop.timer":
        return len(enabled)
    if repo_label == unit:
        return 1
    return len([part for part in repo_label.split(", ") if part])


# --------------------------------------------------------------------------
# html
# --------------------------------------------------------------------------

CSS = """
:root{
--ok:#1f7a6a;--warn:#d2512e;--ink:#14201e;--bg:#f2f6f5;--surface:#ffffff;
--side:#e4eeec;--ink2:#4a5b58;--ink3:#5f706d;--line:#d3e0dd;--line2:#e3ecea;
--track:#dae6e3;--warnbg:#fff0ea;--warnline:#f6cdbd;--warnink:#a03c1a;
--term:#10201e;--termink:#dfece9;--frame:#c3d3cf;--idle:#9fb0ac;
--lav:#26636b;--lavbg:#dcebe8;--okbg:#e1f1ee;
/* The mockup loads these two from Google Fonts. We don't load that file
(CSP blocks it), so the names below are unused and every browser falls
through to the system font right after them. */
--sans:Nunito,"Segoe UI Rounded",ui-rounded,-apple-system,"Segoe UI",system-ui,sans-serif;
--mono:"Geist Mono",ui-monospace,"SF Mono","Cascadia Code","Roboto Mono",monospace;
/* legacy names: src/lupin/roadmap.py's own CSS still refers to these */
--fg:var(--ink);--dim:var(--ink3);--card:var(--surface);--accent:var(--ok);--code:var(--track);
}
:root[data-theme="dark"]{
--ok:#4cc2ad;--warn:#ef8a5c;--ink:#e8f1ef;--bg:#101615;--surface:#172120;
--side:#0c1110;--ink2:#a9bcb8;--ink3:#8da29d;--line:#273532;--line2:#1e2927;
--track:#222f2c;--warnbg:#35211a;--warnline:#5e3626;--warnink:#f5b394;
--term:#0a100f;--termink:#dbe8e5;--frame:#2f3f3b;--idle:#72857f;
--lav:#7fc7cf;--lavbg:#1c2c2c;--okbg:#18291f;
}
*{box-sizing:border-box}
html,body{margin:0}
body{background:var(--bg);color:var(--ink);font:15px/1.5 var(--sans)}
a{color:var(--ok);text-decoration:none}
a:hover{color:var(--ink);text-decoration:underline}
.shell{display:flex;min-height:100vh}
.side{width:200px;flex:none;background:var(--side);border-right:1px solid var(--line);
padding:22px 14px;display:flex;flex-direction:column;position:sticky;top:0;
height:100vh;overflow:auto}
.brand{font:700 19px var(--mono);padding:0 8px 22px 8px}
.navlinks{display:grid;gap:2px}
.navlink{display:flex;align-items:center;gap:9px;padding:8px 10px;border-radius:10px;
font-size:14px;color:var(--ink)}
.navlink svg{color:var(--ink2)}
.navlink:hover{background:var(--line2);text-decoration:none}
.navlink.active{background:var(--lavbg);font-weight:600}
.content{flex:1;min-width:0;display:flex;flex-direction:column}
.topbar{display:flex;align-items:center;gap:14px;padding:12px 28px;
border-bottom:1px solid var(--line);font-size:13px;color:var(--ink2)}
.topbar .sp{flex:1}
.autolabel{display:flex;align-items:center;gap:6px;cursor:pointer}
.iconbtn{width:32px;height:32px;flex:none;padding:0;display:flex;align-items:center;
justify-content:center;border-radius:10px;border:1px solid var(--line);
background:var(--surface);color:var(--ink);cursor:pointer}
.iconbtn:hover{filter:brightness(.94)}
.iconbtn:focus-visible{outline:2px solid var(--ok);outline-offset:2px}
.iconbtn .icon-sun{display:none}
:root[data-theme="dark"] .iconbtn .icon-sun{display:inline-flex}
:root[data-theme="dark"] .iconbtn .icon-moon{display:none}
main{max-width:1500px;padding:1.5rem 1.75rem 4rem;flex:1;min-width:0}
h1{font-size:1.25rem;margin:0}
h2{font-size:.8rem;margin:2rem 0 .6rem;color:var(--ink2);font-family:var(--mono);
text-transform:uppercase;letter-spacing:.06em}
header{display:flex;gap:1rem;align-items:center;flex-wrap:wrap;
border-bottom:1px solid var(--line);padding-bottom:.8rem;margin-bottom:.2rem}
header h1{display:flex;align-items:center;gap:10px;font:600 21px var(--mono)}
header h1 svg{color:var(--ok)}
header .sp{flex:1}
header a{font-size:13px}
.dim{color:var(--ink3)}
.mono{font-family:var(--mono)}
.card{background:var(--surface);border:1px solid var(--line);border-radius:18px;
box-shadow:0 3px 0 var(--line);padding:.9rem 1.1rem;margin-bottom:.7rem}
.row{display:flex;gap:.8rem;align-items:center;flex-wrap:wrap}
.pill{font-size:.75rem;padding:.15rem .6rem;border-radius:99px;
border:1px solid var(--line);color:var(--ink2)}
.pill.on{color:var(--ok);border-color:var(--ok)}
.pill.off{color:var(--warn);border-color:var(--warn)}
.big{font-size:1.05rem;font-weight:600}
pre{background:var(--term);color:var(--termink);border-radius:14px;
padding:.6rem .7rem;overflow-x:auto;font:400 12px/1.5 var(--mono);margin:.6rem 0 0;
max-height:16rem;white-space:pre}
table{border-collapse:collapse;width:100%;font-size:14px}
td,th{text-align:left;padding:.5rem .6rem;border-bottom:1px solid var(--line2)}
th{color:var(--ink3);font-weight:500;font-size:.72rem;letter-spacing:.04em;
text-transform:uppercase}
.section-head{display:flex;align-items:center;gap:7px;margin:1.8rem 0 .7rem}
.section-head svg{color:var(--ink2)}
.section-head h2{margin:0}
.stat-row{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}
.stat{display:flex;flex-direction:column;gap:5px}
.stat svg{color:var(--ink2)}
.stat-label{font-size:.72rem;letter-spacing:.06em;text-transform:uppercase;
color:var(--ink3);font-family:var(--mono);display:flex;align-items:center;gap:6px}
.stat-value{font-size:26px;font-weight:600}
.stat-value.mono{font-family:var(--mono)}
.stat-note{font-size:.8rem;color:var(--ink2)}
.stat.warn{background:var(--warnbg);border-color:var(--warnline);box-shadow:none}
.stat.warn .stat-label,.stat.warn .stat-value{color:var(--warnink)}
.stat.ok .stat-value{color:var(--ok)}
.loop-grid{display:grid;gap:10px}
.loop-card{display:block;color:inherit}
.loop-card:hover{border-color:var(--ink3);text-decoration:none}
.loop-head{display:flex;align-items:center;gap:10px;font-size:13px;
color:var(--ink2);flex-wrap:wrap}
.loop-head b{font-size:15px;color:var(--ink);font-weight:600}
.dot{width:8px;height:8px;border-radius:2px;background:var(--ok);flex:none}
.dot.idle{background:var(--idle)}
.loop-tail{margin:10px 0 0;max-height:4.6em}
.loop-empty{display:flex;gap:14px;align-items:center}
@media(max-width:860px){
.shell{flex-direction:column}
.side{width:auto;height:auto;position:static;flex-direction:row;align-items:center;
gap:14px;padding:12px 16px;overflow-x:auto}
.navlinks{display:flex;flex-direction:row;gap:4px}
.navlink span{display:none}
main{padding:1rem 1rem 3rem}
.stat-row{grid-template-columns:1fr}
}
.quota-heading{display:flex;justify-content:space-between;align-items:baseline;
gap:1rem;flex-wrap:wrap}
.scroll{overflow-x:auto}
.quota-summary{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));
gap:1rem;margin:1.5rem 0}
.quota-summary-key{font-size:.75rem;color:var(--dim);letter-spacing:.06em;
text-transform:uppercase}
.quota-summary-value{font-size:1.15rem;font-weight:600;margin-top:.35rem}
.quota-summary-note{font-size:.8rem;color:var(--dim);margin-top:.15rem}
.quota-legend{display:flex;gap:1.25rem;flex-wrap:wrap;color:var(--dim);
font-size:.85rem;margin:1.25rem 0}
.quota-legend-item{display:flex;align-items:center;gap:.5rem}
.quota-legend-used{width:28px;height:8px;border-radius:4px;background:var(--accent)}
.quota-legend-time{width:2px;height:14px;background:var(--fg)}
.quota-legend-ahead{width:28px;height:8px;border-radius:4px;background:var(--warn)}
.quota-groups{display:grid;gap:.8rem}
.quota-group{padding:1.2rem 1.4rem .4rem}
.quota-group-heading{display:flex;justify-content:space-between;align-items:baseline;
gap:.75rem;flex-wrap:wrap;margin-bottom:.4rem}
.quota-group-heading h3{margin:0;font-size:1.1rem}
.quota-row{display:grid;grid-template-columns:minmax(90px,120px) minmax(0,1fr)
minmax(130px,170px);gap:1.5rem;align-items:center;padding:1rem 0;
border-top:1px solid var(--line)}
.quota-window{font-weight:500}
.quota-status{font-size:.8rem;color:var(--dim);margin-top:.2rem}
.quota-status.ahead{color:var(--warn)}
.quota-status.under{color:var(--accent)}
.quota-values{display:flex;justify-content:space-between;gap:1rem;
font-size:.85rem;color:var(--dim);margin-bottom:.45rem}
.quota-values strong{font-size:1.2rem;color:var(--fg)}
.quota-meter{position:relative;height:9px;border-radius:5px;background:var(--line)}
.quota-meter-used{position:absolute;inset:0 auto 0 0;border-radius:5px;
background:var(--accent)}
.quota-meter-used.ahead{background:var(--warn)}
.quota-meter-elapsed{position:absolute;top:-4px;bottom:-4px;width:2px;
border-radius:1px;background:var(--fg)}
.quota-reset{text-align:right}
.quota-reset-left{font:500 1rem ui-monospace,monospace}
.quota-reset-at{font:400 .75rem ui-monospace,monospace;color:var(--dim);margin-top:.2rem}
@media(max-width:700px){.quota-row{grid-template-columns:1fr;gap:.5rem}
.quota-reset{text-align:left}}
.tier-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:.8rem}
.tier-card{margin-bottom:0;display:flex;flex-direction:column;gap:.45rem}
.tier-heading{display:flex;justify-content:space-between;align-items:baseline;gap:.75rem}
.tier-heading h3{margin:0;font-size:1.05rem}
.tier-row{display:grid;grid-template-columns:3.5rem minmax(0,1fr);gap:.75rem;
align-items:baseline;padding-top:.5rem;border-top:1px solid var(--line)}
.tier-name{font-size:.75rem;letter-spacing:.06em;text-transform:uppercase;
color:var(--dim)}
.tier-picks{display:flex;flex-wrap:wrap;gap:.4rem;align-items:baseline}
.tier-pick{display:inline-flex;gap:.4rem;align-items:baseline;
border:1px solid var(--line);border-radius:6px;padding:.1rem .45rem;
background:var(--code)}
.tier-model{font:500 .9rem ui-monospace,monospace}
.tier-arrow{color:var(--dim)}
.tier-note{margin:.15rem 0 0;font-size:.85rem}
.issue-details{border-top:1px solid var(--line);margin-top:.5rem;padding-top:.35rem}
.issue-details summary{cursor:pointer}
.issue-body{white-space:pre-wrap;overflow-wrap:anywhere;margin:.35rem 0}
.activity-comments{padding-left:1.5rem}
.loops-shell{display:flex;gap:1rem;align-items:flex-start}
.loops-side{width:260px;flex:none;padding:.6rem}
.loops-main{flex:1;min-width:0}
.loops-row{display:block;padding:.3rem .2rem;color:inherit}
.loops-row.sel{font-weight:700}
.loops-group{color:var(--ink3);margin-top:.6rem;font-size:.8rem}
.err{color:#9b2226}
"""

JS = """
// Live-tick the relative times, and reload on a timer if the box is ticked.
function fmt(s){s=Math.max(0,Math.round(s));
 var d=Math.floor(s/86400),h=Math.floor(s%86400/3600),
     m=Math.floor(s%3600/60),x=s%60;
 if(d)return d+"d "+h+"h"; if(h)return h+"h "+m+"m";
 if(m)return m+"m "+x+"s"; return x+"s";}
function tick(){var now=Date.now()/1000;
 document.querySelectorAll("[data-since]").forEach(function(e){
   e.textContent=fmt(now-parseFloat(e.dataset.since))+" ago";});
 document.querySelectorAll("[data-until]").forEach(function(e){
   var d=parseFloat(e.dataset.until)-now;
   e.textContent=d>0?("in "+fmt(d)):("overdue by "+fmt(-d));});}
setInterval(tick,1000);tick();
var box=document.getElementById("auto");
if(box){box.checked=localStorage.getItem("lupin-auto")==="1";
 box.addEventListener("change",function(){
   localStorage.setItem("lupin-auto",box.checked?"1":"0");});
 setInterval(function(){if(box.checked)location.reload();},10000);}
document.querySelectorAll("[data-once-repo]").forEach(function(row){
 var when=row.querySelector("input"),command=row.querySelector("code"),
     status=row.querySelector("[data-copy-status]");
 function update(){command.textContent="loopctl once "+row.dataset.onceRepo+" "+when.value;}
 when.addEventListener("input",update);
 row.querySelector("button").addEventListener("click",function(){
   Promise.resolve().then(function(){
     return navigator.clipboard.writeText(command.textContent);
   }).then(function(){status.textContent="copied";},function(){
     status.textContent="copy failed";
   }).then(function(){setTimeout(function(){status.textContent="";},2000);});
 });
});
var themeBtn=document.getElementById("theme-toggle");
if(themeBtn){themeBtn.addEventListener("click",function(){
  var root=document.documentElement;
  var next=root.getAttribute("data-theme")==="dark"?"light":"dark";
  root.setAttribute("data-theme",next);
  try{localStorage.setItem("lupin-theme",next);}catch(e){}});}
"""

# Runs before the stylesheet paints, so the page never flashes the wrong
# theme. No network access, no state beyond one localStorage key.
THEME_BOOTSTRAP = """<script>(function(){try{
var t=localStorage.getItem("lupin-theme");
if(!t){t=matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light";}
document.documentElement.setAttribute("data-theme",t);
}catch(e){}})();</script>"""

# (nav key, path, label, icon path data) -- icon paths are the same ones the
# design mockup uses for these pages, so the sidebar and page headers agree.
NAV_ITEMS = [
    ("overview", "/", "Overview", "M3 11l9-8 9 8M5 10v10h14V10"),
    (
        "machines",
        "/machines",
        "Machines",
        "M4 4h16v6H4zM4 14h16v6H4zM8 7h.01M8 17h.01",
    ),
    (
        "loops",
        "/loops",
        "Loops",
        "M17 2l4 4-4 4M3 11V9a3 3 0 013-3h15M7 22l-4-4 4-4M21 13v2a3 3 0 01-3 3H3",
    ),
    (
        "schedule",
        "/schedule",
        "Schedule",
        "M4 5h16v16H4zM4 10h16M8 3v4M16 3v4",
    ),
    (
        "repos",
        "/repos",
        "Repos",
        "M6 3v12M18 9a3 3 0 100-6 3 3 0 000 6zM6 21a3 3 0 100-6 3 3 0 000 6zM18 9a9 9 0 01-9 9",
    ),
    ("roadmap", "/roadmap", "Roadmap", "M5 21V4M5 4h12l-2 4 2 4H5"),
    ("usage", "/usage", "Usage", "M5 20V10M12 20V4M19 20v-7"),
    (
        "models",
        "/model-tiers",
        "Models",
        "M7 7h10v10H7zM9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3",
    ),
]
SUN_ICON = "M12 8a4 4 0 100 8 4 4 0 000-8zM12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"
MOON_ICON = "M20 13.5A8.5 8.5 0 1110.5 4a6.5 6.5 0 009.5 9.5zM18 2v3M16.5 3.5h3"


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def icon(d: str, size: int = 16) -> str:
    """A stroke-style icon, matching the mockup's svg icons. `d` is always
    one of the fixed path strings above, never caller-supplied text."""
    return (
        f'<svg aria-hidden="true" width="{size}" height="{size}" viewBox="0 0 24 24" '
        'fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" '
        f'stroke-linejoin="round" style="flex:none"><path d="{d}"></path></svg>'
    )


def render_nav(active: str) -> str:
    links = "".join(
        f"<a class='navlink{' active' if key == active else ''}' href='{href}'>"
        f"{icon(path)}<span>{esc(label)}</span></a>"
        for key, href, label, path in NAV_ITEMS
    )
    return (
        "<nav class=side><div class=brand>lupin</div>"
        f"<div class=navlinks>{links}</div><div style='flex:1'></div></nav>"
    )


def render_topbar() -> str:
    return (
        "<div class=topbar>"
        "<label class=autolabel><input type=checkbox id=auto> auto-refresh</label>"
        "<span class=sp></span>"
        "<button type=button id=theme-toggle class=iconbtn aria-label='Toggle dark mode' "
        "title='Toggle dark mode'>"
        f"<span class=icon-sun>{icon(SUN_ICON, 17)}</span>"
        f"<span class=icon-moon>{icon(MOON_ICON, 17)}</span>"
        "</button></div>"
    )


def page(
    title: str, body: str, extra_css: str = "", extra_js: str = "", active: str = ""
) -> bytes:
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        f"{THEME_BOOTSTRAP}"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<link rel=icon href='/favicon.ico' type='image/svg+xml'>"
        f"<title>{esc(title)}</title><style>{CSS}{extra_css}</style></head>"
        f"<body><div class=shell>{render_nav(active)}<div class=content>"
        f"{render_topbar()}<main>{body}</main></div></div>"
        f"<script>{JS}{extra_js}</script></body></html>"
    ).encode("utf-8")


def render_dashboard(state: dict) -> bytes:
    loops = [s for s in state["sessions"] if s["repo"]]
    others = [s for s in state["sessions"] if not s["repo"]]
    enabled = set(state["enabled"])

    recurring = [t for t in state["timers"] if t["unit"] == "delegation-loop.timer"]
    oneoffs = [t for t in state["timers"] if t["unit"] != "delegation-loop.timer"]
    nxt = recurring[0]["next"] if recurring and recurring[0]["next"] else None

    no_doc = [r for r in state["repos"] if r["state"] == "no-doc"]
    attention = []
    if not state["timer_active"]:
        attention.append("timer paused")
    if no_doc:
        attention.append(f"{len(no_doc)} repo(s) missing docs/delegation-loop.md")

    body = [f'<header><h1>{icon("M3 11l9-8 9 8M5 10v10h14V10")}Overview</h1></header>']

    # ---- the three questions the overview exists to answer ---------------
    body.append('<div class="stat-row">')
    body.append(
        '<div class="card stat">'
        f'<div class="stat-label">{icon("M17 2l4 4-4 4M3 11V9a3 3 0 013-3h15M7 22l-4-4 4-4M21 13v2a3 3 0 01-3 3H3", 13)}Live loops</div>'
        f'<div class="stat-value">{esc(len(loops))}</div>'
        f'<div class="stat-note">{esc(len(enabled))} repos enabled</div></div>'
    )
    if nxt:
        body.append(
            '<div class="card stat">'
            f'<div class="stat-label">{icon("M12 7v5l3 2M12 3a9 9 0 100 18 9 9 0 000-18z", 13)}Next run</div>'
            f'<div class="stat-value mono" data-until="{nxt:.0f}"></div>'
            f'<div class="stat-note">{esc(time.strftime("%a %H:%M:%S %Z", time.localtime(nxt)))}</div></div>'
        )
    else:
        body.append(
            '<div class="card stat">'
            f'<div class="stat-label">{icon("M12 7v5l3 2M12 3a9 9 0 100 18 9 9 0 000-18z", 13)}Next run</div>'
            '<div class="stat-value">None scheduled</div></div>'
        )
    body.append(
        f'<div class="card stat {"warn" if attention else "ok"}">'
        f'<div class="stat-label">{icon("M12 3l10 18H2zM12 10v5M12 18h.01", 13)}Needs attention</div>'
        f'<div class="stat-value">{esc("; ".join(attention)) if attention else "All clear"}</div></div>'
    )
    body.append("</div>")

    # ---- live sessions ----------------------------------------------------
    body.append(
        f'<div class="section-head">{icon("M17 2l4 4-4 4M3 11V9a3 3 0 013-3h15M7 22l-4-4 4-4M21 13v2a3 3 0 01-3 3H3", 15)}<h2>Live loops</h2></div>'
    )
    if not loops:
        body.append(
            "<div class='card loop-empty dim'>No loop session is open."
            + (
                f" Next run <span data-until='{nxt:.0f}'></span>."
                if nxt
                else " No next run scheduled."
            )
            + "</div>"
        )
    body.append('<div class="loop-grid">')
    for s in loops:
        dot = "ok" if s["attached"] or s["activity"] else "idle"
        body.append(
            f"<a class='card loop-card' href='/loops?repo={esc(s['repo'])}&lines=400'>"
        )
        body.append('<div class="loop-head">')
        body.append(f"<span class='dot {dot}'></span>")
        body.append(f"<b>{esc(s['repo'])}</b>")
        body.append(f"<span class=dim>started <span data-since='{s['created']}'></span></span>")
        body.append(f"<span class=dim>output <span data-since='{s['activity']}'></span></span>")
        if s["attached"]:
            body.append("<span class='pill on'>attached</span>")
        if s["repo"] not in enabled:
            body.append("<span class='pill off'>not in the scheduled set</span>")
        body.append("</div>")
        body.append(f"<pre class=loop-tail>{esc(s['tail'].rstrip() or '(no output)')}</pre>")
        body.append("</a>")
    body.append("</div>")

    if others:
        names = ", ".join(esc(s["name"]) for s in others)
        body.append(f"<div class='card dim'>Other tmux sessions (not loops): {names}</div>")

    # ---- coming up --------------------------------------------------------
    body.append(
        f'<div class="section-head">{icon("M12 7v5l3 2M12 3a9 9 0 100 18 9 9 0 000-18z", 15)}<h2>Coming up</h2></div>'
    )
    body.append("<div class='card scroll'><table>")
    body.append("<tr><th>repository</th><th>next</th><th>at</th><th>last</th></tr>")
    for t in recurring + oneoffs:
        when = f"<span data-until='{t['next']:.0f}'></span>" if t["next"] else "<span class=dim>-</span>"
        at = time.strftime("%a %H:%M:%S %Z", time.localtime(t["next"])) if t["next"] else "-"
        last = f"<span data-since='{t['last']:.0f}'></span>" if t["last"] else "<span class=dim>never</span>"
        body.append(
            f"<tr><td>{esc(timer_repository(t['unit']))}</td><td>{when}</td>"
            f"<td class=dim>{esc(at)}</td><td class=dim>{last}</td></tr>"
        )
    body.append("</table></div>")

    # ---- fleet (issue #15) -------------------------------------------
    body.append(
        f'<div class="section-head">{icon("M17 2l4 4-4 4M3 11V9a3 3 0 013-3h15M7 22l-4-4 4-4M21 13v2a3 3 0 01-3 3H3", 15)}<h2>Fleet</h2></div>'
    )
    fleet_error = state.get("fleet_error")
    if fleet_error:
        body.append(f"<div class='card dim'>Fleet registry unreachable: {esc(fleet_error)}</div>")
    else:
        fleet_machines = state.get("machines", [])
        if not fleet_machines:
            body.append("<div class='card dim'>No machines registered.</div>")
        else:
            body.append("<div class='card scroll'><table>")
            body.append("<tr><th>machine</th><th>state</th><th>version</th><th>heartbeat</th></tr>")
            for m in fleet_machines:
                pill = {
                    "online": "<span class='pill on'>online</span>",
                    "offline": "<span class='pill off'>offline</span>",
                }.get(m["state"], f"<span class=pill>{esc(m['state'])}</span>")
                version = esc(m.get("version") or "-")
                if m.get("version_mismatch"):
                    version += " <span class=pill>mismatch</span>"
                body.append(
                    f"<tr><td>{esc(m['name'])}</td><td>{pill}</td>"
                    f"<td class=dim>{version}</td><td class=dim>{esc(m.get('heartbeat') or '-')}</td></tr>"
                )
            body.append("</table></div>")

        fleet_claims = state.get("claims", {})
        if not fleet_claims:
            body.append("<div class='card dim'>No claimed issues.</div>")
        else:
            body.append("<div class='card scroll'><table>")
            body.append("<tr><th>issue</th><th>claimed by</th><th>host</th></tr>")
            for target, info in sorted(fleet_claims.items()):
                body.append(
                    f"<tr><td>{esc(target)}</td><td>{esc(info.get('session', '-'))}</td>"
                    f"<td class=dim>{esc(info.get('host', '-'))}</td></tr>"
                )
            body.append("</table></div>")

    # ---- repos --------------------------------------------------------
    body.append(
        f'<div class="section-head">{icon("M6 3v12M18 9a3 3 0 100-6 3 3 0 000 6zM6 21a3 3 0 100-6 3 3 0 000 6zM18 9a9 9 0 01-9 9", 15)}<h2>Repos</h2></div>'
    )
    body.append("<div class='card scroll'><table>")
    body.append(
        "<tr><th>repo</th><th>state</th><th>session</th><th>roadmap</th>"
        "<th>one-off command</th></tr>"
    )
    live = {s["repo"] for s in loops}
    for r in state["repos"]:
        pill = {
            "enabled": "<span class='pill on'>enabled</span>",
            "disabled": "<span class='pill off'>disabled</span>",
            "no-doc": "<span class=pill>no docs/delegation-loop.md</span>",
        }[r["state"]]
        sess = "live" if r["repo"] in live else "<span class=dim>-</span>"
        queue = (
            f"<a href='/roadmap?repo={quote(r['repo'], safe='')}'>open</a>"
            if r["loopable"]
            else "<span class=dim>-</span>"
        )
        once = ""
        if r["state"] == "enabled" and r["loopable"]:
            command = f"loopctl once {r['repo']} now"
            once = (
                f"<span data-once-repo='{esc(r['repo'])}'>"
                f"<code>{esc(command)}</code> "
                "<label>when <input value=now aria-label='one-off loop time'></label> "
                "<button type=button>copy</button> "
                "<span class=dim data-copy-status aria-live=polite></span></span>"
            )
        body.append(
            f"<tr><td>{esc(r['repo'])}</td><td>{pill}</td><td>{sess}</td>"
            f"<td>{queue}</td><td>{once}</td></tr>"
        )
    body.append("</table></div>")

    body.append(
        "<p class=dim style='margin-top:2rem'>Read-only. To change anything - "
        "start a loop, stop one, change the schedule - use "
        "<code>loopctl</code> over SSH. See docs/loopctl-gui-scope.md for "
        "why writes are not here yet. For recent issue activity across "
        "repos, see <a href='/roadmap'>Roadmap</a>.</p>"
    )
    return page("Overview", "".join(body), active="overview")


def time_until_reset(reset_at_ms, now_ms=None) -> str:
    if not isinstance(reset_at_ms, (int, float)):
        return "-"
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    remaining_ms = reset_at_ms - now_ms
    if remaining_ms <= 0:
        return "now"
    if remaining_ms < 60_000:
        return "<1m"
    remaining_minutes = (remaining_ms + 59_999) // 60_000
    days, remainder = divmod(remaining_minutes, 24 * 60)
    hours, minutes = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    return " ".join(parts)


def time_remaining_pct(row: dict, now_ms=None) -> float | None:
    duration = row.get("duration", QuotaDuration.OTHER)
    if not isinstance(duration, QuotaDuration):
        try:
            duration = QuotaDuration(duration)
        except (TypeError, ValueError):
            return None
    reset_at_ms = row.get("resets_at")
    if duration.milliseconds is None or not isinstance(reset_at_ms, (int, float)):
        return None
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    return min(100, max(0, (reset_at_ms - now_ms) / duration.milliseconds * 100))


def quota_duration_label(row: dict) -> str:
    duration = row.get("duration", QuotaDuration.OTHER)
    if isinstance(duration, QuotaDuration) and duration is not QuotaDuration.OTHER:
        return duration.label
    return row.get("label", "Other")


def quota_progress_pct(row: dict) -> float | None:
    used = row.get("used_pct")
    return min(100, max(0, used)) if isinstance(used, (int, float)) else None


def quota_elapsed_pct(row: dict, now_ms=None) -> float | None:
    remaining = time_remaining_pct(row, now_ms)
    return None if remaining is None else 100 - remaining


def quota_status(row: dict, now_ms=None) -> tuple[str, str]:
    used = quota_progress_pct(row)
    elapsed = quota_elapsed_pct(row, now_ms)
    if used is None or elapsed is None:
        return "Status unavailable", ""
    if used > elapsed + 5:
        return "Ahead of pace", "ahead"
    if used < elapsed - 15:
        return "Under pace", "under"
    return "On pace", ""


def render_quota_summary(rows: list[dict], now_ms: int) -> str:
    measured = [row for row in rows if quota_progress_pct(row) is not None]
    ahead = [row for row in measured if quota_status(row, now_ms)[1] == "ahead"]
    most_used = max(measured, key=quota_progress_pct, default=None)
    resets = [
        row for row in rows
        if isinstance(row.get("resets_at"), (int, float))
    ]
    next_reset = min(resets, key=lambda row: row["resets_at"], default=None)
    attention = (
        ", ".join(f"{row['provider']} {quota_duration_label(row)}" for row in ahead)
        if ahead else "None"
    )
    most_value = (
        f"{most_used['provider']} · {quota_duration_label(most_used)}"
        if most_used else "Not available"
    )
    most_note = (
        f"{quota_progress_pct(most_used):.0f}% used" if most_used else "No quota data"
    )
    next_value = (
        f"{next_reset['provider']} · {quota_duration_label(next_reset)}"
        if next_reset else "Not available"
    )
    next_note = (
        f"Resets in {time_until_reset(next_reset['resets_at'], now_ms)}"
        if next_reset else "No reset time"
    )
    items = (
        ("Needs attention", attention, "Usage is ahead of time elapsed" if ahead else "No quota is ahead of pace"),
        ("Most used", most_value, most_note),
        ("Next reset", next_value, next_note),
    )
    return "<div class=quota-summary>" + "".join(
        "<div class=quota-summary-item>"
        f"<div class=quota-summary-key>{esc(key)}</div>"
        f"<div class=quota-summary-value>{esc(value)}</div>"
        f"<div class=quota-summary-note>{esc(note)}</div></div>"
        for key, value, note in items
    ) + "</div>"


def render_quota_row(row: dict, now_ms: int) -> str:
    duration = row.get("duration", QuotaDuration.OTHER)
    duration_value = duration.value if isinstance(duration, QuotaDuration) else str(duration)
    reset_at_ms = row.get("resets_at")
    reset_timestamp = "" if reset_at_ms is None else str(reset_at_ms)
    used = quota_progress_pct(row)
    elapsed = quota_elapsed_pct(row, now_ms)
    status, status_class = quota_status(row, now_ms)
    available_text = "--" if used is None else f"{max(0, 100 - used):.0f}%"
    used_text = "--" if used is None else f"{used:.0f}%"
    used_width = "0%" if used is None else f"{used:.1f}%"
    elapsed_marker = (
        "" if elapsed is None
        else f"<div class=quota-meter-elapsed style='left:{elapsed:.1f}%' "
        "title='time elapsed in window'></div>"
    )
    aria_label = "quota usage unavailable"
    if used is not None and elapsed is not None:
        aria_label = f"{used:.0f}% used, {elapsed:.0f}% of window elapsed"
    elif used is not None:
        aria_label = f"{used:.0f}% used"
    return (
        f"<div class=quota-row data-window-duration='{esc(duration_value)}' "
        f"data-resets-at-ms='{esc(reset_timestamp)}'>"
        "<div><div class=quota-window>"
        f"{esc(quota_duration_label(row))}</div>"
        f"<div class='quota-status {status_class}'>{esc(status)}</div></div>"
        "<div>"
        f"<div class=quota-values><span><strong>{available_text}</strong> available</span>"
        f"<span>{used_text} used</span></div>"
        f"<div class=quota-meter role=img aria-label='{esc(aria_label)}'>"
        f"<div class='quota-meter-used {status_class}' style='width:{used_width}'></div>"
        f"{elapsed_marker}</div></div>"
        f"<div class=quota-reset><div class=quota-reset-left>"
        f"{esc(time_until_reset(reset_at_ms, now_ms))}</div>"
        f"<div class=quota-reset-at>{esc(epoch_ms_to_local(reset_at_ms))}</div></div>"
        "</div>"
    )


def render_usage(connection: dict) -> bytes:
    """Reads every machine's own already-reported usage out of the fleet
    registry (`machines.py`'s `usage_detail`, written by whichever host
    actually has the provider logins) rather than reading providers
    locally -- this page runs on pihome, which has none of its own.
    """
    now_ms = int(time.time() * 1000)
    body = [f'<header><h1>{icon("M5 20V10M12 20V4M19 20v-7")}Usage</h1></header>']
    try:
        records = machines.machines(connection)
    except machines.CoordinatorUnreachable as exc:
        body.append(f"<p class=dim>Fleet registry unreachable: {esc(str(exc))}</p>")
        return page(
            "Agent usage", "".join(body), extra_css="main{max-width:none}", active="usage"
        )

    any_data = False
    for record in sorted(records, key=lambda r: r["name"]):
        usage = record.get("usage_detail") or {}
        quota_rows = usage.get("quota_rows") or []
        token_rows = usage.get("token_rows") or []
        if not quota_rows and not token_rows:
            continue
        any_data = True
        fetched = next((row["generated_at"] for row in quota_rows if "generated_at" in row), None)
        body.append(
            f"<div class=quota-heading><h2>{esc(record['name'])}</h2>"
            f"<span class=dim>Data timestamp: {esc(fetched) if fetched else 'not available'}</span></div>"
        )
        body.append(render_quota_summary(quota_rows, now_ms))
        body.append(
            "<div class=quota-legend>"
            "<span class=quota-legend-item><span class=quota-legend-used></span>used</span>"
            "<span class=quota-legend-item><span class=quota-legend-time></span>"
            "time elapsed in window</span>"
            "<span class=quota-legend-item><span class=quota-legend-ahead></span>"
            "used faster than time</span></div><div class=quota-groups>"
        )
        groups: dict[str, list[dict]] = {}
        for row in quota_rows:
            groups.setdefault(row["provider"], []).append(row)
        for provider, rows in groups.items():
            body.append(
                f"<section class='card quota-group'><div class=quota-group-heading>"
                f"<h3>{esc(provider)}</h3><span class=dim>"
                f"{esc(quota_source_label(provider))}</span></div>"
            )
            for row in rows:
                if "error" in row or "note" in row:
                    message = row.get("error", row.get("note"))
                    body.append(f"<p class=dim>{esc(message)}</p>")
                else:
                    body.append(render_quota_row(row, now_ms))
            body.append("</section>")
        body.append(
            "</div><p class=dim>Bars show quota used; the marker shows time elapsed "
            "in the window. The reset time appears at the right. OpenCode Go uses "
            "omp or Orca's usage API; Claude uses Anthropic's OAuth usage API. "
            "OpenAI uses omp or Codex's latest local snapshot, which only updates "
            "when Codex writes a session event.</p>"
            "<h3>7-day totals</h3>"
            "<p class=dim>Sources are read on that machine. Claude's local cache "
            "reports one combined token total per day, not an input/output split, "
            "and no daily cost -- shown in the input-tokens column with cost as "
            "'not tracked'.</p>"
            "<div class='card scroll'><table><tr><th>provider</th>"
            "<th>input tokens</th><th>output tokens</th><th>cost</th>"
            "<th>period</th><th>source</th><th>last update</th></tr>"
        )
        for row in token_rows:
            if "error" in row:
                body.append(
                    f"<tr><td>{esc(row['provider'])}</td><td colspan=3>"
                    f"{esc(row['error'])}</td><td>last 7 days</td>"
                    f"<td>{esc(row['source'])}</td><td>-</td></tr>"
                )
                continue
            output_tokens = "-" if row["output_tokens"] is None else esc(row["output_tokens"])
            cost = "not tracked" if row["cost"] is None else f"${row['cost']:.2f}"
            body.append(
                f"<tr><td>{esc(row['provider'])}</td>"
                f"<td>{esc(row['input_tokens'])}</td>"
                f"<td>{output_tokens}</td>"
                f"<td>{cost}</td><td>{esc(row['period'])}</td>"
                f"<td>{esc(row['source'])}</td><td>{esc(row['last_update'])}</td></tr>"
            )
        body.append("</table></div>")
    if not any_data:
        body.append("<p class=dim>No usage data reported yet.</p>")
    return page(
        "Agent usage", "".join(body), extra_css="main{max-width:none}", active="usage"
    )


def unavailable_tiers(error: Exception) -> list[dict]:
    return [{
        "error": f"unavailable ({type(error).__name__})",
        "source": MODEL_TIERS_PATH,
    }]


def model_tiers() -> list[dict]:
    """Read MODEL_TIERS_PATH, one row per task category."""
    try:
        with open(MODEL_TIERS_PATH, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError) as error:
        return unavailable_tiers(error)
    if not isinstance(raw, dict):
        return [{"error": "no categories in the file", "source": MODEL_TIERS_PATH}]
    rows = []
    for category, entry in raw.items():
        # Keys starting with "_" are file comments, not categories.
        if category.startswith("_") or not isinstance(entry, dict):
            continue
        tiers = entry.get("tiers")
        rows.append({
            "category": category,
            "source": entry.get("source", "not recorded"),
            "last_verified": entry.get("last_verified", "-"),
            "tiers": tiers if isinstance(tiers, dict) else {},
            "note": entry.get("note", ""),
        })
    return rows


_ALIAS_PREFIX = re.compile(r"^(bmo|local):")


def load_model_snapshot() -> dict | None:
    """Best-effort read of `model_fetch`'s daily snapshot (issue #16). The
    file may not exist yet -- a machine that has never run
    `lupin fetch-models` -- or may be unreadable; either way this returns
    `None` rather than raising, so the page still renders the tier grid
    on its own, same as `model_tiers()` already degrades for a bad
    `model-tiers.json`.
    """
    try:
        with open(model_fetch.SNAPSHOT_FILE, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def snapshot_models(snapshot: dict | None) -> list[dict]:
    """Flatten a snapshot's per-subscription model lists into one list,
    each row tagged with which subscription it came from and whether that
    subscription's fetch was live that day."""
    rows = []
    for subscription, info in (snapshot or {}).get("subscriptions", {}).items():
        if not isinstance(info, dict):
            continue
        for model in info.get("models") or []:
            if not isinstance(model, dict) or not model.get("id"):
                continue
            rows.append({
                "subscription": subscription,
                "id": model["id"],
                "display_name": model.get("display_name") or model["id"],
                "price": model.get("price"),
                "promo": model.get("promo"),
                "live": bool(info.get("live")),
                "stale_reason": info.get("stale_reason") or info.get("error"),
            })
    return rows


def match_live_model(alias: str, models: list[dict]) -> dict | None:
    """Match a model-tiers.json alias (short hand names like "sonnet" or
    "bmo:qwen3.8-flash-next") to a snapshot model (full API IDs like
    "claude-sonnet-4-5-..."). It is not a 1:1 lookup, so this is a
    heuristic, not a resolver:

    - A "bmo:" or "local:" prefix is stripped before matching, but those
      two are never found -- `model_fetch` only covers the claude,
      opencode-go, and codex subscriptions, not bmo's or a local model
      server's own catalog. Those aliases always report "no live data".
    - What remains is looked up as a case-insensitive substring of a
      snapshot model's id or display name, first match wins. Good enough
      to flag "known reachable today" without pretending to be a precise
      ID resolver -- a short alias like "opus" could in principle match
      more than one real id (e.g. a future "opus-mini"), but today's
      catalogs don't have that collision.
    """
    bare = _ALIAS_PREFIX.sub("", alias).strip().lower()
    if not bare:
        return None
    for model in models:
        haystack = f"{model['id']} {model.get('display_name', '')}".lower()
        if bare in haystack:
            return model
    return None


def _format_price(price: dict | None) -> str:
    if not price:
        return "no price data"
    input_price, output_price = price.get("input"), price.get("output")
    if input_price is None and output_price is None:
        return "no price data"
    def fmt(value):
        return f"${value:.2f}" if isinstance(value, (int, float)) else "-"

    return f"{fmt(input_price)} / {fmt(output_price)} per Mtok"


def _live_badge(alias: str, models: list[dict] | None) -> str:
    """A tier pick's live-match note: today's price if `match_live_model`
    finds one, "no live data" if not, or nothing at all when the caller
    (e.g. an existing test of the static chain) passed no snapshot."""
    if models is None:
        return ""
    match = match_live_model(alias, models)
    if not match:
        return "<span class=dim> &middot; no live data</span>"
    return f"<span class=dim> &middot; {esc(_format_price(match['price']))}</span>"


def render_tier_picks(tiers: dict, models: list[dict] | None = None) -> str:
    """One row per tier: its ordered fallback chain, or that it has none.

    `models` is the day's snapshot (issue #16), optional for callers that
    only want the static chain (e.g. existing tests). When given, each
    pick gets a live-match badge from `match_live_model` -- today's price
    if matched, "no live data" if not.
    """
    rows = []
    for tier in MODEL_TIER_ORDER:
        entries = tiers.get(tier)
        picks = [
            pick
            for pick in (entries if isinstance(entries, list) else [])
            if isinstance(pick, dict)
        ]
        chain = "<span class=tier-arrow>&rarr;</span>".join(
            "<span class=tier-pick>"
            f"<span class=tier-model>{esc(pick.get('model', '-'))}</span>"
            f"<span class=dim>{esc(pick.get('effort', '-'))}</span>"
            f"{_live_badge(pick.get('model', ''), models)}"
            "</span>"
            for pick in picks
        ) or "<span class='tier-pick dim'>none</span>"
        rows.append(
            f"<div class=tier-row><span class=tier-name>{esc(tier)}</span>"
            f"<div class=tier-picks>{chain}</div></div>"
        )
    return "".join(rows)


def _snapshot_age(fetched_at: str | None) -> str:
    if not fetched_at:
        return "never pulled"
    try:
        epoch = datetime.fromisoformat(fetched_at).timestamp()
    except ValueError:
        return "never pulled"
    return f"<span data-since='{epoch:.0f}'></span> ago"


def _format_score(entry: dict | None) -> str:
    """"Perf" cell: `entry`'s score with its scale as a hover tooltip, or
    "no data" if there's no entry or the dispatched agent couldn't find
    one (`{"score": null, "reason": ...}`, never a guess -- see
    `benchmark_fetch.py`)."""
    if not entry or entry.get("score") is None:
        return "no data"
    scale = entry.get("scale") or ""
    score = f"{entry['score']:g}"
    return f"<span title='{esc(scale)}'>{score}</span>" if scale else score


def _format_value(entry: dict | None, price: dict | None) -> str:
    """"Value" cell: score per dollar, i.e. `score / blended price`, where
    blended price averages whatever of input/output price is on file.
    This is this module's own ratio, computed from two real numbers (a
    fetched score, a fetched price) -- never a guess, but also not
    something the issue or the mockup ever specified a formula for; a
    judgment call, flagged as such in this change's report. "no data"
    whenever either input is missing, or price is free (nothing to divide
    by that means anything).
    """
    if not entry or entry.get("score") is None or not price:
        return "no data"
    prices = [p for p in (price.get("input"), price.get("output")) if isinstance(p, (int, float))]
    blended = sum(prices) / len(prices) if prices else 0
    if not blended:
        return "no data"
    return f"{entry['score'] / blended:.1f} pts/$"


def render_snapshot_models_table(models: list[dict], benchmark_scores: list[dict] | None = None) -> str:
    """The "All models" table (issue #17): every model each subscription
    actually returned that day, not a hardcoded list. "Perf"/"Value" read
    from `benchmark_fetch`'s fleet-shared snapshot (issue #17's reopen),
    matched by model id the same way price already matches by id --
    "no data" for a model the dispatched agent couldn't find a credible
    score for, never a guess.
    """
    if not models:
        return (
            "<p class=dim>No model snapshot yet. Run <code>lupin fetch-models</code> "
            "or click \"Pull models\" above.</p>"
        )
    rows = []
    for model in sorted(models, key=lambda m: (m["subscription"], m["id"])):
        source = "live" if model["live"] else (esc(model["stale_reason"]) if model["stale_reason"] else "stale")
        promo = esc(model["promo"]) if model.get("promo") else "-"
        score_entry = benchmark_fetch.match_score(model["id"], benchmark_scores or [])
        rows.append(
            "<tr>"
            f"<td>{esc(model['display_name'])}</td>"
            f"<td>{esc(model['subscription'])}</td>"
            f"<td>{esc(_format_price(model['price']))}</td>"
            f"<td>{_format_score(score_entry)}</td>"
            f"<td>{_format_value(score_entry, model['price'])}</td>"
            f"<td>{promo}</td>"
            f"<td class=dim>{source}</td>"
            "</tr>"
        )
    return (
        "<div class='card scroll'><table><tr><th>model</th><th>subscription</th>"
        "<th>price</th><th>perf</th><th>value</th><th>promo</th><th>source</th></tr>"
        f"{''.join(rows)}</table></div>"
    )


def render_model_tiers(*, sent: str | None = None, connection: dict | None = None) -> bytes:
    rows = model_tiers()
    snapshot = load_model_snapshot()
    models = snapshot_models(snapshot)
    benchmark_snapshot = benchmark_fetch.read_snapshot(**(connection or {}))
    benchmark_scores = (benchmark_snapshot or {}).get("scores") or []
    body = [
        f'<header><h1>{icon("M7 7h10v10H7zM9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3")}Models</h1></header>',
    ]
    if sent:
        body.append(f"<p class=dim>{esc(sent)}</p>")
    body.append(
        "<div class='card' style='display:flex;align-items:center;gap:1rem'>"
        f"<span class=dim>Last pulled: {_snapshot_age(snapshot.get('fetched_at') if snapshot else None)}</span>"
        f"<span class=dim>Benchmarks: {_snapshot_age(benchmark_snapshot.get('fetched_at') if benchmark_snapshot else None)}</span>"
        "<span style='flex:1'></span>"
        "<form method=post action='/model-tiers/refresh'>"
        "<button type=submit>Pull models</button></form>"
        "<form method=post action='/model-tiers/refresh-benchmarks'>"
        "<button type=submit>Pull benchmarks</button></form></div>"
    )
    body.extend([
        "<h2>Routing by task category</h2>",
        "<p class=dim>Read from <code>"
        f"{esc(MODEL_TIERS_PATH)}</code>. Each tier is an ordered fallback "
        "chain: the first entry is tried first, then the next. A category "
        "with no entry for a tier escalates to the next tier up. The "
        "&middot; note after each pick is today's live match (issue #16's "
        "fetch), when one is found.</p>",
        "<div class=tier-grid>",
    ])
    for row in rows:
        if "error" in row:
            body.append(
                "<section class='card tier-card'>"
                f"<p class=dim>{esc(row['source'])}</p>"
                f"<p class=dim>{esc(row['error'])}</p></section>"
            )
            continue
        note = row["note"]
        body.append(
            "<section class='card tier-card'>"
            "<div class=tier-heading>"
            f"<h3>{esc(row['category'])}</h3>"
            f"<span class=dim>verified {esc(row['last_verified'])}</span></div>"
            f"<div class=dim>{esc(row['source'])}</div>"
            f"{render_tier_picks(row['tiers'], models)}"
            + (f"<p class='tier-note dim'>{esc(note)}</p>" if note else "")
            + "</section>"
        )
    if not rows:
        body.append("<div class='card dim'>No task categories.</div>")
    body.append("</div>")
    body.extend([
        "<h2>All models</h2>",
        "<p class=dim>Every model <code>lupin fetch-models</code> found reachable "
        "today, across opencode-go, Claude, and Codex. \"Perf\" is "
        "<code>lupin fetch-benchmarks</code>'s score for that model (a "
        "dispatched agent's web research, cached fleet-wide); \"Value\" is "
        "that score divided by price. Either reads \"no data\" when no "
        "credible score was found, never a guess.</p>",
        render_snapshot_models_table(models, benchmark_scores),
    ])
    return page("Model tiers", "".join(body), active="models")


def _heartbeat_epoch(stamp: str | None) -> float | None:
    """`machines()`'s `heartbeat` field is an ISO stamp; the page's
    `data-since` ticker (see `JS` above) wants epoch seconds, same as
    `tmux_sessions()`'s `created`/`activity` fields.
    """
    if not stamp:
        return None
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _slot_controls(slot: str, current_max: int) -> str:
    """Two tiny forms, not one with a number input -- a GET-free "fewer" /
    "more" button pair needs no JS and matches the mockup's control shape.
    Posting the already-computed next value (not a +1/-1 delta) means the
    route has no read-modify-write race to get wrong.
    """
    fewer = max(1, current_max - 1)
    more = current_max + 1
    fewer_disabled = " disabled" if current_max <= 1 else ""
    return (
        "<form method=post action='/machines/slot-max' style='display:inline'>"
        f"<input type=hidden name=slot value='{esc(slot)}'>"
        f"<input type=hidden name=max value='{fewer}'>"
        f"<button type=submit{fewer_disabled}>fewer slots</button></form> "
        "<form method=post action='/machines/slot-max' style='display:inline'>"
        f"<input type=hidden name=slot value='{esc(slot)}'>"
        f"<input type=hidden name=max value='{more}'>"
        "<button type=submit>more slots</button></form>"
    )


def render_machines(records: list[dict], slot_status: dict) -> bytes:
    """The fleet machine list (issue #20).

    `slot_status` is a live `slots_redis.status()` read, not each record's
    own `slots` field -- that field is a snapshot taken at the machine's
    last `join`/`heartbeat` call (`machines.py`'s `_write_record`), so it
    would hide a slot-max change made through this page's own controls
    until the next heartbeat (up to 30s, longer if the heartbeat loop isn't
    running). `slots_redis.status()` has no such lag.

    v1 has exactly one fleet slot (`bmo`), shared by the whole fleet, not
    partitioned per machine -- so the same live numbers are shown, and the
    same controls apply, on every machine's card. Changing a slot's max
    from any one card changes it everywhere.
    """
    body = [f'<header><h1>{icon("M4 4h16v6H4zM4 14h16v6H4zM8 7h.01M8 17h.01")}Machines</h1></header>']
    if not records:
        body.append("<div class='card dim'>No machine has joined the fleet yet.</div>")
    for record in sorted(records, key=lambda r: r["name"]):
        state = record["state"]
        pill_class = {"online": "on", "offline": "off"}.get(state, "")
        body.append("<div class=card>")
        body.append("<div class=row>")
        body.append(f"<span class=big>{esc(record['name'])}</span>")
        body.append(f"<span class='pill {pill_class}'>{esc(state)}</span>")
        if record["version_mismatch"]:
            body.append(f"<span class='pill off'>version mismatch: {esc(record['version'] or '-')}</span>")
        else:
            body.append(f"<span class=dim>{esc(record['version'] or '-')}</span>")
        hb = _heartbeat_epoch(record.get("heartbeat"))
        if hb is not None:
            body.append(f"<span class=dim>heartbeat <span data-since='{hb:.0f}'></span></span>")
        else:
            body.append("<span class=dim>no heartbeat</span>")
        body.append("</div>")
        if slot_status:
            body.append("<table><tr><th>slot</th><th>holders</th><th>max</th><th></th></tr>")
            for slot_name, info in sorted(slot_status.items()):
                used = info.get("holders", 0)
                slot_max = info.get("max")
                controls = _slot_controls(slot_name, slot_max if slot_max is not None else 1)
                body.append(
                    f"<tr><td>{esc(slot_name)}</td><td>{esc(used)}</td>"
                    f"<td>{esc(slot_max) if slot_max is not None else '-'}</td>"
                    f"<td>{controls}</td></tr>"
                )
            body.append("</table>")
        else:
            body.append("<p class=dim>No slot data reported.</p>")
        body.append("</div>")
    return page("Machines", "".join(body), active="machines")


STATE_PILL = {
    "enabled": "<span class='pill on'>enabled</span>",
    "disabled": "<span class=pill>disabled</span>",
    "no-doc": "<span class='pill off'>no doc</span>",
}
REPOS_ICON = "M6 3v12M18 9a3 3 0 100-6 3 3 0 000 6zM6 21a3 3 0 100-6 3 3 0 000 6zM18 9a9 9 0 01-9 9"


def _repo_slot_controls(repo: str, current_max: int) -> str:
    """Same fewer/more button-pair shape `_slot_controls` uses on the
    Machines page, but the form only ever posts `repo` -- `do_repos_slot_max`
    derives the actual slot name (`_repo_slot_name`) itself, so a request
    can only ever change the one slot that belongs to the repo it named,
    never an arbitrary slot string chosen by the browser.
    """
    fewer = max(1, current_max - 1)
    more = current_max + 1
    fewer_disabled = " disabled" if current_max <= 1 else ""
    return (
        "<form method=post action=/repos/slot-max style='display:inline'>"
        f"<input type=hidden name=repo value='{esc(repo)}'>"
        f"<input type=hidden name=max value='{fewer}'>"
        f"<button type=submit{fewer_disabled} aria-label='Lower max'>&minus;</button></form> "
        f"<span class=mono>{current_max}</span> "
        "<form method=post action=/repos/slot-max style='display:inline'>"
        f"<input type=hidden name=repo value='{esc(repo)}'>"
        f"<input type=hidden name=max value='{more}'>"
        "<button type=submit aria-label='Raise max'>+</button></form>"
    )


def _render_add_panel(tab: str, repos: list[dict]) -> str:
    """The "Add repo" card. "Existing repo" (issue #23) is fully wired:
    a repo with a doc gets an "add to schedule" button, one without gets
    "generate docs and add". "Clone from Git" and "Create new" are shown
    but inert -- see this issue's report for why: both are real
    filesystem/git-network operations (an arbitrary clone URL, process
    spawning, file generation) with a bigger security surface than
    anything else on this page, and landing them safely didn't fit this
    pass.
    """
    tab = "new" if tab == "new" else "existing"

    def tab_link(value: str, label: str) -> str:
        active = value == tab
        text = f"<b>{esc(label)}</b>" if active else esc(label)
        return f"<a href='/repos?add={value}'>{text}</a>"

    tabs = f"<div class=row style='margin-bottom:.6rem'>{tab_link('existing', 'Existing repo')} {tab_link('new', 'Create new')}</div>"

    if tab == "new":
        body = (
            "<input type=text disabled placeholder='repo name, e.g. payments-sync' "
            "style='display:block;width:100%;max-width:420px;margin-bottom:.5rem'>"
            "<textarea disabled placeholder='message for the loop (optional)' "
            "style='display:block;width:100%;max-width:420px;height:80px;margin-bottom:.5rem'></textarea>"
            "<button type=button disabled>Create repo</button>"
            "<p class=dim>Not implemented yet -- scaffolding a new repo and generating its "
            "delegation doc is a bigger surface than this pass takes on.</p>"
        )
        return f"<div class=card>{tabs}{body}</div>"

    clone = (
        "<div style='font-size:12px;color:var(--ink3);margin-bottom:6px'>Clone from Git</div>"
        "<input type=text disabled placeholder='git@github.com:owner/name.git or https URL' "
        "style='width:100%;max-width:420px'> "
        "<button type=button disabled>Clone and add</button>"
        "<p class=dim>Not implemented yet.</p>"
    )
    rows = []
    for r in repos:
        if r["state"] == "enabled":
            continue
        repo = r["repo"]
        if r["loopable"]:
            rows.append(
                "<div class=row style='justify-content:space-between;border-top:1px solid var(--line2);padding:.4rem 0'>"
                f"<span><b>{esc(repo)}</b> <span class=dim>has {esc(LOOP_DOC)}</span></span>"
                "<form method=post action=/repos/add style='display:inline'>"
                f"<input type=hidden name=repo value='{esc(repo)}'>"
                "<button type=submit>Add to schedule</button></form></div>"
            )
        else:
            rows.append(
                "<div class=row style='justify-content:space-between;border-top:1px solid var(--line2);padding:.4rem 0'>"
                f"<span><b>{esc(repo)}</b> <span class=dim>missing {esc(LOOP_DOC)}</span></span>"
                "<form method=post action=/repos/generate-docs style='display:inline'>"
                f"<input type=hidden name=repo value='{esc(repo)}'>"
                "<button type=submit>Generate docs and add</button></form></div>"
            )
    picker = "".join(rows) or "<p class=dim>Every repo under /code is already on the schedule.</p>"
    body = (
        f"{clone}"
        f"<div style='font-size:12px;color:var(--ink3);margin:14px 0 6px'>Or pick a directory in /code</div>"
        f"{picker}"
    )
    return f"<div class=card>{tabs}{body}</div>"


def _render_doc_panel(repo: str, text: str, edit: bool) -> str:
    view_href = f"/repos?doc={quote(repo, safe='')}"
    edit_href = f"/repos?doc={quote(repo, safe='')}&edit=1"
    if edit:
        inner = (
            "<form method=post action=/repos/doc/save>"
            f"<input type=hidden name=repo value='{esc(repo)}'>"
            f"<textarea name=text spellcheck=false "
            "style='width:100%;height:230px;font-family:monospace;box-sizing:border-box'>"
            f"{esc(text)}</textarea>"
            "<div class=row style='margin-top:.4rem'><button type=submit>Save</button>"
            f"<a href='{view_href}'>cancel</a></div></form>"
        )
    else:
        inner = f"<pre style='max-height:230px'>{esc(text)}</pre>"
    view_label = "View" if edit else "<b>View</b>"
    edit_label = "<b>Edit</b>" if edit else "Edit"
    return (
        "<div class=card>"
        f"<div class=row><b class=mono>{esc(repo)}/{esc(LOOP_DOC)}</b><span class=sp></span>"
        f"<a href='{view_href}'>{view_label}</a> <a href='{edit_href}'>{edit_label}</a> "
        "<a href='/repos'>Close</a></div>"
        f"{inner}"
        "<p class=dim>Loops pick up changes at the start of their next run.</p>"
        "</div>"
    )


def _render_schedule_panel(repo: str) -> str:
    """A one-off run, via `loopctl once` -- a real command, already used
    the same way by the Overview page's own "one-off command" widget. Not
    `lupin once`: `lupin`'s own `cli.py` has no such subcommand (see this
    issue's report), but `loopctl once <when> [repo...]` already exists and
    does the same job, so this dispatches it for real instead of inventing
    a fake success path -- see `do_repos_schedule`'s docstring for why it's
    local-machine only.
    """
    return (
        "<div class=card>"
        "<form method=post action=/repos/schedule class=row>"
        f"<b>Schedule a one-off run for {esc(repo)}</b>"
        f"<input type=hidden name=repo value='{esc(repo)}'>"
        "<input type=text name=when placeholder='15:00, +2h, tomorrow 09:00' required>"
        "<button type=submit>Schedule</button>"
        "<a href='/repos'>Cancel</a>"
        "</form>"
        "<p class=dim>Runs once, in addition to the recurring schedule. Same as "
        f"<code>loopctl once &lt;when&gt; {esc(repo)}</code>.</p>"
        "</div>"
    )


def _render_remove_panel(repo: str) -> str:
    return (
        "<div class=card style='border-color:var(--warnline);background:var(--warnbg)'>"
        "<form method=post action=/repos/remove class=row>"
        f"<div><b>Remove {esc(repo)} from the schedule?</b>"
        "<div class=dim>Stops scheduled and one-off runs. A live session keeps "
        "running. Files in /code are untouched. Type the repo name to confirm.</div></div>"
        f"<input type=hidden name=repo value='{esc(repo)}'>"
        f"<input type=text name=confirm placeholder='{esc(repo)}' required>"
        "<button type=submit>Remove repo</button>"
        "<a href='/repos'>Cancel</a>"
        "</form></div>"
    )


def _render_repo_table(repos: list[dict], local_host: str) -> str:
    rows = [
        "<tr><th>repo</th><th>state</th><th>loops &middot; max</th>"
        "<th>machine</th><th>roadmap</th><th>actions</th></tr>"
    ]
    for r in repos:
        repo = r["repo"]
        pill = STATE_PILL[r["state"]]
        if not r["loopable"]:
            rows.append(
                f"<tr><td><b>{esc(repo)}</b></td><td>{pill}</td>"
                "<td class=dim>-</td><td class=dim>-</td><td class=dim>-</td>"
                '<td class=dim>generate docs via "Add repo" to enable</td></tr>'
            )
            continue
        dot = "ok" if r["running"] else "idle"
        sess_label = "running" if r["running"] else "stopped"
        current_max = r["max"] if r["max"] is not None else 1
        stepper = _repo_slot_controls(repo, current_max)
        roadmap_link = f"<a href='/roadmap?repo={quote(repo, safe='')}'>Roadmap</a>"
        doc_link = f"<a href='/repos?doc={quote(repo, safe='')}'>doc</a>"
        schedule_link = f"<a href='/repos?schedule={quote(repo, safe='')}'>Schedule&hellip;</a>"
        if r["state"] == "enabled":
            # Posts straight to the existing, tested /schedule/run route --
            # no new dispatch code. place=local_host is the only option
            # here (unlike Schedule's own "Run now" card, a repo row has no
            # machine picker in the mockup either).
            run_form = (
                "<form method=post action=/schedule/run style='display:inline'>"
                f"<input type=hidden name=repo value='{esc(repo)}'>"
                "<input type=hidden name=cnt value=1>"
                f"<input type=hidden name=place value='{esc(local_host)}'>"
                "<button type=submit>Run now</button></form>"
            )
            remove_link = f"<a href='/repos?remove={quote(repo, safe='')}'>Remove</a>"
            actions = f"{run_form} {schedule_link} &middot; {doc_link} &middot; {remove_link}"
        else:
            add_btn = (
                "<form method=post action=/repos/add style='display:inline'>"
                f"<input type=hidden name=repo value='{esc(repo)}'>"
                "<button type=submit>Add to schedule</button></form>"
            )
            actions = f"{add_btn} {schedule_link} &middot; {doc_link}"
        rows.append(
            f"<tr><td><b>{esc(repo)}</b></td><td>{pill}</td>"
            f"<td><span class='dot {dot}'></span> {esc(sess_label)} {stepper}</td>"
            f"<td>{esc(r['machine'])}</td><td>{roadmap_link}</td><td>{actions}</td></tr>"
        )
    return "<div class=card><table>" + "".join(rows) + "</table></div>"


def render_repos(
    data: dict,
    *,
    add: str | None = None,
    doc_repo: str | None = None,
    doc_text: str | None = None,
    doc_edit: bool = False,
    schedule_repo: str | None = None,
    remove_repo: str | None = None,
    sent: str | None = None,
) -> bytes:
    """The Repos page (issue #23): add/remove a repo from the local
    schedule, view or edit its delegation doc, raise/lower its loop
    concurrency cap, and run or one-off-schedule a loop.

    `data` is `gather_repos()`'s output. Query-string flags (`add`, `doc`,
    `schedule`, `remove`) open the matching panel -- the same convention
    `/roadmap`'s `state=closed` and `/loops`'s `group=`/`repo=` already use:
    a plain link, no client-side state.

    The mockup's filter pills ("All/Enabled/Disabled/No doc") are not
    built here -- they only narrow which rows of an already-read table are
    shown, no state to mutate, and this page's scope is already large
    (add/remove/doc/run/schedule/slot-max). Left out, not silently cut.
    """
    repos = data["repos"]
    local_host = data["local_host"]
    body = [f'<header><h1>{icon(REPOS_ICON)}Repos</h1></header>']

    if sent:
        body.append(f"<div class=card><span class='pill on'>{esc(sent)}</span></div>")

    add_href = "/repos" if add else "/repos?add=existing"
    add_label = "Close" if add else "Add repo"
    body.append(f"<div class=row style='margin-bottom:.6rem'><span class=sp></span><a href='{add_href}'>{esc(add_label)}</a></div>")

    if add:
        body.append(_render_add_panel(add, repos))
    if doc_repo:
        body.append(_render_doc_panel(doc_repo, doc_text or "", doc_edit))
    if schedule_repo:
        body.append(_render_schedule_panel(schedule_repo))
    if remove_repo:
        body.append(_render_remove_panel(remove_repo))

    if not repos:
        body.append(
            "<div class=card><p class=dim>No repos yet. Add one to start scheduling loops.</p>"
            "<a href='/repos?add=existing'>Add your first repo</a></div>"
        )
    else:
        body.append(_render_repo_table(repos, local_host))

    fleet_error = data.get("fleet_error")
    if fleet_error:
        body.append(
            f"<p class=dim>Fleet registry unreachable: {esc(fleet_error)} "
            "(loop status and concurrency caps below are limited to this machine).</p>"
        )

    return page("Repos", "".join(body), active="repos")


TIMER_WINDOW_S = 4 * 3600  # the mockup's 4-hour timeline strip


def render_schedule(data: dict, *, sent: str | None = None) -> bytes:
    """The Schedule page (issue #22): the `delegation-loop.timer` status
    card, a table of every timer (recurring + one-off), and a "Run now"
    form that dispatches `loop.run` on demand.

    `data` is `gather_schedule()`'s output -- same read-then-render split
    the rest of this module uses.
    """
    now = data["now"]
    enabled = data["enabled"]
    timers_list = data["timers"]
    recurring = [t for t in timers_list if t["unit"] == "delegation-loop.timer"]
    fleet_machines = data.get("machines", [])
    local_host = data["local_host"]

    body = [f'<header><h1>{icon("M4 5h16v16H4zM4 10h16M8 3v4M16 3v4")}Schedule</h1></header>']

    if sent:
        body.append(f"<div class=card><span class='pill on'>{esc(sent)}</span></div>")

    # ---- recurring timer card ---------------------------------------------
    active = data["timer_active"]
    recurring_next = recurring[0]["next"] if recurring and recurring[0]["next"] else None
    status_text = (
        f"Running - next at {esc(time.strftime('%a %H:%M', time.localtime(recurring_next)))}"
        if active and recurring_next
        else "Running - no upcoming run scheduled" if active else "Stopped - no automatic runs"
    )
    toggle_action, toggle_label = ("stop", "Stop") if active else ("start", "Start")

    due = sorted(
        (t for t in timers_list if t["next"] is not None and t["next"] - now <= TIMER_WINDOW_S),
        key=lambda t: t["next"],
    )
    if due:
        marks = "".join(
            "<li>"
            f"{esc(timer_repository(t['unit']))} "
            f"<span class=dim>({'recurring' if t['unit'] == 'delegation-loop.timer' else 'one-off'})</span> "
            f"<span class=mono data-until='{t['next']:.0f}'></span> "
            f"<span class=dim>{esc(time.strftime('%H:%M', time.localtime(t['next'])))}</span></li>"
            for t in due
        )
        marks_html = f"<ul style='margin:0;padding-left:1.1rem'>{marks}</ul>"
    else:
        marks_html = "<p class=dim style='margin:0'>No runs in the next 4 hours.</p>"

    overall_next = next((t["next"] for t in timers_list if t["next"] is not None), None)
    next_block = (
        f"<div class=dim>Next</div><div class='big mono' data-until='{overall_next:.0f}'></div>"
        f"<div class=dim>{esc(time.strftime('%a %H:%M', time.localtime(overall_next)))}</div>"
        if overall_next
        else "<div class=dim>Next</div><div class=big>None scheduled</div>"
    )

    body.append(
        "<div class=card><div class=row style='justify-content:space-between;align-items:flex-start'>"
        "<div><div class=dim>Recurring timer</div><div class=big>delegation-loop.timer</div>"
        f"<div class=dim>{status_text}</div>"
        f"<form method=post action=/schedule/timer style='margin-top:.5rem'>"
        f"<input type=hidden name=action value={toggle_action}>"
        f"<button type=submit>{toggle_label}</button></form></div>"
        f"<div style='flex:1;min-width:220px'><div class=dim style='margin-bottom:.3rem'>Next 4 hours</div>{marks_html}</div>"
        f"<div style='text-align:right'>{next_block}</div>"
        "</div></div>"
    )

    # ---- timer table --------------------------------------------------
    body.append("<div class=card><table>")
    body.append("<tr><th>repository</th><th>loops</th><th>next</th><th>at</th><th>last run</th></tr>")
    if not timers_list:
        body.append("<tr><td colspan=5 class=dim>No timer found.</td></tr>")
    for t in timers_list:
        repo_label = timer_repository(t["unit"])
        kind = "recurring" if t["unit"] == "delegation-loop.timer" else "one-off"
        loops = _timer_loop_count(t["unit"], repo_label, enabled)
        next_cell = (
            f"<span class=mono data-until='{t['next']:.0f}'></span>" if t["next"] else "<span class=dim>-</span>"
        )
        at_cell = (
            esc(time.strftime("%a %H:%M", time.localtime(t["next"]))) if t["next"] else "<span class=dim>-</span>"
        )
        last_cell = (
            f"<span data-since='{t['last']:.0f}'></span>" if t["last"] else "<span class=dim>never</span>"
        )
        body.append(
            f"<tr><td>{esc(repo_label)} <span class=pill>{esc(kind)}</span></td>"
            f"<td>{esc(loops)}</td><td>{next_cell}</td><td>{at_cell}</td><td>{last_cell}</td></tr>"
        )
    body.append("</table></div>")

    # ---- run now --------------------------------------------------------
    repo_options = "".join(f"<option value='{esc(r)}'>{esc(r)}</option>" for r in sorted(enabled))
    cnt_options = "".join(f"<option value={n}>{n} loop{'s' if n != 1 else ''}</option>" for n in range(1, 5))
    place_options = ["<option value=spread>spread out</option>", "<option value=any>any machine</option>"]
    # The registry's own record for `local_host` wins when there is one (a
    # draining local machine must show as draining here too, not as a
    # synthetic always-available entry) -- same rule `_machine_available`
    # enforces on the POST side.
    by_name = {m["name"]: m for m in fleet_machines}
    by_name.setdefault(local_host, {"name": local_host, "state": "online"})
    for m in sorted(by_name.values(), key=lambda m: m["name"]):
        if m["state"] == "offline":
            continue
        disabled = " disabled" if m["state"] == "draining" else ""
        label = f"{m['name']} (draining)" if m["state"] == "draining" else m["name"]
        place_options.append(f"<option value='{esc(m['name'])}'{disabled}>{esc(label)}</option>")

    fleet_error = data.get("fleet_error")
    error_note = (
        f"<p class=dim>Fleet registry unreachable: {esc(fleet_error)} "
        "(placement choices below are limited to this machine).</p>"
        if fleet_error
        else ""
    )

    body.append(
        '<div class=card><div class=row style="margin-bottom:.4rem">'
        "<b>Run now</b><span class=dim>Starts loops on free machines. "
        "The note is kept with the request but is not yet passed into the loop "
        "-- loopctl has no way to receive one today.</span></div>"
        f"{error_note}"
        "<form method=post action=/schedule/run class=row>"
        f"<select name=repo><option value=all>all enabled repos</option>{repo_options}</select>"
        f"<select name=cnt>{cnt_options}</select>"
        f"<select name=place>{''.join(place_options)}</select>"
        "<input type=text name=note placeholder='note for the loop (optional)' style='flex:1;min-width:180px'>"
        "<button type=submit>Start run</button>"
        "</form></div>"
    )

    body.append(
        "<p class=dim>To schedule a one-off run: "
        "<code>lupin once &lt;when&gt; [repo...] [--note TEXT] [--platform P]</code> "
        "-- this command does not exist yet, this line only names the intended "
        "shape.</p>"
    )

    return page("Schedule", "".join(body), active="schedule")


LOOP_STATUS_DOT = {"running": "ok", "remote": "ok", "stopped": "idle"}
LOOP_STATUS_LABEL = {"running": "running", "remote": "running elsewhere", "stopped": "stopped"}


def _loops_url(repo: str, group: str, lines: int) -> str:
    return f"/loops?repo={quote(repo, safe='')}&group={esc(group)}&lines={lines}"


def render_loops(
    data: dict, *, group: str, selected_repo: str, selected_tail: str | None, lines: int
) -> bytes:
    """The Loops page (issue #21): a sidebar grouped by repo or by machine,
    and a tail pane for whichever loop is selected.

    `data` is `gather_loops()`'s output, with `selected_tail` fetched
    separately by the caller (same split `gather()`/`render_dashboard()`
    already use: this function renders a already-read state, it doesn't go
    read anything itself).
    """
    entries = data["entries"]
    local_host = data["local_host"]
    fleet_machines = data.get("machines", [])
    selected = next((e for e in entries if e["repo"] == selected_repo), None)

    def row(entry: dict) -> str:
        sel = " sel" if entry["repo"] == selected_repo else ""
        dot = LOOP_STATUS_DOT.get(entry["status"], "idle")
        label = LOOP_STATUS_LABEL.get(entry["status"], entry["status"])
        return (
            f"<a class='loops-row{sel}' href='{_loops_url(entry['repo'], group, lines)}'>"
            f"<span class='dot {dot}'></span> {esc(entry['repo'])} "
            f"<span class=dim>{esc(label)}</span></a>"
        )

    side = [
        "<div class=row style='margin-bottom:.4rem'>",
        f"<a href='/loops?group=repo&repo={quote(selected_repo, safe='')}&lines={lines}'>by repo</a>",
        " &middot; ",
        f"<a href='/loops?group=machine&repo={quote(selected_repo, safe='')}&lines={lines}'>by machine</a>",
        "</div>",
    ]
    if not entries:
        side.append("<p class=dim>No loopable repos found.</p>")
    elif group == "machine":
        by_machine: dict[str, list[dict]] = {}
        for entry in entries:
            by_machine.setdefault(entry["machine"], []).append(entry)
        # A machine with a live loop isn't always in the registry (it may
        # not have `join`ed yet) -- include it anyway, or a claim-derived
        # "remote" entry would silently vanish from this grouping while
        # still showing up under "by repo".
        known = sorted({m["name"] for m in fleet_machines} | {local_host} | {e["machine"] for e in entries})
        for name in known:
            label = "this machine" if name == local_host else name
            side.append(f"<div class=loops-group>{esc(label)}</div>")
            rows = by_machine.get(name, [])
            if not rows:
                side.append("<p class=dim style='margin:.2rem 0 0 .8rem'>no loop visible from here</p>")
            side.extend(row(entry) for entry in rows)
    else:
        side.extend(row(entry) for entry in entries)

    main = ["<div class=card>"]
    if selected is None:
        main.append("<p class=dim>Select a loop.</p>")
    else:
        main.append(
            "<div class=row>"
            f"<span class=big>{esc(selected['repo'])}</span>"
            f"<span class=dim>on {esc(selected['machine'])}</span>"
            "</div>"
        )
        if selected["status"] == "running":
            # Preset lengths, same three the mockup's line-count picker
            # offers, plus whatever `lines=` the caller already asked for
            # (a direct link with an arbitrary count still works).
            presets = sorted({25, 400, 2000, lines})
            picker = " ".join(
                f"<a href='{_loops_url(selected_repo, group, n)}'>"
                f"{'<b>' if n == lines else ''}{n} lines{'</b>' if n == lines else ''}</a>"
                for n in presets
            )
            main.append(
                f"<div style='margin:.6rem 0'>{picker} &middot; "
                f"<a href='{_loops_url(selected_repo, group, lines)}&fullscreen=1'>fullscreen</a></div>"
            )
            main.append(f"<pre style='max-height:none'>{esc((selected_tail or '').rstrip() or '(no output)')}</pre>")
        elif selected["status"] == "remote":
            main.append(
                "<p class=dim>This loop looks like it is running on "
                f"{esc(selected['machine'])}. This dashboard cannot read "
                "another machine's terminal yet, so there is no tail to "
                "show here.</p>"
            )
        else:
            main.append("<p class=dim>Not running.</p>")

        main.append("<div class=row style='margin-top:1rem'>")
        if selected["status"] in ("running", "remote"):
            main.append(
                "<form method=post action=/loops/close style='display:inline'>"
                f"<input type=hidden name=repo value='{esc(selected_repo)}'>"
                f"<input type=hidden name=machine value='{esc(selected['machine'])}'>"
                "<select name=scope>"
                "<option value=repo>this repo</option>"
                "<option value=all>every loop on this machine</option>"
                "</select> "
                "<button type=submit>close gracefully</button></form>"
            )
        if selected["status"] == "stopped":
            main.append(
                "<form method=post action=/loops/start style='display:inline'>"
                f"<input type=hidden name=repo value='{esc(selected_repo)}'>"
                f"<input type=hidden name=machine value='{esc(selected['machine'])}'>"
                "<button type=submit>start again</button></form>"
            )
        main.append("</div>")
    main.append("</div>")

    fleet_error = data.get("fleet_error")
    error_note = (
        f"<p class=dim>Fleet registry unreachable: {esc(fleet_error)}</p>" if fleet_error else ""
    )
    body = (
        f'<header><h1>{icon("M17 2l4 4-4 4M3 11V9a3 3 0 013-3h15M7 22l-4-4 4-4M21 13v2a3 3 0 01-3 3H3")}Loops</h1></header>'
        f"{error_note}"
        "<div class=loops-shell>"
        f"<div class='card loops-side'>{''.join(side)}</div>"
        f"<div class=loops-main>{''.join(main)}</div>"
        "</div>"
    )
    return page("Loops", body, active="loops")


def render_loop_fullscreen(entry: dict, tail: str | None, lines: int, group: str) -> bytes:
    """A bare tail view: no sidebar, no nav, no topbar -- the mockup's
    fullscreen mode. A plain link takes the reader back to the normal page;
    nothing here needs JavaScript.
    """
    exit_url = _loops_url(entry["repo"], group, lines)
    tail_text = esc((tail or "").rstrip() or "(no output)")
    presets = sorted({25, 400, 2000, lines})
    picker = " ".join(
        f"<a href='{_loops_url(entry['repo'], group, n)}&fullscreen=1' style='color:inherit'>"
        f"{'<b>' if n == lines else ''}{n} lines{'</b>' if n == lines else ''}</a>"
        for n in presets
    )
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        f"{THEME_BOOTSTRAP}"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{esc(entry['repo'])} - fullscreen</title>"
        f"<style>{CSS}body{{padding:0}}.full{{padding:1rem}}"
        "pre{max-height:none;height:calc(100vh - 5rem)}</style></head>"
        f"<body><div class=full><header><h1>{esc(entry['repo'])}</h1>"
        f"<span class=dim>{esc(entry['machine'])}</span><span class=sp></span>"
        f"<span style='font-size:13px'>{picker}</span>"
        f"<a href='{exit_url}'>exit fullscreen</a></header>"
        f"<pre>{tail_text}</pre></div></body></html>"
    ).encode("utf-8")


def render_error(msg: str) -> bytes:
    return page(
        "error",
        "<header><h1>Rejected</h1><span class=sp></span>"
        "<a href='/'>back</a></header>"
        f"<div class=card><p class=err>{esc(msg)}</p></div>",
        active="overview",
    )


# --------------------------------------------------------------------------
# server
# --------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "lupin"
    sys_version = ""
    peek_lines = 25
    allowed_hosts: set = set()
    # Named `fleet_connection`, not `connection` -- `socketserver`'s own
    # `BaseRequestHandler` already sets `self.connection` to the live
    # client socket, which would otherwise shadow this class attribute on
    # every real request (a bug caught by the real-HTTP tests, not the
    # mocked-Handler ones, since those never call setup()). Used by issue
    # #15's fleet data, issue #19's quest POST routes, and issue #20's
    # Machines page alike.
    fleet_connection: dict = {}
    # Needed only to close/restart a loop on another fleet machine (issue
    # #21) -- a local-machine close/restart never signs anything. See
    # commands.py's docstring for what this key is and why it's per target.
    # Known, flagged gap (issue #2's posted architecture-plan comment,
    # "Signing-key scheme"): one shared key signs for every target today,
    # so once any machine can command any other, a single compromised
    # host could forge a command fleet-wide. Moving to per-machine keys is
    # Grace's call to make, not a default to pick here.
    cmd_signing_key: str | None = None

    def reply(self, body: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # Images load from the same-origin attachment route.
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; "
            "script-src 'unsafe-inline'; img-src 'self'; base-uri 'none'",
        )
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def reply_json(self, obj) -> None:
        body = json.dumps(obj, indent=2).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, location: str) -> None:
        """303: the browser re-GETs `location` instead of re-submitting
        the form that landed here (standard post/redirect/get)."""
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def host_ok(self) -> bool:
        """Block DNS rebinding: only the names we bound to are accepted."""
        host = (self.headers.get("Host") or "").strip().lower()
        return host in self.allowed_hosts

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def do_GET(self):  # noqa: N802
        if not self.host_ok():
            self.reply(render_error("bad Host header"), 421)
            return
        url = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}

        if url.path == "/favicon.ico":
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(FAVICON)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(FAVICON)
        elif url.path == "/":
            self.reply(render_dashboard(gather(self.peek_lines, self.fleet_connection)))
        elif url.path == "/roadmap":
            # roadmap.py owns this page's content; it only knows the shell's
            # 4-argument page_fn contract, so pin the "Roadmap" nav entry
            # here rather than changing that contract.
            def roadmap_page(title, body, extra_css="", extra_js=""):
                return page(title, body, extra_css, extra_js, active="roadmap")

            repos = roadmap.repository_names(code_repos())
            selected = query.get("repo", "").strip()
            if selected and selected not in repos:
                self.reply(render_error("unknown or non-loopable repository"), 404)
                return
            state = "closed" if query.get("state") == "closed" else "open"
            if query.get("view") == "list" and state == "open":
                models = {
                    repo: roadmap.cached_model(repo, os.path.join(CODE_DIR, repo))
                    for repo in repos
                }
                body = roadmap.render_list_page(repos, models, roadmap_page, query)
            elif state == "closed":
                issues_by_repo = {
                    name: roadmap.cached_github(
                        name, os.path.join(CODE_DIR, name), "closed"
                    )
                    for name in ([selected] if selected else repos)
                }
                body = roadmap.render_completed_page(
                    selected, repos, issues_by_repo, roadmap_page
                )
            elif selected:
                model = roadmap.cached_model(selected, os.path.join(CODE_DIR, selected))
                quest_state = self.quest_state(query.get("quest", "").strip())
                body = roadmap.render_page(selected, repos, model, roadmap_page, quest_state)
            else:
                models = {
                    repo: roadmap.cached_combined_model(repo, os.path.join(CODE_DIR, repo))
                    for repo in repos
                }
                body = roadmap.render_combined_page(repos, models, roadmap_page)
            self.reply(body)
        elif url.path == "/usage":
            self.reply(render_usage(self.fleet_connection))
        elif url.path == "/model-tiers":
            self.reply(render_model_tiers(sent=query.get("sent"), connection=self.fleet_connection))
        elif url.path == "/machines":
            try:
                records = machines.machines(self.fleet_connection)
                slot_status = slots_redis.status(**self.fleet_connection)
            except machines.CoordinatorUnreachable:
                self.reply(render_error("cannot reach the machine registry"), 502)
                return
            self.reply(render_machines(records, slot_status))
        elif url.path == "/api/state":
            self.reply_json(gather(self.peek_lines, self.fleet_connection))
        elif url.path == "/repos":
            self.do_repos(query)
        elif url.path == "/loops":
            self.do_loops(query)
        elif url.path == "/schedule":
            self.reply(render_schedule(gather_schedule(self.fleet_connection), sent=query.get("sent")))
        elif url.path == "/peek":
            self.do_peek(query)
        elif url.path == "/image":
            self.do_image(query)
        elif url.path == "/healthz":
            self.reply(b"ok")
        else:
            self.reply(render_error("no such page"), 404)

    def do_image(self, query: dict) -> None:
        attachment_id = query.get("id", "")
        if not ATTACHMENT_ID.fullmatch(attachment_id):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        image = github_attachment(attachment_id)
        if image is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        data, content_type = image
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=300")
        self.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _repo_loopable(self, repo: str) -> bool:
        """A repo name that is both well-formed and actually has a
        delegation doc on this machine -- the one check every `/repos/*`
        write route that touches a repo starts with."""
        return bool(repo) and _valid_repo_name(repo) and os.path.isfile(os.path.join(CODE_DIR, repo, LOOP_DOC))

    def do_repos(self, query: dict) -> None:
        doc_repo = (query.get("doc") or "").strip()
        doc_text = None
        if doc_repo:
            if not self._repo_loopable(doc_repo):
                self.reply(render_error("unknown or non-loopable repository"), 404)
                return
            try:
                with open(os.path.join(CODE_DIR, doc_repo, LOOP_DOC), encoding="utf-8") as fh:
                    doc_text = fh.read()
            except OSError as exc:
                self.reply(render_error(f"could not read doc: {exc}"), 500)
                return

        data = gather_repos(self.fleet_connection)
        self.reply(
            render_repos(
                data,
                add=query.get("add"),
                doc_repo=doc_repo or None,
                doc_text=doc_text,
                doc_edit=query.get("edit") == "1",
                schedule_repo=(query.get("schedule") or "").strip() or None,
                remove_repo=(query.get("remove") or "").strip() or None,
                sent=query.get("sent"),
            )
        )

    def do_repos_add(self, form: dict) -> None:
        repo = form.get("repo", [""])[0].strip()
        if not self._repo_loopable(repo):
            self.reply(render_error("unknown or non-loopable repository"), 400)
            return
        enabled = enabled_repos()
        if repo not in enabled:
            write_enabled_repos(sorted(set(enabled) | {repo}))
        self.redirect(f"/repos?sent={quote(f'{repo} added to the schedule', safe='')}")

    def do_repos_generate_docs(self, form: dict) -> None:
        repo = form.get("repo", [""])[0].strip()
        if not repo or not _valid_repo_name(repo) or not os.path.isdir(os.path.join(CODE_DIR, repo)):
            self.reply(render_error("unknown repository"), 400)
            return
        doc_path = os.path.join(CODE_DIR, repo, LOOP_DOC)
        if os.path.exists(doc_path):
            self.reply(render_error(f"{repo} already has {LOOP_DOC}"), 400)
            return
        try:
            os.makedirs(os.path.dirname(doc_path), exist_ok=True)
            with open(doc_path, "w", encoding="utf-8") as fh:
                fh.write(_delegation_doc_template(repo))
        except OSError as exc:
            self.reply(render_error(f"could not write doc: {exc}"), 500)
            return
        write_enabled_repos(sorted(set(enabled_repos()) | {repo}))
        self.redirect(f"/repos?sent={quote(f'generated docs and added {repo}', safe='')}")

    def do_repos_remove(self, form: dict) -> None:
        repo = form.get("repo", [""])[0].strip()
        confirm = form.get("confirm", [""])[0].strip()
        enabled = enabled_repos()
        if not repo or repo not in enabled:
            self.reply(render_error("unknown or not-scheduled repository"), 400)
            return
        if confirm != repo:
            self.reply(render_error("type the repo name to confirm removal"), 400)
            return
        write_enabled_repos([r for r in enabled if r != repo])
        self.redirect(f"/repos?sent={quote(f'{repo} removed from the schedule', safe='')}")

    def do_repos_doc_save(self, form: dict) -> None:
        repo = form.get("repo", [""])[0].strip()
        text = form.get("text", [""])[0]
        if not self._repo_loopable(repo):
            self.reply(render_error("unknown or non-loopable repository"), 400)
            return
        if len(text) > 200_000:
            self.reply(render_error("doc is too long"), 400)
            return
        try:
            with open(os.path.join(CODE_DIR, repo, LOOP_DOC), "w", encoding="utf-8") as fh:
                fh.write(text)
        except OSError as exc:
            self.reply(render_error(f"could not save doc: {exc}"), 500)
            return
        self.redirect(f"/repos?doc={quote(repo, safe='')}&sent=saved")

    def do_repos_slot_max(self, form: dict) -> None:
        repo = form.get("repo", [""])[0].strip()
        raw_max = form.get("max", [""])[0].strip()
        try:
            max_value = int(raw_max)
        except ValueError:
            max_value = None
        if not self._repo_loopable(repo) or max_value is None or max_value < 1:
            self.reply(render_error("bad slot-max request"), 400)
            return
        try:
            slots_redis.set_max(_repo_slot_name(repo), max_value, **self.fleet_connection)
        except CoordinatorUnreachable:
            self.reply(render_error("cannot reach the machine registry"), 502)
            return
        self.redirect("/repos")

    def do_repos_schedule(self, form: dict) -> None:
        """One-off run via `loopctl once <when> <repo>` -- a real command
        (see `_render_schedule_panel`'s docstring for why this is not the
        fake-success path issue #22 avoided for `lupin once`). Local
        machine only: `agent.py`'s ACTIONS table has no `loop.once`, so
        there is no way to enqueue this for another fleet machine, the same
        scope call `do_schedule_timer` makes for the recurring timer.
        """
        repo = form.get("repo", [""])[0].strip()
        when = form.get("when", [""])[0].strip()
        if not self._repo_loopable(repo):
            self.reply(render_error("unknown or non-loopable repository"), 400)
            return
        if not when or len(when) > 200:
            self.reply(render_error("missing or too-long schedule time"), 400)
            return
        rc, out = run(["loopctl", "once", when, repo], timeout=20.0)
        if rc != 0:
            self.reply(render_error(f"loopctl once failed: {out.strip()}"), 502)
            return
        sent = f"scheduled a one-off run for {repo} at {when}"
        self.redirect(f"/repos?sent={quote(sent, safe='')}")

    def do_model_tiers_refresh(self, form: dict) -> None:
        """"Pull models": run `model_fetch.snapshot()` live, right now, and
        save it. Each subscription's own fetch already carries a 10s
        timeout and catches its own errors (see model_fetch.py), so this
        realistically never raises -- the broad except is a last-resort
        guard so a surprise failure (e.g. disk full on save) redirects
        back with a message instead of 500ing the page, matching the
        issue's "must still render something useful" requirement.
        """
        try:
            model_fetch.save_snapshot(model_fetch.snapshot())
            sent = "pulled today's model list and prices"
        except Exception as exc:  # best-effort by design, see docstring above
            sent = f"pull failed: {exc}"
        self.redirect(f"/model-tiers?sent={quote(sent, safe='')}")

    def do_model_tiers_refresh_benchmarks(self, form: dict) -> None:
        """"Pull benchmarks": force a fresh `benchmark_fetch` dispatch,
        right now. `force=True` because a human clicking this button has
        already decided they want a fresh pull -- it still goes through
        `refresh_snapshot`'s single-fetcher lock (`benchmark_fetch.py`'s
        docstring), so if another machine is mid-fetch this click just
        reports whatever is cached instead of starting a second, paying
        dispatch. `refresh_snapshot` already turns every failure (timed
        out, bad output, Redis unreachable) into a `live: False` result
        rather than raising, so the broad except below is only a
        last-resort guard, same reasoning as `do_model_tiers_refresh`'s.
        """
        try:
            data = benchmark_fetch.refresh_snapshot(force=True, **self.fleet_connection)
            if data.get("live"):
                sent = f"pulled today's benchmark scores ({len(data.get('scores', []))} models)"
            else:
                sent = f"benchmark pull did not complete: {data.get('stale_reason', 'unknown reason')}"
        except Exception as exc:  # best-effort by design, see docstring above
            sent = f"pull failed: {exc}"
        self.redirect(f"/model-tiers?sent={quote(sent, safe='')}")

    def do_loops(self, query: dict) -> None:
        group = "machine" if query.get("group") == "machine" else "repo"
        lines_raw = (query.get("lines") or "60").strip() or "60"
        if not lines_raw.isdigit() or not (1 <= int(lines_raw) <= 5000):
            self.reply(render_error("lines must be a number from 1 to 5000"), 400)
            return
        lines = int(lines_raw)

        data = gather_loops(self.fleet_connection)
        entries = data["entries"]
        selected_repo = (query.get("repo") or "").strip()
        selected = next((e for e in entries if e["repo"] == selected_repo), None)
        if selected is None and entries and not selected_repo:
            selected = entries[0]
            selected_repo = selected["repo"]

        tail = None
        if selected is not None and selected["status"] == "running":
            tail = session_tail(selected["session"]["name"], lines)

        if query.get("fullscreen") == "1":
            if selected is None:
                self.reply(render_error("no such loop"), 404)
                return
            self.reply(render_loop_fullscreen(selected, tail, lines, group))
            return

        self.reply(render_loops(data, group=group, selected_repo=selected_repo, selected_tail=tail, lines=lines))

    def _loop_targets(self, machine: str, scope: str, repo: str) -> list[str]:
        """Repos to act on for one close request. `"all"` means every loop
        this dashboard currently believes is on `machine` -- there's no
        broader fleet-wide scope, and no finer scope below one repo either:
        loopctl only ever stops or starts a whole repo's loop (see `loopctl
        --help` -- `stop`/`run` both take a repo name, nothing smaller). The
        mockup's "this issue"/"this loop" options aren't offered here
        because neither corresponds to anything loopctl or agent.py's
        ACTIONS table can actually act on.
        """
        if scope != "all":
            return [repo]
        entries = gather_loops(self.fleet_connection)["entries"]
        targets = [
            e["repo"] for e in entries if e["machine"] == machine and e["status"] in ("running", "remote")
        ]
        return targets or [repo]

    def do_loops_close(self, form: dict) -> None:
        repo = form.get("repo", [""])[0].strip()
        machine = form.get("machine", [""])[0].strip()
        scope = form.get("scope", ["repo"])[0].strip()
        if not repo or not _valid_repo_name(repo) or not machine:
            self.reply(render_error("bad close request"), 400)
            return
        targets = self._loop_targets(machine, scope, repo)
        if any(not _valid_repo_name(t) for t in targets):
            self.reply(render_error("bad repo name"), 400)
            return
        local_host = machines.hostname()
        if machine != local_host and not self.cmd_signing_key:
            self.reply(
                render_error(
                    "closing a loop on another machine needs --cmd-signing-key "
                    "or $LUPIN_CMD_SIGNING_KEY"
                ),
                400,
            )
            return
        errors = []
        for target in targets:
            try:
                result = loops.dispatch_loop_action(
                    machine=machine, local_host=local_host,
                    local_argv=["loopctl", "stop", target],
                    queue_action="loop.stop", queue_params={"repo": target},
                    connection=self.fleet_connection, signing_key=self.cmd_signing_key,
                    actor="lupin-dashboard", issuer=local_host,
                    run_local=lambda argv: run(argv, timeout=20.0),
                )
            except CoordinatorUnreachable:
                self.reply(render_error("cannot reach the redis coordinator"), 502)
                return
            if result["mode"] == "local" and result["returncode"] != 0:
                errors.append(f"{target}: {result['output'].strip()}")
        if errors:
            self.reply(render_error("loopctl stop failed:\n" + "\n".join(errors)), 502)
            return
        self.redirect(f"/loops?repo={quote(repo, safe='')}")

    def do_loops_start(self, form: dict) -> None:
        repo = form.get("repo", [""])[0].strip()
        machine = form.get("machine", [""])[0].strip()
        if not repo or not _valid_repo_name(repo) or not machine:
            self.reply(render_error("bad start request"), 400)
            return
        local_host = machines.hostname()
        if machine != local_host and not self.cmd_signing_key:
            self.reply(
                render_error(
                    "starting a loop on another machine needs --cmd-signing-key "
                    "or $LUPIN_CMD_SIGNING_KEY"
                ),
                400,
            )
            return
        try:
            result = loops.dispatch_loop_action(
                machine=machine, local_host=local_host,
                local_argv=["loopctl", "run", repo],
                queue_action="loop.run", queue_params={"repo": repo},
                connection=self.fleet_connection, signing_key=self.cmd_signing_key,
                actor="lupin-dashboard", issuer=local_host,
                run_local=lambda argv: run(argv, timeout=20.0),
            )
        except CoordinatorUnreachable:
            self.reply(render_error("cannot reach the redis coordinator"), 502)
            return
        if result["mode"] == "local" and result["returncode"] != 0:
            self.reply(render_error(f"loopctl run failed: {result['output'].strip()}"), 502)
            return
        self.redirect(f"/loops?repo={quote(repo, safe='')}")

    def do_schedule_timer(self, form: dict) -> None:
        """Start or stop `delegation-loop.timer` -- always on this machine.
        Issue #22's scope call: the timer card controls the timer on
        whatever host `lupin serve` itself runs on, same as `timers()`/
        `timer_active()` already read it; there is no cross-machine timer
        control to wire up here.
        """
        action = (form.get("action") or [None])[0]
        if action not in ("start", "stop"):
            self.reply(render_error("bad timer action"), 400)
            return
        rc, out = run(["systemctl", action, "delegation-loop.timer"], timeout=10.0)
        if rc != 0:
            self.reply(render_error(f"systemctl {action} failed: {out.strip()}"), 502)
            return
        self.redirect("/schedule")

    def do_schedule_run(self, form: dict) -> None:
        """"Run now": start one `loop.run` per target repo, on a machine
        chosen by `place` -- "spread" (round-robin across ranked online
        machines), "any" (the single best-ranked one), or a user-pinned
        machine name. See `_rank_candidates`'s docstring for why this does
        not call `place.py`'s `place()`: there is no task here to classify
        or quota-routed provider to pick a machine for, just free loop
        capacity.
        """
        enabled = enabled_repos()
        repo_choice = (form.get("repo") or [None])[0]
        if not repo_choice:
            self.reply(render_error("missing repo"), 400)
            return
        if repo_choice == "all":
            if not enabled:
                self.reply(render_error("no enabled repos to run"), 400)
                return
            cnt_raw = (form.get("cnt") or ["1"])[0].strip()
            try:
                cnt = int(cnt_raw)
            except ValueError:
                cnt = None
            if cnt is None or not (1 <= cnt <= 4):
                self.reply(render_error("loop count must be 1-4"), 400)
                return
            targets_repos = sorted(enabled)[:cnt]
        else:
            # A specific repo always means exactly one loop -- loopctl runs
            # one tmux session per repo, so "N loops" of the same repo has
            # nothing to mean; the loop-count field only matters for "all
            # enabled repos", where it picks how many distinct repos to
            # start. See this handler's issue report for the full reasoning.
            if not _valid_repo_name(repo_choice) or repo_choice not in enabled:
                self.reply(render_error("unknown or non-enabled repository"), 400)
                return
            targets_repos = [repo_choice]

        place_choice = (form.get("place") or [None])[0]
        if not place_choice:
            self.reply(render_error("missing placement"), 400)
            return

        note = (form.get("note") or [""])[0].strip()
        if len(note) > 500:
            self.reply(render_error("note is too long"), 400)
            return

        local_host = machines.hostname()
        try:
            records = machines.machines(self.fleet_connection)
        except CoordinatorUnreachable:
            records = []

        if place_choice in ("spread", "any"):
            ranked = _rank_candidates(records, local_host)
            if not ranked:
                self.reply(render_error("no machine is available to place a run on"), 400)
                return
            if place_choice == "spread":
                assigned = [ranked[i % len(ranked)]["name"] for i in range(len(targets_repos))]
            else:
                assigned = [ranked[0]["name"]] * len(targets_repos)
        elif _machine_available(place_choice, records, local_host):
            assigned = [place_choice] * len(targets_repos)
        else:
            self.reply(render_error("bad placement"), 400)
            return

        if any(m != local_host for m in assigned) and not self.cmd_signing_key:
            self.reply(
                render_error(
                    "starting a loop on another machine needs --cmd-signing-key "
                    "or $LUPIN_CMD_SIGNING_KEY"
                ),
                400,
            )
            return

        errors = []
        for repo, machine in zip(targets_repos, assigned):
            try:
                result = loops.dispatch_loop_action(
                    machine=machine, local_host=local_host,
                    local_argv=["loopctl", "run", repo],
                    queue_action="loop.run", queue_params={"repo": repo},
                    connection=self.fleet_connection, signing_key=self.cmd_signing_key,
                    actor="lupin-dashboard", issuer=local_host,
                    run_local=lambda argv: run(argv, timeout=20.0),
                )
            except CoordinatorUnreachable:
                errors.append(f"{repo}@{machine}: cannot reach the redis coordinator")
                continue
            if result["mode"] == "local" and result["returncode"] != 0:
                errors.append(f"{repo}@{machine}: {result['output'].strip()}")
        if errors:
            self.reply(render_error("run now failed:\n" + "\n".join(errors)), 502)
            return

        summary = ", ".join(f"{r}@{m}" for r, m in zip(targets_repos, assigned))
        sent = f"Started {len(targets_repos)} loop(s): {summary}"
        self.redirect(f"/schedule?sent={quote(sent, safe='')}")

    def do_peek(self, query: dict) -> None:
        repo = query.get("repo", "").strip()
        if not repo or "/" in repo or repo in (".", ".."):
            self.reply(render_error("bad repo name"), 400)
            return
        lines = query.get("lines", "60").strip() or "60"
        if not lines.isdigit() or not (1 <= int(lines) <= 5000):
            self.reply(render_error("lines must be a number from 1 to 5000"), 400)
            return
        session = f"{SESSION_PREFIX}{repo}"
        out = session_tail(session, int(lines))
        body = (
            f"<header><h1>{esc(repo)}</h1><span class=sp></span>"
            "<a href='/'>back to dashboard</a></header>"
            f"<pre style='max-height:none'>{esc(out.rstrip() or '(no output)')}</pre>"
        )
        self.reply(page(f"peek {repo}", body, active="overview"))

    def quest_state(self, quest_id: str) -> dict | None:
        """Read `quest:<quest_id>` and work out which of its issues are
        still claimed (in progress) versus released (done, closed, or
        merged). Returns `None` if there's no id, no such quest, or Redis
        can't be reached -- the roadmap page just skips the progress card
        in that case rather than failing the whole (read-only) page.
        """
        if not quest_id:
            return None
        try:
            record = quest.read_quest(quest_id, self.fleet_connection)
        except CoordinatorUnreachable:
            return None
        if record is None:
            return None
        targets = record.get("targets", [])
        owner_repos = sorted({target.rpartition("#")[0] for target in targets})
        try:
            held = claims.claims_for(owner_repos, **self.fleet_connection) if owner_repos else {}
        except CoordinatorUnreachable:
            held = {}
        holder = f"quest:{quest_id}"
        pending, done = [], []
        for number, target in zip(record.get("issues", []), targets):
            if held.get(target, {}).get("session") == holder:
                pending.append(number)
            else:
                done.append(number)
        return {
            "id": quest_id,
            "machine": record.get("machine"),
            "state": record.get("state"),
            "pending": pending,
            "done": done,
            "total": len(record.get("issues", [])),
        }

    def do_POST(self):  # noqa: N802
        if not self.host_ok():
            self.reply(render_error("bad Host header"), 421)
            return
        url = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.reply(render_error("bad Content-Length header"), 400)
            return
        length = max(0, min(length, MAX_FORM_BYTES))
        raw = self.rfile.read(length) if length else b""
        try:
            form = parse_qs(raw.decode("utf-8"))
        except UnicodeDecodeError:
            self.reply(render_error("request body must be utf-8"), 400)
            return

        if url.path == "/quest/start":
            self.do_quest_start(form)
        elif url.path == "/quest/stop":
            self.do_quest_stop(form)
        elif url.path == "/machines/slot-max":
            self.do_set_slot_max(form)
        elif url.path == "/loops/close":
            self.do_loops_close(form)
        elif url.path == "/loops/start":
            self.do_loops_start(form)
        elif url.path == "/schedule/timer":
            self.do_schedule_timer(form)
        elif url.path == "/schedule/run":
            self.do_schedule_run(form)
        elif url.path == "/repos/add":
            self.do_repos_add(form)
        elif url.path == "/repos/generate-docs":
            self.do_repos_generate_docs(form)
        elif url.path == "/repos/remove":
            self.do_repos_remove(form)
        elif url.path == "/repos/doc/save":
            self.do_repos_doc_save(form)
        elif url.path == "/repos/slot-max":
            self.do_repos_slot_max(form)
        elif url.path == "/repos/schedule":
            self.do_repos_schedule(form)
        elif url.path == "/model-tiers/refresh":
            self.do_model_tiers_refresh(form)
        elif url.path == "/model-tiers/refresh-benchmarks":
            self.do_model_tiers_refresh_benchmarks(form)
        else:
            self.reply(render_error("no such page"), 404)

    def do_quest_start(self, form: dict) -> None:
        repo = form.get("repo", [""])[0].strip()
        try:
            issue_numbers = [int(value) for value in form.get("issue", [])]
        except ValueError:
            self.reply(render_error("bad issue number"), 400)
            return
        if not issue_numbers:
            self.reply(render_error("select at least one issue to start a quest"), 400)
            return
        try:
            result = quest.start(issue_numbers, enabled_repos(), connection=self.fleet_connection)
        except quest.QuestError as exc:
            self.reply(render_error(f"quest start failed: {exc}"), 400)
            return
        except CoordinatorUnreachable as exc:
            self.reply(render_error(f"cannot reach the redis coordinator: {exc}"), 502)
            return
        query = f"quest={quote(result['id'], safe='')}"
        if repo:
            query = f"repo={quote(repo, safe='')}&{query}"
        self.redirect(f"/roadmap?{query}")

    def do_quest_stop(self, form: dict) -> None:
        quest_id = form.get("id", [""])[0].strip()
        repo = form.get("repo", [""])[0].strip()
        if not quest_id:
            self.reply(render_error("missing quest id"), 400)
            return
        try:
            quest.stop(quest_id, connection=self.fleet_connection)
        except quest.QuestError as exc:
            self.reply(render_error(f"quest stop failed: {exc}"), 400)
            return
        except CoordinatorUnreachable as exc:
            self.reply(render_error(f"cannot reach the redis coordinator: {exc}"), 502)
            return
        self.redirect(f"/roadmap?repo={quote(repo, safe='')}" if repo else "/roadmap")

    def do_set_slot_max(self, form: dict) -> None:
        slot = form.get("slot", [""])[0].strip()
        raw_max = form.get("max", [""])[0].strip()
        try:
            # int(), not raw_max.isdigit(): isdigit() also accepts Unicode
            # digits like superscript two ('²') that int() then
            # can't parse, which used to crash this handler.
            max_value = int(raw_max)
        except ValueError:
            max_value = None
        if not slot or max_value is None or max_value < 1:
            self.reply(render_error("bad slot-max request"), 400)
            return
        try:
            slots_redis.set_max(slot, max_value, **self.fleet_connection)
        except machines.CoordinatorUnreachable:
            self.reply(render_error("cannot reach the machine registry"), 502)
            return
        self.send_response(303)
        self.send_header("Location", "/machines")
        self.send_header("Content-Length", "0")
        self.end_headers()


def _roadmap_rows(model: dict) -> list[dict]:
    buckets = {
        number: stage["name"]
        for stage in model["stages"]
        for number in stage["numbers"]
    }
    rows = []
    for node in model["nodes"]:
        number = node["number"]
        incoming = [
            edge for edge in model["edges"]
            if edge["to"] == number and edge["kind"] in {"depends", "parent", "split"}
        ]
        rows.append(
            {
                "number": number,
                "priority": node["priority"],
                "size": node["size"],
                "bucket": buckets.get(number, ""),
                "comments": len(node["comments"]),
                "deps": sorted(
                    {edge["from"] for edge in incoming if edge["kind"] == "depends"}
                ),
                "parents": sorted(
                    {edge["from"] for edge in incoming if edge["kind"] in {"parent", "split"}}
                ),
                "title": node["title"],
                "body": node["body"],
                "commentText": [comment["body"] for comment in node["comments"]],
            }
        )
    return rows


def _print_roadmap(repo: str, model: dict, verbose: bool, as_json: bool) -> None:
    rows = _roadmap_rows(model)
    if as_json:
        if not verbose:
            for row in rows:
                del row["title"]
                del row["body"]
                del row["commentText"]
        print(json.dumps({"repo": repo, "issues": rows}, ensure_ascii=False, indent=2))
        return
    for row in rows:
        refs = []
        if row["deps"]:
            refs.append("deps:" + ",".join(f"#{number}" for number in row["deps"]))
        if row["parents"]:
            refs.append("parent:" + ",".join(f"#{number}" for number in row["parents"]))
        suffix = " " + " ".join(refs) if refs else ""
        print(
            f"#{row['number']} {row['bucket']} {row['priority']} {row['size']} "
            f"{row['comments']} comments{suffix}"
        )
        if verbose:
            node = next(node for node in model["nodes"] if node["number"] == row["number"])
            print(f"  {row['title']}")
            print(f"  {row['body']}")
            for index, comment in enumerate(node["comments"], 1):
                print(f"  Comment {index}: {comment['body']}")


def main(argv: list[str] | None = None) -> int:
    """Serve the dashboard, or print one repository's roadmap.

    `argv` is the argument list without the leading subcommand name. The
    caller owns the argument surface (see `lupin.cli`), so this only parses
    what it needs and ignores `None` to read sys.argv.
    """
    import argparse

    ap = argparse.ArgumentParser(prog="lupin serve", add_help=False)
    ap.add_argument("--bind", default="127.0.0.1", help="loopback or a tailnet address")
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--peek-lines", type=int, default=25, help="tail lines shown per loop")
    ap.add_argument("--roadmap", metavar="REPO", help="print a repository roadmap and exit")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--json", action="store_true")
    # Fleet data (issue #15, also used by issue #19's quest POST routes and
    # issue #20's Machines page): same flags and env-var fallback as
    # cli.py's `_fleet_connection_args`, kept in sync by hand since serve.py
    # parses its own argv independently of cli.py (see this function's
    # docstring) and importing cli.py here would be circular.
    ap.add_argument("--redis-host", default=os.environ.get("LUPIN_REDIS_HOST"))
    ap.add_argument(
        "--redis-port", type=int,
        default=int(os.environ["LUPIN_REDIS_PORT"]) if os.environ.get("LUPIN_REDIS_PORT") else None,
    )
    ap.add_argument("--redis-username", default=os.environ.get("LUPIN_REDIS_USERNAME"))
    ap.add_argument("--redis-password", default=os.environ.get("LUPIN_REDIS_PASSWORD"))
    ap.add_argument(
        "--config-path", default=os.environ.get("LUPIN_FLEET_CONFIG"),
        help="fleet config file (default: $LUPIN_FLEET_CONFIG or ~/.config/lupin/fleet.json)",
    )
    # issue #21: the Loops page's close/restart needs this only to reach a
    # loop on another fleet machine -- same env var cli.py's `lupin cmd
    # send`/`lupin agent` already use (`_signing_key_arg`).
    ap.add_argument(
        "--cmd-signing-key", default=os.environ.get("LUPIN_CMD_SIGNING_KEY"),
        help="default: $LUPIN_CMD_SIGNING_KEY -- needed only to close/restart a loop on another machine",
    )
    args, _unknown = ap.parse_known_args(argv)

    if args.roadmap:
        repos = roadmap.repository_names(code_repos())
        if args.roadmap not in repos:
            print(f"lupin: unknown repository {args.roadmap!r}", file=sys.stderr)
            return 2
        model = roadmap.cached_model(args.roadmap, os.path.join(CODE_DIR, args.roadmap))
        _print_roadmap(args.roadmap, model, args.verbose, args.json)
        return 0


    try:
        addr = ipaddress.ip_address(args.bind)
    except ValueError:
        print(f"lupin: --bind must be an IP address, got {args.bind!r}", file=sys.stderr)
        return 2
    if not bind_allowed(addr):
        print(
            f"lupin: refusing to bind {args.bind}. This dashboard is meant "
            "for loopback or a tailnet address (100.64.0.0/10) only. Use an "
            "SSH forward, or a tailnet ACL, to reach it from another machine.",
            file=sys.stderr,
        )
        return 2

    class Server(ThreadingHTTPServer):
        address_family = socket.AF_INET6 if addr.version == 6 else socket.AF_INET
        daemon_threads = True

    Handler.peek_lines = args.peek_lines
    Handler.allowed_hosts = {
        f"127.0.0.1:{args.port}",
        f"localhost:{args.port}",
        f"[::1]:{args.port}",
        f"{args.bind}:{args.port}",
    }
    Handler.fleet_connection = machines.resolve_connection(
        redis_host=args.redis_host,
        redis_port=args.redis_port,
        redis_username=args.redis_username,
        redis_password=args.redis_password,
        config_path=args.config_path,
    )
    Handler.cmd_signing_key = args.cmd_signing_key

    httpd = Server((args.bind, args.port), Handler)
    print(f"lupin on http://{args.bind}:{args.port}", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
