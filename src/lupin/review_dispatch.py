"""Decide how to run a review call, and print the decision as JSON.

`route.route()` picks a {model, effort} pair. This module picks the lock that
pair needs. It decides only. It never takes a lock or starts a process.

Plans:

- "same_unit": no fleet lock. Used for a Claude model in same-unit mode, and
  for any model on another provider (for example `opencode-go/glm-5.3`) in
  either mode.
- "claude_lock": a Claude model in separate mode. Take claude-dispatch.lock
  and recheck capacity before launching.
- "omp_lock": a `bmo:` or `local:` model. It runs on the shared LM Studio
  server, so take the blocking omp.lock.

`--prefetch` (issue #1) is a second, unrelated mode in the same command: it
fetches issue/PR text up front so a delegation-loop orchestrator, and the
subagent it dispatches, don't each spend a turn on their own `gh` calls. It
does not route anything -- see `prefetch()` below.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

from . import classify
from . import route

# Same redaction as roadmap.py's _run_json: a `gh` error can echo a token
# back (e.g. in a URL) if auth is misconfigured.
_TOKEN_RE = re.compile(r"(?:gh[oprsu]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)")


def dispatch_plan(model: str, *, mode: str = "same-unit") -> str:
    """Return "same_unit", "claude_lock", or "omp_lock" for a routed model.

    `mode` changes the result only for a Claude model. "same-unit" (the
    default) means the caller already runs inside the primary loop's
    loop-claude-* unit, so it runs the review in-process. "separate" means it
    cannot, so a Claude model returns "claude_lock".

    A model on any other provider (for example `opencode-go/glm-5.3`) returns
    "same_unit" in both modes. "same_unit" means no fleet lock. The claude
    lock serializes Claude calls only, so holding it for a call that never
    touches Claude would block real Claude work.
    """
    if model.startswith("bmo:") or model.startswith("local:"):
        return "omp_lock"
    if mode == "separate" and route.provider_for_model(model) == "claude":
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


def _run_gh_json(args: list[str], *, repo: str | None = None, timeout: int = 60):
    """Run `gh <args> --json ...`, return (data, error).

    Same shape as roadmap.py's `_run_json`: exactly one of the pair is not
    None. Kept separate because this module takes no repo checkout path --
    `gh` runs from the current directory, or against `--repo` if given.
    """
    argv = ["gh", *args]
    if repo:
        argv += ["--repo", repo]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return None, "gh is not installed"
    except OSError as error:
        return None, f"gh could not start: {error}"
    except subprocess.TimeoutExpired:
        return None, f"gh timed out after {timeout} seconds"
    if result.returncode:
        error = (result.stderr or result.stdout or "gh failed").strip()
        error = _TOKEN_RE.sub("[redacted]", error)
        return None, error
    try:
        return json.loads(result.stdout), None
    except json.JSONDecodeError as error:
        return None, f"invalid JSON from gh: {error}"


def _fetch_item(item: str, *, repo: str | None = None) -> dict:
    """Fetch one issue or PR's text and (for a PR) its diff stat.

    An issue and a PR share one number counter in a GitHub repo, so there is
    no way to tell which an item is without asking. Try `gh issue view`
    first; if that fails, it's either a PR or nothing. Diff stat only --
    the changed-file list plus additions/deletions -- never the full diff
    text (out of scope, see issue #1: same cost whether lupin or the
    dispatched agent fetches it).
    """
    issue_data, issue_error = _run_gh_json(
        ["issue", "view", item, "--json", "body,comments"], repo=repo
    )
    if issue_data is not None:
        return {"kind": "issue", **issue_data}

    pr_data, pr_error = _run_gh_json(
        ["pr", "view", item, "--json", "body,comments,reviews,files"], repo=repo
    )
    if pr_data is None:
        return {"error": f"not a fetchable issue or PR {item!r}: {issue_error}; {pr_error}"}

    files = pr_data.pop("files", [])
    pr_data["diff_stat"] = [
        {"path": f["path"], "additions": f["additions"], "deletions": f["deletions"]}
        for f in files
    ]
    return {"kind": "pr", **pr_data}


def prefetch(items: list[str], *, repo: str | None = None) -> dict:
    """Fetch issue/PR text for every item, keyed by its number (as given).

    A bad item (neither an issue nor a PR) does not fail the whole batch --
    its entry gets {"error": ...} instead, so one typo in an orchestrator's
    list doesn't lose the rest of the prefetch.
    """
    return {item: _fetch_item(item, repo=repo) for item in items}


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
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
    parser.add_argument(
        "--prefetch",
        default=None,
        help="comma-separated issue/PR numbers to fetch instead of routing",
    )
    parser.add_argument(
        "--repo", default=None, help="OWNER/REPO for --prefetch (default: gh's own resolution)"
    )
    args = parser.parse_args(argv)
    if args.prefetch:
        return args
    if not args.category and not args.issue_json:
        parser.error("one of --category or --issue-json is required (unless using --prefetch)")
    if args.category and not args.size:
        parser.error("--category needs --size")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    if args.prefetch:
        items = [item.strip() for item in args.prefetch.split(",") if item.strip()]
        print(json.dumps(prefetch(items, repo=args.repo)))
        return 0
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
