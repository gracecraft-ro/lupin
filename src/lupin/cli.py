"""The `lupin` command-line entry point: one executable, subcommands.

`route` and `classify` are the model-routing calls, moved out of
ghostbook.nix in issue #203. `acquire`/`hold`/`release`/`status` are the
slot-lease commands (issue #205 for the `local` backend, #210 for
`redis`). `review-route` picks which lock a routed model needs (issue
#185) and `serve` runs the read-only dashboard (issue #204). All of them
share one process so a caller has one binary to find and one `lupin --help`
to read; the concerns stay as separate modules underneath, same as this
project's other CLIs split "decide" from "do" (see review_dispatch.py).

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
     "this invocation didn't produce a result", so sharing the code is fine)
  3  cannot reach the coordinator, and this slot has no local fallback. The
     `local` backend's coordinator is the filesystem, which it always
     reaches once the state root is writable, so it never returns 3. The
     `redis` backend returns 3 for any slot other than `bmo` when Redis is
     unreachable -- `bmo` falls back to the `local` backend instead (see
     `slots_redis.py`), so it does not reach this exit code.
  1  any other error (malformed lease id, bad JSON input, hold with neither
     --lease nor <slot>/--holder, etc.)
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import classify as classify_mod
from . import review_dispatch
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


def _backend_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--backend", choices=["local", "redis"], default=os.environ.get("LUPIN_BACKEND", "local"),
        help="slot-lease backend (default: $LUPIN_BACKEND or local)",
    )
    parser.add_argument(
        "--redis-host", default=os.environ.get("LUPIN_REDIS_HOST", "localhost"),
        help="redis backend only (default: $LUPIN_REDIS_HOST or localhost)",
    )
    parser.add_argument(
        "--redis-port", type=int, default=int(os.environ.get("LUPIN_REDIS_PORT", "6379")),
        help="redis backend only (default: $LUPIN_REDIS_PORT or 6379)",
    )


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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lupin", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    _route_args(sub.add_parser("route", help="pick a {model, effort} for a (category, size) pair"))
    _classify_args(sub.add_parser("classify", help="sort an issue into (category, size)"))
    _acquire_args(sub.add_parser("acquire", help="take a lease on a slot"))
    _hold_args(sub.add_parser("hold", help="acquire (or reuse a lease), run a command, release on exit"))
    _release_args(sub.add_parser("release", help="give up a lease"))
    _status_args(sub.add_parser("status", help="list every slot's holder count and max"))
    _review_route_args(
        sub.add_parser("review-route", help="route a pair and report which lock it needs")
    )
    _serve_args(sub.add_parser("serve", help="run the read-only loopback dashboard"))
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
        return {"redis_host": args.redis_host, "redis_port": args.redis_port}
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
    parser.error(f"unknown command {args.cmd!r}")  # pragma: no cover
    return 1


if __name__ == "__main__":
    sys.exit(main())
