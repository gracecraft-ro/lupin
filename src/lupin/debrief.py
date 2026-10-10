"""Written debrief for one loop stop.

A debrief is one markdown file per loop stop. Facts come from GitHub
(`gh`). Unless the stop was forced, they also come from the ledger and
claims in Redis. Files stay on the machine that stopped the loop.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from html import escape
from pathlib import Path

import redis

from . import claims, ledger, roadmap
from .slots import CoordinatorUnreachable

GH_TIMEOUT = 30.0
DEBRIEF_TIME_LIMIT_S = 10.0  # Time limit for all gh calls in one debrief. Redis reads do not count.
LIST_LIMIT = "200"
REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
FILE_RE = re.compile(r"^\d{8}-\d{6}\.md$")
STAMP_FORMAT = "%Y%m%d-%H%M%S"
EVIDENCE_DIRS = ("docs/", "evidence/")
EVIDENCE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
MAX_EVIDENCE_BYTES = 10 * 1024 * 1024
FAILING_CONCLUSIONS = {"FAILURE", "TIMED_OUT", "STARTUP_FAILURE"}
FAILING_STATES = {"FAILURE", "ERROR"}
CSS = ".debrief img{max-width:100%;height:auto}"


class DebriefError(Exception):
    """A debrief cannot be written or read."""


class GhTimeout(DebriefError):
    """A gh call hit its time limit."""


def _gh(args: list[str], cwd: str | None = None, timeout: float = GH_TIMEOUT):
    try:
        proc = subprocess.run(
            ["gh", *args], cwd=cwd, capture_output=True, text=True,
            check=False, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise GhTimeout(f"gh {' '.join(args[:2])} failed: {exc}") from exc
    except OSError as exc:
        raise DebriefError(f"gh {' '.join(args[:2])} failed: {exc}") from exc
    if proc.returncode != 0:
        raise DebriefError(
            f"gh {' '.join(args[:2])} exited {proc.returncode}: {proc.stderr.strip()}"
        )
    try:
        return json.loads(proc.stdout or "null")
    except json.JSONDecodeError as exc:
        raise DebriefError(f"gh {' '.join(args[:2])} sent bad JSON") from exc


class _TimeLimit:
    """Time left for the gh calls of one debrief. Redis reads do not use it."""

    def __init__(self, seconds: float):
        self.left = seconds

    def gh(self, args: list[str], cwd: str | None = None):
        """Run gh with the time left. Raise `GhTimeout` when none is left."""
        if self.left <= 0:
            raise GhTimeout(f"gh {' '.join(args[:2])} not run: time limit reached")
        started = time.monotonic()
        try:
            return _gh(args, cwd=cwd, timeout=min(GH_TIMEOUT, self.left))
        finally:
            self.left -= time.monotonic() - started


def _list(time_limit: _TimeLimit, args: list[str]) -> list[dict] | None:
    """Return the items. None means the time limit cut the call."""
    try:
        return time_limit.gh(args + ["--limit", LIST_LIMIT]) or []
    except GhTimeout:
        return None


def _cut(section: str, name: str, items: list | None) -> list[str]:
    if items is None:
        return [f"- Not collected: time limit reached. {section}: {name} list."]
    if len(items) < int(LIST_LIMIT):
        return []
    return [f"- {section}: {name} list cut at {LIST_LIMIT} items. Some items may be missing."]


def _parse(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _in_window(value, start: datetime, end: datetime) -> bool:
    when = _parse(value)
    return when is not None and start <= when <= end


def _well_formed(event: dict) -> bool:
    """Return False when a field the debrief reads has the wrong type."""
    for key in ("next", "decisions"):
        texts = event.get(key, [])
        if not isinstance(texts, list) or not all(isinstance(text, str) for text in texts):
            return False
    return True


def _failing_checks(pr: dict) -> list[str]:
    names = []
    for check in pr.get("statusCheckRollup") or []:
        if check.get("conclusion") in FAILING_CONCLUSIONS or check.get("state") in FAILING_STATES:
            names.append(check.get("name") or check.get("context") or "unnamed check")
    return names


def _attachments(text: str | None) -> list[str]:
    found = []
    for match in roadmap.IMAGE.finditer(text or ""):
        attachment = roadmap.github_attachment_id(match.group(2))
        if attachment and attachment not in found:
            found.append(attachment)
    return found


def build_markdown(
    full_name: str,
    start: datetime,
    end: datetime,
    *,
    forced: bool = False,
    events: list[dict] | None = None,
    ledger_note: str | None = None,
    claimed: set[int] | None = None,
    time_limit: _TimeLimit | None = None,
) -> str:
    """Return the debrief for `full_name` over `[start, end]` as markdown.

    `events` is None when the ledger was not read. `claimed` is None when
    claims were not read. A forced stop uses GitHub facts only. A section
    whose gh call the time limit cut says so.
    """
    day = start.strftime("%Y-%m-%d")
    repo = ["--repo", full_name]
    time_limit = _TimeLimit(DEBRIEF_TIME_LIMIT_S) if time_limit is None else time_limit
    window_events = (
        [] if forced or events is None
        else [
            e for e in events
            if _well_formed(e) and _in_window(e.get("timestamp"), start, end)
        ]
    )
    lines = [f"# Debrief: {full_name}", "", "## Window", f"- From: {_iso(start)}", f"- To: {_iso(end)}"]
    lines.append("- Stop: forced, no handoff" if forced else "- Stop: normal")
    lines.append("")

    merged_all = _list(time_limit, ["pr", "list", *repo, "--state", "merged",
                                "--search", f"merged:>={day}",
                                "--json", "number,title,mergedAt,mergeCommit"])
    merged = [pr for pr in merged_all or [] if _in_window(pr.get("mergedAt"), start, end)]
    closed_all = _list(time_limit, ["issue", "list", *repo, "--state", "closed",
                                "--search", f"closed:>={day}",
                                "--json", "number,title,closedAt"])
    closed = [issue for issue in closed_all or [] if _in_window(issue.get("closedAt"), start, end)]
    lines.append("## Shipped")
    for pr in merged:
        commit = (pr.get("mergeCommit") or {}).get("oid", "")[:7]
        lines.append(f"- PR #{pr['number']}: {pr['title']} (merged {pr['mergedAt']}, commit {commit})")
    for issue in closed:
        lines.append(f"- Issue #{issue['number']}: {issue['title']} (closed {issue['closedAt']})")
    if not merged and not closed and merged_all is not None and closed_all is not None:
        lines.append("- None.")
    lines += _cut("Shipped", "merged PR", merged_all) + _cut("Shipped", "closed issue", closed_all)
    lines.append("")

    lines.append("## Follow-up tasks")
    if forced:
        lines.append("- Forced stop. Ledger not read.")
    elif events is None:
        lines.append(f"- {ledger_note or 'No ledger read.'}")
    else:
        for event in window_events:
            for text in event.get("next", []):
                prefix = f"#{event['issue']}: " if event.get("issue") else ""
                lines.append(f"- {prefix}{text}")
        if not any(event.get("next") for event in window_events):
            lines.append("- None.")
    lines.append("")

    failing = []
    open_prs = _list(time_limit, ["pr", "list", *repo, "--state", "open",
                              "--json", "number,title,statusCheckRollup"])
    for pr in open_prs or []:
        names = _failing_checks(pr)
        if names:
            failing.append((pr, names))
    blocked = _list(time_limit, ["issue", "list", *repo, "--state", "open", "--label", "blocked",
                             "--json", "number,title"])
    decisions = [text for event in window_events for text in event.get("decisions", [])]
    risk = []
    for pr, names in failing:
        risk.append(f"- PR #{pr['number']}: {pr['title']} (failing: {', '.join(names)})")
    for issue in blocked or []:
        risk.append(f"- Issue #{issue['number']}: {issue['title']} (labelled blocked)")
    for text in decisions:
        risk.append(f"- Decision: {text}")
    lines.append("## Risk")
    lines.append("Derived from GitHub facts and ledger decisions in the window.")
    lines += risk or (["- None."] if open_prs is not None and blocked is not None else [])
    lines += _cut("Risk", "open PR", open_prs) + _cut("Risk", "blocked issue", blocked)
    lines.append("")

    ready = _list(time_limit, ["issue", "list", *repo, "--state", "open", "--label", "ready",
                           "--json", "number,title"])
    lines.append("## Opportunities")
    if forced:
        lines.append("Forced stop. Claims not read.")
    elif claimed is None:
        lines.append("Claims not read. Listed issues may already be claimed.")
    unclaimed = [issue for issue in ready or [] if not claimed or issue["number"] not in claimed]
    for issue in unclaimed:
        lines.append(f"- Issue #{issue['number']}: {issue['title']}")
    if not unclaimed and ready is not None:
        lines.append("- None.")
    lines += _cut("Opportunities", "ready issue", ready)
    lines.append("")

    lines.append("## Evidence")
    seen = []
    cut = []
    complete = True
    for kind, args in (
        ("Issue", ["issue", "list", *repo, "--state", "all",
                   "--search", f"updated:>={day}",
                   "--json", "number,title,body,comments,updatedAt"]),
        ("PR", ["pr", "list", *repo, "--state", "all",
                "--search", f"updated:>={day}",
                "--json", "number,title,body,comments,updatedAt"]),
    ):
        items = _list(time_limit, args)
        cut += _cut("Evidence", kind, items)
        complete = complete and items is not None
        for item in items or []:
            texts = []
            if _in_window(item.get("updatedAt"), start, end):
                texts.append(item.get("body"))
            texts += [c.get("body") for c in item.get("comments") or []
                      if _in_window(c.get("createdAt"), start, end)]
            for text in texts:
                for attachment in _attachments(text):
                    if attachment in seen:
                        continue
                    seen.append(attachment)
                    url = f"https://github.com/user-attachments/assets/{attachment}"
                    lines.append(f"- {kind} #{item['number']}: ![{kind} {item['number']} image]({url})")
    if not seen and complete:
        lines.append("- None.")
    lines += cut
    lines.append("")
    return "\n".join(lines)


def write_debrief(
    root: Path,
    repo: str,
    checkout: Path,
    started_at: str | None,
    *,
    forced: bool = False,
) -> Path:
    """Write one debrief under `root/debriefs/<repo>/` and return its path.

    Raises `DebriefError` or a Redis error. Nothing is written then. The gh
    calls share one time limit. A section that the time limit cut says so in the file.
    """
    if not REPO_RE.fullmatch(repo or ""):
        raise DebriefError(f"invalid repo name {repo!r}")
    start = _parse(started_at)
    if start is None:
        raise DebriefError(f"loop state for {repo} has no start time")
    if not checkout.is_dir():
        raise DebriefError(f"no checkout at {checkout}")
    end = datetime.now(timezone.utc)
    time_limit = _TimeLimit(DEBRIEF_TIME_LIMIT_S)
    view = time_limit.gh(["repo", "view", "--json", "nameWithOwner"], cwd=str(checkout))
    full_name = view["nameWithOwner"]

    events = None
    claimed = None
    ledger_note = None
    if not forced:
        try:
            events = ledger.read_events(full_name, limit=None)
        except (CoordinatorUnreachable, redis.exceptions.RedisError, ValueError, KeyError) as exc:
            ledger_note = f"Ledger unavailable: {exc.__cause__ or exc}"
        try:
            held = claims.claims_for([full_name])
            claimed = {
                int(key.rsplit("#", 1)[1]) for key in held if key.startswith(f"{full_name}#")
            }
        except (CoordinatorUnreachable, redis.exceptions.RedisError, ValueError):
            claimed = None

    text = build_markdown(
        full_name, start, end, forced=forced, events=events,
        ledger_note=ledger_note, claimed=claimed, time_limit=time_limit,
    )
    folder = root / "debriefs" / repo
    folder.mkdir(parents=True, exist_ok=True, mode=0o750)
    path = folder / f"{end.strftime(STAMP_FORMAT)}.md"
    fd, temporary = tempfile.mkstemp(prefix=".debrief.", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(temporary, 0o640)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return path


def list_debriefs(root: Path) -> list[tuple[str, str]]:
    """Return (repo, file name) for each debrief, newest first."""
    base = root / "debriefs"
    if not base.is_dir():
        return []
    found = []
    for folder in base.iterdir():
        if not REPO_RE.fullmatch(folder.name) or not folder.is_dir():
            continue
        for path in folder.iterdir():
            if FILE_RE.fullmatch(path.name) and path.is_file():
                found.append((folder.name, path.name))
    return sorted(found, key=lambda item: (item[1], item[0]), reverse=True)


def read_debrief(root: Path, repo: str, name: str) -> str:
    """Return the markdown of one debrief. Raises `DebriefError` if absent."""
    if not REPO_RE.fullmatch(repo or "") or not FILE_RE.fullmatch(name or ""):
        raise DebriefError("no such debrief")
    path = root / "debriefs" / repo / name
    if not path.is_file():
        raise DebriefError("no such debrief")
    return path.read_text(encoding="utf-8")


def _inline(text: str) -> str:
    parts = []
    position = 0
    for match in roadmap.IMAGE.finditer(text):
        parts.append(escape(text[position:match.start()]))
        attachment = roadmap.github_attachment_id(match.group(2))
        if attachment:
            alt = escape(match.group(1), quote=True)
            parts.append(f"<img src='/image?id={attachment}' alt='{alt}' loading=lazy>")
        else:
            parts.append(escape(match.group(0)))
        position = match.end()
    parts.append(escape(text[position:]))
    return "".join(parts)


def render_html(markdown: str) -> str:
    """Render the debrief markdown subset as HTML.

    Headings, bullets, paragraphs and GitHub attachment images only. All
    other text is escaped. Links show as text.
    """
    out = []
    in_list = False
    for line in markdown.splitlines():
        if line.startswith("- "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{_inline(line[2:])}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if line.startswith("# "):
            out.append(f"<h1>{_inline(line[2:])}</h1>")
        elif line.startswith("## "):
            out.append(f"<h2>{_inline(line[3:])}</h2>")
        elif line.strip():
            out.append(f"<p>{_inline(line)}</p>")
    if in_list:
        out.append("</ul>")
    return "".join(out)


def read_evidence(checkout: Path, rel: str) -> tuple[bytes, str] | None:
    """Return (bytes, content type) for an image under `docs/` or `evidence/`.

    Returns None for an absolute path, a `://` or `..` path, or another extension.
    Also None when the path is outside those folders or resolves outside the checkout.
    """
    if not rel or rel.startswith("/") or "://" in rel or ".." in rel or "\\" in rel or "\0" in rel:
        return None
    content_type = EVIDENCE_TYPES.get(Path(rel).suffix.lower())
    if content_type is None or not rel.startswith(EVIDENCE_DIRS):
        return None
    try:
        root = checkout.resolve(strict=True)
        target = (root / rel).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not target.is_relative_to(root):
        return None
    if not target.relative_to(root).as_posix().startswith(EVIDENCE_DIRS) or not target.is_file():
        return None
    if target.stat().st_size > MAX_EVIDENCE_BYTES:
        return None
    return target.read_bytes(), content_type
