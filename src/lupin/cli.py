"""The `lupin` command-line entry point: one executable, subcommands.

`route` and `classify` are the model-routing calls, moved out of
ghostbook.nix in issue #203. `acquire`/`hold`/`release`/`status` are the
slot-lease commands (issue #205 for the `local` backend, #210 for
`redis`). `claim`/`renew-claim`/`release-claim` mark a GitHub issue as one
loop's own, so two loops never work the same task (issue #6; Redis only, no
`--backend` choice -- see `claims.py`). `review-route` picks which lock a
routed model needs (issue #185) and `serve` runs the read-only dashboard
(issue #204). `join`/`heartbeat`/`drain`/`undrain`/`machines` are the fleet
machine registry (issue #7); see `machines.py` for the Redis record they
read and write. `place` picks which registered machine should run a task
already routed to a model (issue #9; see `place.py`). `quest` lists quests
(GitHub issues labeled `quest`) and their progress (issue #11, read-only);
`quest focus`/`quest release` pin a quest to a machine (issue #12);
`start`/`stop` claim and release a quest's issues (issue #13). `reconcile`
applies the automatic release rules -- a claim with no heartbeat, a quest
focus that is done/closed/idle/down, a started quest that is done/down
(issue #14; see `reconcile.py`). All of them share one process so a caller
has one binary to find and one `lupin --help` to read; the concerns stay as
separate modules underneath, same as this project's other CLIs split
"decide" from "do" (see review_dispatch.py).

Backend choice: `--backend local|redis` on each slot subcommand, default
from the `LUPIN_BACKEND` env var, falling back to `local` if neither is
set. An env var as the default (not a required flag) means a host's own
config (a systemd `Environment=`, a shell profile) picks the backend once,
and every call site -- `delegation-launch`, `loopctl`, an interactive
`lupin status` -- doesn't need its own copy of that choice.

Exit codes, by design (see #198's architecture plan):
  0  done
  2  busy/full (acquire, hold) -- skip and try again later, or a usage error
     from argparse itself (its own default for a bad flag; both meanings are
     "this invocation didn't produce a result", so sharing the code is fine).
     `place` reuses this code too: no online machine runs the routed
     provider right now, which is the same "try again later" shape.
  3  cannot reach the coordinator, and this slot has no local fallback. The
     `local` backend's coordinator is the filesystem, which it always
     reaches once the state root is writable, so it never returns 3. The
     `redis` backend returns 3 for any slot other than `bmo` when Redis is
     unreachable -- `bmo` falls back to the `local` backend instead (see
     `slots_redis.py`), so it does not reach this exit code. Claims have no
     local fallback at all, so `claim`/`renew-claim`/`release-claim` return
     3 for every unreachable-Redis case.
  1  any other error (malformed lease id, bad JSON input, hold with neither
     --lease nor <slot>/--holder, etc.) -- also `renew-claim`/`release-claim`
     when the caller isn't the claim's current holder.

`claim` returns 2 when another holder already has the issue -- same "busy,
skip and try again later" meaning as a full slot. `quest start` reuses the
same two codes for its own validation failures: 2 for "claimed by another
loop" or "target machine draining" (both "try again later"), 1 for
anything else (closed, missing, or blocked by an issue outside the quest).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import claims
from . import classify as classify_mod
from . import machines
from . import place as place_mod
from . import quest as quest_mod
from . import reconcile as reconcile_mod
from . import review_dispatch
from . import roadmap
from . import route as route_mod
from . import serve
from . import slots
from . import slots_redis


def _route_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("category")
    parser.add_argument("size")
    parser.add_argument(
        "--no-bmo",
        dest="bmo_available",
        action="store_false",
        default=True,
        help="bmo's lock already timed out -- skip a bmo-dependent tier0 pick",
    )
    parser.add_argument("--primary-effort", default=None)
    parser.add_argument("--json", action="store_true")


def _classify_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--issue-json", required=True, help="path to a `gh issue view --json ...` file")
    parser.add_argument("--diff-stat", default=None, help="path to a `git diff --stat` file")
    parser.add_argument("--json", action="store_true")


def _state_root_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--state-root", default=None, help="default: $LUPIN_STATE_ROOT or ~/.lupin/slots")


def _redis_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--redis-host", default=os.environ.get("LUPIN_REDIS_HOST", "localhost"),
        help="default: $LUPIN_REDIS_HOST or localhost",
    )
    parser.add_argument(
        "--redis-port", type=int, default=int(os.environ.get("LUPIN_REDIS_PORT", "6379")),
        help="default: $LUPIN_REDIS_PORT or 6379",
    )
    parser.add_argument(
        "--redis-username", default=os.environ.get("LUPIN_REDIS_USERNAME"),
        help="default: $LUPIN_REDIS_USERNAME, no auth if unset",
    )
    parser.add_argument(
        "--redis-password", default=os.environ.get("LUPIN_REDIS_PASSWORD"),
        help="default: $LUPIN_REDIS_PASSWORD, no auth if unset",
    )


def _backend_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--backend", choices=["local", "redis"], default=os.environ.get("LUPIN_BACKEND", "local"),
        help="slot-lease backend (default: $LUPIN_BACKEND or local)",
    )
    _redis_args(parser)


def _quest_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "mode", nargs="?", choices=["status", "start", "stop", "focus", "release"], default=None,
        help=(
            "omit to list every quest; 'status' for one quest's task breakdown; "
            "'start' to claim --issue N as a quest, 'stop' to release one; "
            "'focus'/'release' to pin or unpin a quest's focus machine"
        ),
    )
    parser.add_argument(
        "id", nargs="?", default=None,
        help="quest name or issue number (status, focus, release); quest id (stop)",
    )
    parser.add_argument(
        "--issue", dest="issues", type=int, action="append", default=None,
        help="an issue to ship; repeat for each (start only)",
    )
    parser.add_argument(
        "--machine", default=None,
        help="run on this machine (start); target machine (focus, default: most free slots)",
    )
    parser.add_argument("--platform", default=None, help="force a provider (start only)")
    parser.add_argument("--note", default=None, help="extra instruction for the loop (start only)")
    parser.add_argument(
        "--pin", action="store_true", help="with focus: keep the focus until explicitly released"
    )
    parser.add_argument("--json", action="store_true")
    _redis_args(parser)


def _reconcile_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--ttl", type=float, default=300.0,
        help="how long this run may hold the fleet-wide reconcile slot, in seconds",
    )
    parser.add_argument("--json", action="store_true")
    _redis_args(parser)


def _slot_common_args(parser: argparse.ArgumentParser) -> None:
    _state_root_arg(parser)
    _backend_args(parser)
    parser.add_argument("--ttl", type=float, default=slots.DEFAULT_TTL, help="lease TTL in seconds")


def _acquire_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("slot")
    parser.add_argument("--holder", required=True)
    parser.add_argument("--wait", type=float, default=0.0, help="seconds to poll before giving up")
    parser.add_argument(
        "--max", dest="max_holders", type=int, default=None,
        help="slot capacity, used only the first time this slot is created",
    )
    _slot_common_args(parser)


def _hold_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("slot", nargs="?", default=None)
    parser.add_argument("--holder", default=None)
    parser.add_argument("--lease", default=None, help="reuse a lease an earlier `acquire` returned")
    parser.add_argument("--wait", type=float, default=0.0)
    parser.add_argument("--max", dest="max_holders", type=int, default=None)
    _slot_common_args(parser)


def _release_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--lease", required=True)
    _state_root_arg(parser)
    _backend_args(parser)


def _status_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true")
    _state_root_arg(parser)
    _backend_args(parser)


def _redis_conn_args(parser: argparse.ArgumentParser) -> None:
    """Connection flags for the claim subcommands -- same env-var defaults
    as `_backend_args`, but no `--backend` choice: claims only ever live in
    Redis, there is no `local` backend for them to pick (see `claims.py`).
    """
    parser.add_argument(
        "--redis-host", default=os.environ.get("LUPIN_REDIS_HOST", "localhost"),
        help="default: $LUPIN_REDIS_HOST or localhost",
    )
    parser.add_argument(
        "--redis-port", type=int, default=int(os.environ.get("LUPIN_REDIS_PORT", "6379")),
        help="default: $LUPIN_REDIS_PORT or 6379",
    )
    parser.add_argument(
        "--redis-username", default=os.environ.get("LUPIN_REDIS_USERNAME"),
        help="default: $LUPIN_REDIS_USERNAME, no auth if unset",
    )
    parser.add_argument(
        "--redis-password", default=os.environ.get("LUPIN_REDIS_PASSWORD"),
        help="default: $LUPIN_REDIS_PASSWORD, no auth if unset",
    )


def _claim_args(parser: argparse.ArgumentParser) -> None:
    """Shared by `claim` and `renew-claim` -- both take a TTL."""
    parser.add_argument("target", help="OWNER/REPO#N, e.g. gracecraft/lupin#6")
    parser.add_argument("--holder", required=True)
    parser.add_argument("--ttl", type=float, default=claims.DEFAULT_TTL, help="claim TTL in seconds")
    _redis_conn_args(parser)


def _release_claim_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("target", help="OWNER/REPO#N, e.g. gracecraft/lupin#6")
    parser.add_argument("--holder", required=True)
    _redis_conn_args(parser)


def _serve_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--bind", default="127.0.0.1", help="loopback address only")
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument("--peek-lines", type=int, default=25, help="tail lines shown per loop")
    parser.add_argument("--roadmap", metavar="REPO", help="print a repository roadmap and exit")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--json", action="store_true")


def _review_route_args(parser: argparse.ArgumentParser) -> None:
    """`lupin review-route` — route a pair and report which lock it needs."""
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--category", help="task category, with --size")
    source.add_argument(
        "--issue-json", help="path to a `gh issue view --json ...` file to classify"
    )
    parser.add_argument("--size", help="task size label, with --category")
    parser.add_argument(
        "--diff-stat", help="path to a `git diff --stat` file (refines --issue-json size)"
    )
    parser.add_argument(
        "--mode",
        choices=("same-unit", "separate"),
        default="same-unit",
        help="same-unit (default): caller is already inside a loop-claude-* unit",
    )
    parser.add_argument(
        "--bmo-unavailable",
        action="store_true",
        help="bmo's lock already timed out -- skip a bmo-dependent tier0 pick",
    )
    parser.add_argument("--primary-effort", default=None)


def _fleet_connection_args(parser: argparse.ArgumentParser) -> None:
    """Redis location overrides for `heartbeat`/`drain`/`undrain`/`machines`.

    Unlike `_backend_args`, the default is `None`, not `"localhost"` --
    these commands fall back to what `lupin join` already wrote to the
    fleet config (`machines.resolve_connection`), and a `"localhost"`
    default would mask that file every time.
    """
    parser.add_argument("--redis-host", default=os.environ.get("LUPIN_REDIS_HOST"))
    parser.add_argument(
        "--redis-port", type=int,
        default=int(os.environ["LUPIN_REDIS_PORT"]) if os.environ.get("LUPIN_REDIS_PORT") else None,
    )
    parser.add_argument("--redis-username", default=os.environ.get("LUPIN_REDIS_USERNAME"))
    parser.add_argument("--redis-password", default=os.environ.get("LUPIN_REDIS_PASSWORD"))
    parser.add_argument(
        "--config-path", default=os.environ.get("LUPIN_FLEET_CONFIG"),
        help="fleet config file (default: $LUPIN_FLEET_CONFIG or ~/.config/lupin/fleet.json)",
    )


def _join_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("coordinator", help="redis host[:port] for this machine group")
    parser.add_argument("--redis-username", default=os.environ.get("LUPIN_REDIS_USERNAME"))
    parser.add_argument("--redis-password", default=os.environ.get("LUPIN_REDIS_PASSWORD"))
    parser.add_argument(
        "--config-path", default=os.environ.get("LUPIN_FLEET_CONFIG"),
        help="fleet config file (default: $LUPIN_FLEET_CONFIG or ~/.config/lupin/fleet.json)",
    )


def _machines_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true")
    _fleet_connection_args(parser)


def _place_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("task", help="a GitHub issue number (e.g. 418 or #418) or free text")
    parser.add_argument("--explain", action="store_true", help="show every candidate machine and why")
    parser.add_argument("--json", action="store_true")
    _fleet_connection_args(parser)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lupin", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    _route_args(sub.add_parser("route", help="pick a {model, effort} for a (category, size) pair"))
    _classify_args(sub.add_parser("classify", help="sort an issue into (category, size)"))
    _acquire_args(sub.add_parser("acquire", help="take a lease on a slot"))
    _hold_args(sub.add_parser("hold", help="acquire (or reuse a lease), run a command, release on exit"))
    _release_args(sub.add_parser("release", help="give up a lease"))
    _status_args(sub.add_parser("status", help="list every slot's holder count and max"))
    _claim_args(sub.add_parser("claim", help="atomically take a GitHub issue, so no other loop works it"))
    _claim_args(sub.add_parser("renew-claim", help="push a claim's TTL back out"))
    _release_claim_args(sub.add_parser("release-claim", help="give up a claim (compare-and-delete)"))
    _review_route_args(
        sub.add_parser("review-route", help="route a pair and report which lock it needs")
    )
    _serve_args(sub.add_parser("serve", help="run the read-only loopback dashboard"))
    _join_args(sub.add_parser("join", help="add this machine to the fleet"))
    _fleet_connection_args(sub.add_parser("heartbeat", help="refresh this machine's fleet record"))
    _fleet_connection_args(sub.add_parser("drain", help="mark this machine as draining"))
    _fleet_connection_args(sub.add_parser("undrain", help="mark this machine as online again"))
    _machines_args(sub.add_parser("machines", help="list every registered machine's state"))
    _place_args(sub.add_parser("place", help="pick which machine should run a task"))
    _quest_args(sub.add_parser("quest", help="list quests, their progress, and their focus machine"))
    _reconcile_args(sub.add_parser("reconcile", help="apply the automatic release rules once"))
    return parser


def _cmd_route(args: argparse.Namespace) -> int:
    result = route_mod.route(
        args.category,
        args.size,
        bmo_available=args.bmo_available,
        primary_effort=args.primary_effort,
    )
    if args.json:
        print(json.dumps(result))
    else:
        print(f"{result['model']} {result['effort']}")
    return 0


def _cmd_classify(args: argparse.Namespace) -> int:
    with open(args.issue_json, encoding="utf-8") as handle:
        issue = json.load(handle)
    diff_stat = None
    if args.diff_stat:
        with open(args.diff_stat, encoding="utf-8") as handle:
            diff_stat = handle.read()
    category, size = classify_mod.classify(issue, diff_stat)
    if args.json:
        print(json.dumps({"category": category, "size": size}))
    else:
        print(f"{category} {size}")
    return 0


def _backend_module(args: argparse.Namespace):
    return slots_redis if args.backend == "redis" else slots


def _backend_kwargs(args: argparse.Namespace) -> dict:
    """Extra kwargs the `redis` backend needs that `local` doesn't take."""
    if args.backend == "redis":
        return {
            "redis_host": args.redis_host,
            "redis_port": args.redis_port,
            "redis_username": args.redis_username,
            "redis_password": args.redis_password,
        }
    return {}


def _cmd_acquire(args: argparse.Namespace) -> int:
    backend = _backend_module(args)
    try:
        lease = backend.acquire(
            args.slot,
            args.holder,
            wait=args.wait,
            ttl=args.ttl,
            max_holders=args.max_holders,
            state_root=args.state_root,
            **_backend_kwargs(args),
        )
    except slots.SlotFull:
        print(f"slot {args.slot!r} is full", file=sys.stderr)
        return 2
    except slots.CoordinatorUnreachable:
        print(f"cannot reach the {args.backend} coordinator for slot {args.slot!r}", file=sys.stderr)
        return 3
    print(lease)
    return 0


def _cmd_hold(args: argparse.Namespace, command: list[str]) -> int:
    if not command:
        print("hold needs -- <command>", file=sys.stderr)
        return 1
    if args.lease and (args.slot or args.holder):
        print("--lease is exclusive with <slot>/--holder", file=sys.stderr)
        return 1
    if not args.lease and not (args.slot and args.holder):
        print("hold needs either --lease ID, or <slot> --holder H", file=sys.stderr)
        return 1
    backend = _backend_module(args)
    try:
        return backend.hold(
            command,
            lease=args.lease,
            slot=args.slot,
            holder=args.holder,
            wait=args.wait,
            ttl=args.ttl,
            max_holders=args.max_holders,
            state_root=args.state_root,
            **_backend_kwargs(args),
        )
    except slots.SlotFull:
        print(f"slot {args.slot!r} is full", file=sys.stderr)
        return 2
    except slots.CoordinatorUnreachable:
        print(f"cannot reach the {args.backend} coordinator for slot {args.slot!r}", file=sys.stderr)
        return 3


def _cmd_release(args: argparse.Namespace) -> int:
    backend = _backend_module(args)
    try:
        backend.release(args.lease, state_root=args.state_root, **_backend_kwargs(args))
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    except slots.CoordinatorUnreachable:
        print(f"cannot reach the {args.backend} coordinator for lease {args.lease!r}", file=sys.stderr)
        return 3
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    backend = _backend_module(args)
    result = backend.status(state_root=args.state_root, **_backend_kwargs(args))
    if args.json:
        print(json.dumps(result))
    else:
        for name in sorted(result):
            info = result[name]
            max_display = info["max"] if info["max"] is not None else "?"
            print(f"{name}: {info['holders']}/{max_display}")
    return 0


def _claim_kwargs(args: argparse.Namespace) -> dict:
    return {
        "redis_host": args.redis_host,
        "redis_port": args.redis_port,
        "redis_username": args.redis_username,
        "redis_password": args.redis_password,
    }


def _cmd_claim(args: argparse.Namespace) -> int:
    try:
        claims.claim(args.target, args.holder, ttl=args.ttl, **_claim_kwargs(args))
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    except claims.ClaimHeld as exc:
        print(exc, file=sys.stderr)
        return 2
    except slots.CoordinatorUnreachable:
        print(f"cannot reach the redis coordinator for claim {args.target!r}", file=sys.stderr)
        return 3
    return 0


def _cmd_renew_claim(args: argparse.Namespace) -> int:
    try:
        renewed = claims.renew_claim(args.target, args.holder, ttl=args.ttl, **_claim_kwargs(args))
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    except slots.CoordinatorUnreachable:
        print(f"cannot reach the redis coordinator for claim {args.target!r}", file=sys.stderr)
        return 3
    if not renewed:
        print(f"{args.target!r} is not held by {args.holder!r}", file=sys.stderr)
        return 1
    return 0


def _cmd_release_claim(args: argparse.Namespace) -> int:
    try:
        released = claims.release_claim(args.target, args.holder, **_claim_kwargs(args))
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    except slots.CoordinatorUnreachable:
        print(f"cannot reach the redis coordinator for claim {args.target!r}", file=sys.stderr)
        return 3
    if not released:
        print(f"{args.target!r} is not held by {args.holder!r}", file=sys.stderr)
        return 1
    return 0


def _fleet_connection(args: argparse.Namespace) -> dict:
    return machines.resolve_connection(
        redis_host=args.redis_host,
        redis_port=args.redis_port,
        redis_username=args.redis_username,
        redis_password=args.redis_password,
        config_path=args.config_path,
    )


def _cmd_join(args: argparse.Namespace) -> int:
    try:
        record = machines.join(
            args.coordinator,
            redis_username=args.redis_username,
            redis_password=args.redis_password,
            config_path=args.config_path,
        )
    except machines.CoordinatorUnreachable as exc:
        print(f"cannot reach the {exc}", file=sys.stderr)
        return 3
    print(json.dumps(record))
    return 0


def _cmd_heartbeat(args: argparse.Namespace) -> int:
    try:
        record = machines.heartbeat(_fleet_connection(args))
    except machines.CoordinatorUnreachable as exc:
        print(f"cannot reach the {exc}", file=sys.stderr)
        return 3
    print(json.dumps(record))
    return 0


def _cmd_drain(args: argparse.Namespace) -> int:
    try:
        record = machines.drain(_fleet_connection(args))
    except machines.CoordinatorUnreachable as exc:
        print(f"cannot reach the {exc}", file=sys.stderr)
        return 3
    print(json.dumps(record))
    return 0


def _cmd_undrain(args: argparse.Namespace) -> int:
    try:
        record = machines.undrain(_fleet_connection(args))
    except machines.CoordinatorUnreachable as exc:
        print(f"cannot reach the {exc}", file=sys.stderr)
        return 3
    print(json.dumps(record))
    return 0


def _cmd_machines(args: argparse.Namespace) -> int:
    try:
        result = machines.machines(_fleet_connection(args))
    except machines.CoordinatorUnreachable as exc:
        print(f"cannot reach the {exc}", file=sys.stderr)
        return 3
    if args.json:
        print(json.dumps(result))
    else:
        for record in result:
            note = " (version mismatch)" if record["version_mismatch"] else ""
            print(f"{record['name']}: {record['state']}{note}")
    return 0


def _format_place_table(result: dict) -> str:
    headers = ["machine", "state", "slots", "quest focus", "lupin", "result"]
    rows = [
        [
            c["name"],
            c["state"],
            f"{c['slots_used']}/{c['slots_max']}",
            c["quest_focus"] or "-",
            c["version"] or "-",
            c["result"],
        ]
        for c in result["candidates"]
    ]
    widths = [len(h) for h in headers]
    for row in rows:
        for i, value in enumerate(row):
            widths[i] = max(widths[i], len(value))

    def fmt(cells: list[str]) -> str:
        return "   ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells)).rstrip()

    return "\n".join([fmt(headers)] + [fmt(row) for row in rows])


def _format_place_explain(result: dict) -> str:
    lines = [
        f"{result['task_label']} · {result['category_label']}, {result['size_label']}"
        f" → {result['model']} {result['effort']} ({result['provider']})"
    ]
    quota = result["quota"]
    if quota and quota.get("pct_left") is not None:
        pct = f"{quota['pct_left']:.0f}%"
        resets_at = quota.get("resets_at")
        if resets_at:
            resets_in = place_mod.format_duration(resets_at / 1000 - time.time())
            lines.append(f"{result['provider']} quota {pct}, resets in {resets_in}")
        else:
            lines.append(f"{result['provider']} quota {pct}")
    else:
        lines.append(f"{result['provider']} quota: unknown")
    lines.append("")
    if result["candidates"]:
        lines.append(_format_place_table(result))
    skipped = result["skipped"]
    total_skipped = skipped["offline"] + skipped["other_provider"]
    if total_skipped:
        reasons = []
        if skipped["other_provider"]:
            reasons.append(f"{skipped['other_provider']} run a different provider")
        if skipped["offline"]:
            reasons.append(f"{skipped['offline']} offline")
        lines.append(f"{total_skipped} machine(s) skipped: {', '.join(reasons)}")
    return "\n".join(lines)


def _cmd_place(args: argparse.Namespace) -> int:
    try:
        result = place_mod.place(args.task, _fleet_connection(args))
    except place_mod.CoordinatorUnreachable as exc:
        print(f"cannot reach the {exc}", file=sys.stderr)
        return 3
    if args.json:
        print(json.dumps(result))
        return 0 if result["pick"] else 2
    if args.explain:
        print(_format_place_explain(result))
        return 0 if result["pick"] else 2
    if not result["pick"]:
        print(f"no online machine runs provider {result['provider']!r}", file=sys.stderr)
        return 2
    print(result["run_command"])
    return 0


def _cmd_quest_focus(args: argparse.Namespace, repos: list[str], redis_kwargs: dict) -> int:
    if not args.id:
        print("quest focus needs a quest name", file=sys.stderr)
        return 1
    try:
        result = quest_mod.focus(args.id, redis_kwargs, repos, machine=args.machine, pin=args.pin)
    except quest_mod.QuestNotFound as exc:
        print(exc, file=sys.stderr)
        return 1
    except (quest_mod.MachineDraining, quest_mod.MachineNotFound, quest_mod.NoReadyTasks) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except quest_mod.NoMachineAvailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except quest_mod.CoordinatorUnreachable as exc:
        print(f"cannot reach the {exc}", file=sys.stderr)
        return 3
    print(
        f"focused {result['quest']} on {result['machine']} · "
        f"{result['ready_count']} ready tasks will route there in order"
    )
    return 0


def _cmd_quest_release(args: argparse.Namespace, repos: list[str], redis_kwargs: dict) -> int:
    if not args.id:
        print("quest release needs a quest name", file=sys.stderr)
        return 1
    try:
        result = quest_mod.release(args.id, redis_kwargs, repos)
    except quest_mod.QuestNotFound as exc:
        print(exc, file=sys.stderr)
        return 1
    except quest_mod.NoFocus as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except quest_mod.CoordinatorUnreachable as exc:
        print(f"cannot reach the {exc}", file=sys.stderr)
        return 3
    print(f"released {result['quest']} · {result['machine']} returns to normal routing")
    return 0


def _cmd_quest(args: argparse.Namespace) -> int:
    repos = serve.enabled_repos()
    redis_kwargs = {
        "redis_host": args.redis_host,
        "redis_port": args.redis_port,
        "redis_username": args.redis_username,
        "redis_password": args.redis_password,
    }

    if args.mode == "start":
        if not args.issues:
            print("quest start needs at least one --issue", file=sys.stderr)
            return 1
        try:
            result = quest_mod.start(
                args.issues,
                repos,
                connection=redis_kwargs,
                machine=args.machine,
                platform=args.platform,
                note=args.note,
            )
        except quest_mod.QuestError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return exc.exit_code
        except slots.CoordinatorUnreachable as exc:
            print(f"cannot reach the redis coordinator for quest start: {exc}", file=sys.stderr)
            return 3
        if args.json:
            print(json.dumps(result))
        else:
            print(quest_mod.render_start(result))
        return 0

    if args.mode == "stop":
        if not args.id:
            print("quest stop needs an id", file=sys.stderr)
            return 1
        try:
            message = quest_mod.stop(args.id, connection=redis_kwargs)
        except quest_mod.QuestError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return exc.exit_code
        except slots.CoordinatorUnreachable as exc:
            print(f"cannot reach the redis coordinator for quest stop: {exc}", file=sys.stderr)
            return 3
        if args.json:
            print(json.dumps({"id": args.id, "message": message}))
        else:
            print(message)
        return 0

    if args.mode == "focus":
        return _cmd_quest_focus(args, repos, redis_kwargs)
    if args.mode == "release":
        return _cmd_quest_release(args, repos, redis_kwargs)

    quests, warnings = quest_mod.load_quests(repos)
    for warning in warnings:
        print(f"lupin: {warning}", file=sys.stderr)

    if args.mode == "status":
        if args.id:
            match = quest_mod.find_quest(quests, args.id)
            if match is None:
                print(f"no quest matches {args.id!r}", file=sys.stderr)
                return 1
            selected = [match]
        else:
            selected = quests
        dag = roadmap.cached_dependency_dag(repos)
        results = [
            quest_mod.status_to_json(
                one, dag, quest_mod.read_focus(one["name"], **redis_kwargs)
            )
            for one in selected
        ]
        if args.json:
            print(json.dumps(results))
        elif not results:
            print("No quests found.")
        else:
            print("\n\n".join(quest_mod.render_status(result) for result in results))
        return 0

    focuses = {
        one["name"]: quest_mod.read_focus(one["name"], **redis_kwargs) for one in quests
    }
    if args.json:
        print(json.dumps([quest_mod.to_json(one, focuses[one["name"]]) for one in quests]))
    else:
        print(quest_mod.render_list(quests, focuses))
    return 0


def _cmd_reconcile(args: argparse.Namespace) -> int:
    repos = serve.enabled_repos()
    redis_kwargs = {
        "redis_host": args.redis_host,
        "redis_port": args.redis_port,
        "redis_username": args.redis_username,
        "redis_password": args.redis_password,
    }
    try:
        lease = slots_redis.acquire("reconcile", machines.hostname(), wait=0.0, ttl=args.ttl, **redis_kwargs)
    except slots.SlotFull:
        print("reconcile is already running on another machine", file=sys.stderr)
        return 2
    except slots.CoordinatorUnreachable:
        print("cannot reach the redis coordinator for the reconcile slot", file=sys.stderr)
        return 3

    try:
        lines, warnings = reconcile_mod.reconcile(repos, redis_kwargs)
    except reconcile_mod.CoordinatorUnreachable as exc:
        print(f"cannot reach the {exc}", file=sys.stderr)
        return 3
    finally:
        try:
            slots_redis.release(lease, **redis_kwargs)
        except slots.CoordinatorUnreachable:
            pass

    for warning in warnings:
        print(f"lupin: {warning}", file=sys.stderr)
    if args.json:
        print(json.dumps({"released": lines}))
        return 0
    if not lines:
        print("Nothing to release.")
        return 0
    for line in lines:
        print(line)
    return 0


def main(argv: list[str] | None = None) -> int:
    raw = sys.argv[1:] if argv is None else argv
    if "--" in raw:
        split = raw.index("--")
        lupin_argv, command = raw[:split], raw[split + 1 :]
    else:
        lupin_argv, command = raw, []

    parser = _build_parser()
    args = parser.parse_args(lupin_argv)

    if args.cmd == "serve":
        return serve.main(lupin_argv[1:])

    if args.cmd == "review-route":
        return review_dispatch.main(lupin_argv[1:])

    if args.cmd == "route":
        return _cmd_route(args)
    if args.cmd == "classify":
        return _cmd_classify(args)
    if args.cmd == "acquire":
        return _cmd_acquire(args)
    if args.cmd == "hold":
        return _cmd_hold(args, command)
    if args.cmd == "release":
        return _cmd_release(args)
    if args.cmd == "status":
        return _cmd_status(args)
    if args.cmd == "claim":
        return _cmd_claim(args)
    if args.cmd == "renew-claim":
        return _cmd_renew_claim(args)
    if args.cmd == "release-claim":
        return _cmd_release_claim(args)
    if args.cmd == "join":
        return _cmd_join(args)
    if args.cmd == "heartbeat":
        return _cmd_heartbeat(args)
    if args.cmd == "drain":
        return _cmd_drain(args)
    if args.cmd == "undrain":
        return _cmd_undrain(args)
    if args.cmd == "machines":
        return _cmd_machines(args)
    if args.cmd == "place":
        return _cmd_place(args)
    if args.cmd == "quest":
        return _cmd_quest(args)
    if args.cmd == "reconcile":
        return _cmd_reconcile(args)
    parser.error(f"unknown command {args.cmd!r}")  # pragma: no cover
    return 1


if __name__ == "__main__":
    sys.exit(main())
