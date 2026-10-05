"""Decide how to run a review call, and print the decision as JSON.

This is the wiring for issue #185: `route.route()` (issue #184) already picks
a {model, effort} pair; this module picks which lock that pair needs, so the
shell dispatch code in hosts/jesus/configuration.nix/loopctl.nix can reuse
the existing claude-dispatch.lock/omp.lock machinery instead of a hardcoded
model. It decides; it never touches a lock or a process itself -- same
division of work as route.py.

Three plans, matching the three cases in #185:

- "same_unit": a claude model, run sequentially inside the caller's own
  already-counted loop-claude-* unit. No lock -- this costs nothing extra
  against claude_max_concurrent because it starts no new systemd unit.
- "claude_lock": a claude model, but a separate process is unavoidable. Take
  claude-dispatch.lock and recheck capacity before launching.
- "omp_lock": the routed model runs through omp's shared LM Studio server
  (a "bmo:" or "local:" model -- see the omp case's own comment in
  configuration.nix for why both share one lock). Take the blocking
  omp.lock, same as the omp dispatch path.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import classify
from . import route


def dispatch_plan(model: str, *, mode: str = "same-unit") -> str:
    """Return "same_unit", "claude_lock", or "omp_lock" for a routed model.

    `mode` only matters for a claude model: "same-unit" (the default) means
    the caller is itself inside the primary loop's loop-claude-* unit and
    can run the review in-process; "separate" means it cannot, so the result
    picks the lock path instead.
    """
    if model.startswith("bmo:") or model.startswith("local:"):
        return "omp_lock"
    if mode == "separate":
        return "claude_lock"
    return "same_unit"


def decide(
    category: str,
    size: str,
    *,
    bmo_available: bool = True,
    primary_effort: str | None = None,
    mode: str = "same-unit",
) -> dict:
    """Route a (category, size) pair and attach its dispatch plan."""
    # route.route() reads model-tiers.json as packaged data, so there is no
    # tiers path to pass here.
    choice = route.route(
        category,
        size,
        bmo_available=bmo_available,
        primary_effort=primary_effort,
    )
    return {**choice, "plan": dispatch_plan(choice["model"], mode=mode)}


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
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
    args = parser.parse_args(argv)
    if args.category and not args.size:
        parser.error("--category needs --size")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    if args.issue_json:
        with open(args.issue_json, encoding="utf-8") as handle:
            issue = json.load(handle)
        diff_stat = None
        if args.diff_stat:
            with open(args.diff_stat, encoding="utf-8") as handle:
                diff_stat = handle.read()
        category, size = classify.classify(issue, diff_stat)
    else:
        category, size = args.category, args.size

    result = decide(
        category,
        size,
        bmo_available=not args.bmo_unavailable,
        primary_effort=args.primary_effort,
        mode=args.mode,
    )
    print(json.dumps({**result, "category": category, "size": size}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
