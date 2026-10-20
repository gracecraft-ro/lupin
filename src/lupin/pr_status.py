"""`lupin pr-status` -- one read-only view of every open pull request.

The whole queue comes from one `gh pr list` call. The command never makes one
`gh pr view` call per pull request. The issue asks for one view without
repeated `gh` calls. `review-route --prefetch` is the opposite case: it
fetches one item that the caller names. A queue view would have to prefetch
every item first, then fetch it again for the table. One list call returns
the same facts for every open pull request.

The command reads claims from Redis. It does not fail when Redis is
unavailable. The claim column shows `unknown` and a warning follows the
table instead.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

import redis

from . import claims
from . import machines
from .slots import CoordinatorUnreachable

PR_FIELDS = (
    "number,title,body,isDraft,headRefName,headRefOid,mergeable,"
    "reviewDecision,reviews,comments,statusCheckRollup,reviewRequests"
)

# Same redaction as roadmap.py's _run_json: a `gh` error can echo a token
# back (for example, in a URL) if auth is misconfigured.
_TOKEN_RE = re.compile(r"(?:gh[oprsu]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)")

FAILING_CHECKS = frozenset(
    {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"}
)
PENDING_CHECKS = frozenset({"PENDING", "QUEUED", "IN_PROGRESS", "WAITING", "EXPECTED"})

# An informal approval is a comment line that says "Review verdict: APPROVE"
# or "Verdict: ship" (case-insensitive). The matched line is kept in the
# output so a human can check the wording.
INFORMAL_RE = re.compile(
    r"^\s*(?:review\s+verdict|verdict)\s*:\s*(?:approve|ship)\b", re.IGNORECASE
)

PREVIEW_CHARS = 160


def _redact(text: str) -> str:
    return _TOKEN_RE.sub("[redacted]", text)


def _run_gh(argv: list[str], timeout: int = 60):
    """Run `gh` and return `(stdout, error)`; exactly one is None."""
    try:
        result = subprocess.run(
            ["gh", *argv], capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError:
        return None, "gh is not installed"
    except OSError as error:
        return None, f"gh could not start: {error}"
    except subprocess.TimeoutExpired:
        return None, f"gh timed out after {timeout} seconds"
    if result.returncode:
        error = (result.stderr or result.stdout or "gh failed").strip()
        return None, _redact(error)
    return result.stdout, None


def _run_gh_json(argv: list[str], timeout: int = 60):
    """Run `gh` and decode its JSON; return `(data, error)`."""
    output, error = _run_gh(argv, timeout=timeout)
    if error:
        return None, error
    try:
        return json.loads(output), None
    except json.JSONDecodeError as error:
        return None, f"invalid JSON from gh: {error}"


def _resolve_repo():
    """Ask `gh` for `OWNER/REPO` in the current directory."""
    output, error = _run_gh(
        ["repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"]
    )
    if error:
        return None, error
    repo = (output or "").strip()
    if not repo:
        return None, "gh did not name the current repository"
    return repo, None


def _fetch_prs(repo: str):
    """Fetch every open pull request with one `gh pr list` call."""
    return _run_gh_json(
        [
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--limit",
            "100",
            "--json",
            PR_FIELDS,
        ]
    )


def _read_claims(repo: str):
    """Return `(claims, warning)`; a Redis failure never fails the command."""
    try:
        connection = machines.resolve_connection()
        return claims.claims_for([repo], **connection), None
    except (
        CoordinatorUnreachable,
        redis.exceptions.NoPermissionError,
        redis.exceptions.AuthenticationError,
    ) as error:
        return {}, f"claim data is unavailable: {error}. Showing claim=unknown."


def _login(entry: dict | None) -> str:
    if not isinstance(entry, dict):
        return "?"
    return entry.get("login") or "?"


def _when(value) -> str:
    return str(value or "")


def _preview(text, *, full: bool) -> str:
    """Collapse whitespace, then cut to a short one-line preview."""
    collapsed = " ".join(str(text or "").split())
    if full or len(collapsed) <= PREVIEW_CHARS:
        return collapsed
    return collapsed[: PREVIEW_CHARS - 1].rstrip() + "…"


def _merge(pr: dict) -> str:
    value = pr.get("mergeable")
    if value == "CONFLICTING":
        return "conflict"
    if value == "MERGEABLE":
        return "clean"
    return "unknown"


def _checks(pr: dict) -> str:
    rollup = pr.get("statusCheckRollup") or []
    pending = False
    for check in rollup:
        if not isinstance(check, dict):
            pending = True
            continue
        state = check.get("conclusion") or check.get("state")
        if state in FAILING_CHECKS:
            return "fail"
        if state in PENDING_CHECKS or not state:
            pending = True
    if pending:
        return "pending"
    return "pass" if rollup else "none"


def _latest_reviews(reviews: list) -> list[dict]:
    """Keep the newest review for each reviewer."""
    newest: dict[str, dict] = {}
    for review in reviews or []:
        if not isinstance(review, dict):
            continue
        login = _login(review.get("author"))
        when = _when(review.get("submittedAt"))
        if login not in newest or when >= _when(newest[login].get("submittedAt")):
            newest[login] = review
    return sorted(
        (
            {
                "login": _login(review.get("author")),
                "state": review.get("state") or "none",
                "submitted_at": _when(review.get("submittedAt")),
            }
            for review in newest.values()
        ),
        key=lambda row: row["submitted_at"],
    )


def _review_commit(review: dict) -> str:
    commit = review.get("commit_id")
    if commit:
        return str(commit)
    nested = review.get("commit")
    if isinstance(nested, dict):
        return str(nested.get("oid") or "")
    return ""


def _approval(pr: dict) -> tuple[bool, str]:
    """Return `(approval_at_head, text value)` for the newest approval."""
    approvals = [
        review
        for review in pr.get("reviews") or []
        if isinstance(review, dict) and review.get("state") == "APPROVED"
    ]
    if not approvals:
        return False, "none"
    newest = max(approvals, key=lambda review: _when(review.get("submittedAt")))
    at_head = bool(pr.get("headRefOid")) and _review_commit(newest) == pr.get("headRefOid")
    return at_head, "head" if at_head else "stale"


def _informal_approvals(pr: dict) -> list[dict]:
    found = []
    for comment in pr.get("comments") or []:
        if not isinstance(comment, dict):
            continue
        body = comment.get("body") or ""
        for line in str(body).splitlines():
            match = INFORMAL_RE.match(line)
            if match:
                found.append(
                    {
                        "login": _login(comment.get("author")),
                        "created_at": _when(comment.get("createdAt")),
                        "line": line.strip(),
                    }
                )
    return found


def _latest_comment(pr: dict, *, full: bool) -> dict | None:
    comments = [c for c in pr.get("comments") or [] if isinstance(c, dict)]
    if not comments:
        return None
    latest = max(comments, key=lambda comment: _when(comment.get("createdAt")))
    return {
        "login": _login(latest.get("author")),
        "created_at": _when(latest.get("createdAt")),
        "body": _preview(latest.get("body"), full=full),
    }


def _review_requests(pr: dict) -> list[str]:
    names = []
    for request in pr.get("reviewRequests") or []:
        if not isinstance(request, dict):
            continue
        name = request.get("login") or request.get("name") or request.get("slug")
        if name and str(name) not in names:
            names.append(str(name))
    return names


def analyze(pr: dict, repo: str, claim_map: dict | None = None, *, full: bool = False) -> dict:
    """Turn one `gh pr list` entry into the row this command prints."""
    claim_map = claim_map or {}
    target = f"{repo}#{pr.get('number')}"
    claim = (claim_map.get(target) or {}).get("session") or "-"
    approval_at_head, approval = _approval(pr)
    return {
        "number": pr.get("number"),
        "title": pr.get("title") or "",
        "head_ref_name": pr.get("headRefName") or "",
        "head_ref_oid": pr.get("headRefOid") or "",
        "merge": _merge(pr),
        "checks": _checks(pr),
        "review": pr.get("reviewDecision") or "none",
        "approval": approval,
        "approval_at_head": approval_at_head,
        "latest_reviews": _latest_reviews(pr.get("reviews")),
        "informal_approvals": _informal_approvals(pr),
        "comments_count": len(pr.get("comments") or []),
        "latest_comment": _latest_comment(pr, full=full),
        "description": _preview(pr.get("body"), full=full),
        "claim": claim,
        "review_requests": _review_requests(pr),
    }


def render_text(rows: list[dict], warnings: list[str]) -> str:
    lines = ["PR  merge  checks  review  approval  claim  comments  title"]
    for row in rows:
        lines.append(
            f"#{row['number']}  merge={row['merge']}  checks={row['checks']}  "
            f"review={row['review']}  approval={row['approval']}  "
            f"claim={row['claim']}  comments={row['comments_count']}  {row['title']}"
        )
    for warning in warnings:
        lines.append(f"warning: {warning}")
    for row in rows:
        for approval in row["informal_approvals"]:
            lines.append(
                f"#{row['number']}: {approval['login']} {approval['created_at']}: "
                f"{approval['line']}"
            )
    return "\n".join(lines)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="lupin pr-status",
        description="Show every open pull request in one view.",
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="OWNER/REPO (default: let gh resolve the current repository)",
    )
    parser.add_argument("--json", action="store_true", help="print one JSON object")
    parser.add_argument(
        "--full",
        action="store_true",
        help="do not truncate the description or the latest comment",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)

    repo = args.repo
    if repo is None:
        repo, error = _resolve_repo()
        if error:
            print(f"lupin: {_redact(error)}", file=sys.stderr)
            return 1

    prs, error = _fetch_prs(repo)
    if error:
        print(f"lupin: {_redact(error)}", file=sys.stderr)
        return 1
    if not isinstance(prs, list):
        print("lupin: gh returned an unexpected pull request list", file=sys.stderr)
        return 1

    claim_map, claim_warning = _read_claims(repo)
    warnings = [claim_warning] if claim_warning else []
    rows = [
        analyze(pr, repo, claim_map, full=args.full)
        for pr in prs
        if isinstance(pr, dict)
    ]
    # A failed claim read makes every claim "unknown", not "unclaimed".
    if claim_warning:
        for row in rows:
            row["claim"] = "unknown"

    if args.json:
        print(json.dumps({"repo": repo, "prs": rows, "warnings": warnings}))
        return 0

    print(render_text(rows, warnings))
    return 0
