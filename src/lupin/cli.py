"""The `lupin` command-line entry point: one executable, subcommands.

`route` and `classify` are the model-routing calls moved from
hosts/jesus/loopgui/ (issue #203). `acquire`/`hold`/`release`/`status` are
the slot-lease commands (issue #205), backed by lupin.slots' `local`
backend. Both groups share one process so a caller only has one binary to
find and one `lupin --help` to read; the two concerns (route decisions,
lease state) stay as separate modules underneath, same as this project's
other CLIs already split "decide" from "do" (see review_dispatch.py's
docstring in the repo this was moved out of).

Exit codes, by design (see #198's architecture plan):
  0  done
  2  busy/full (acquire, hold) -- skip and try again later, or a usage error
     from argparse itself (its own default for a bad flag; both meanings are
     "this invocation didn't produce a result", so sharing the code is fine)
  3  reserved for "cannot reach the coordinator" -- the `local` backend's
     coordinator is the filesystem, which this process always reaches once
     the state root is writable, so nothing here actually returns 3 today.
     It is reserved so a future `redis` backend can use it without changing
     this contract.
  1  any other error (malformed lease id, bad JSON input, hold with neither
     --lease nor <slot>/--holder, etc.)
"""

from __future__ import annotations

import argparse
import json
import sys

from . import classify as classify_mod
from . import route as route_mod
from . import slots


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


def _slot_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--state-root", default=None, help="default: $LUPIN_STATE_ROOT or ~/.lupin/slots")
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
    parser.add_argument("--state-root", default=None)


def _status_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--state-root", default=None)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lupin", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    _route_args(sub.add_parser("route", help="pick a {model, effort} for a (category, size) pair"))
    _classify_args(sub.add_parser("classify", help="sort an issue into (category, size)"))
    _acquire_args(sub.add_parser("acquire", help="take a lease on a slot"))
    _hold_args(sub.add_parser("hold", help="acquire (or reuse a lease), run a command, release on exit"))
    _release_args(sub.add_parser("release", help="give up a lease"))
    _status_args(sub.add_parser("status", help="list every slot's holder count and max"))
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


def _cmd_acquire(args: argparse.Namespace) -> int:
    try:
        lease = slots.acquire(
            args.slot,
            args.holder,
            wait=args.wait,
            ttl=args.ttl,
            max_holders=args.max_holders,
            state_root=args.state_root,
        )
    except slots.SlotFull:
        print(f"slot {args.slot!r} is full", file=sys.stderr)
        return 2
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
    try:
        return slots.hold(
            command,
            lease=args.lease,
            slot=args.slot,
            holder=args.holder,
            wait=args.wait,
            ttl=args.ttl,
            max_holders=args.max_holders,
            state_root=args.state_root,
        )
    except slots.SlotFull:
        print(f"slot {args.slot!r} is full", file=sys.stderr)
        return 2


def _cmd_release(args: argparse.Namespace) -> int:
    try:
        slots.release(args.lease, state_root=args.state_root)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    result = slots.status(state_root=args.state_root)
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
