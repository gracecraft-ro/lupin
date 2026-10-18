"""Readable digest of each debrief, shared through Redis (issue #105).

A worker writes a debrief file (`debrief.py`). The files stay on that worker.
This module does two jobs:

1. A model reads one debrief and writes a short digest. `lupin route` picks
   the model. The worker runs it (`claude -p` or `omp -p`).
2. The worker puts the debrief text and its digest in Redis. Any machine that
   runs `lupin serve`, PiHome included, reads them from there.

`sync` does both jobs for each local debrief that Redis does not have yet.
The agent calls it on a timer. A failure never stops the agent.

Screenshots are not copied. Redis holds text only. The copy of a debrief in
Redis names each screenshot and says which machine keeps it.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import socket
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import redis

from . import debrief, roadmap, route, slots_redis

KEY_PREFIX = f"{slots_redis.PREFIX}debrief-digest:"
KEY_TTL_S = 30 * 24 * 60 * 60  # Redis drops an entry after 30 days. The file on the worker stays.
# `sync` ignores a debrief older than this. Its Redis entry may have expired, and a
# missing entry would start a new model call every 30 days for every old debrief.
SYNC_MAX_AGE_S = KEY_TTL_S - 24 * 60 * 60
CATEGORY = "prose"  # `lupin route` category. Prose is what a digest is.
SIZE = "size-s"
LLM_TIMEOUT_S = 300.0
RAW_MAX_BYTES = 60_000  # More UTF-8 bytes than this are cut. One argument may be 128 KB on Linux.
MAX_NEW_PER_SYNC = 3  # Model calls in one `sync`. The rest wait for the next one.
MAX_TRIES = 3  # After this many failed model calls, `sync` stops trying for that debrief.
# A debrief with only `- None.` in all of these sections shows a period with no change.
# `build_markdown` writes `- None.` only when it collected that data. A gap is never "None".
QUIET_SECTIONS = ("Shipped", "Follow-up tasks", "Risk", "Evidence")

_REDIS_ERRORS = (redis.exceptions.RedisError,)  # Includes an ACL denial, which is not a ConnectionError.
MACHINE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_FENCE_RE = re.compile(r"^```[a-z]*\n(.*)\n```$", re.DOTALL)

MAX_IMAGES = 3  # Most images in one digest.
IMAGE_KEY_PREFIX = f"{slots_redis.PREFIX}debrief-image:"
IMAGE_MAX_BYTES = 1_500_000  # A bigger image is not copied to Redis.
IMAGE_RETRY_S = 3600  # After a failed copy, wait this long before another try.
IMAGE_TYPES = {"image/gif", "image/jpeg", "image/png", "image/webp"}  # Same as serve.IMAGE_TYPES.

PROMPT = """\
You write a short digest of one software project debrief for the project owner.
The owner has no time. They do not want a list of pull requests. They can read
the debrief for that. They want the story: what changed, and why it matters.

Facts counted by a program (trust these numbers): {counts}

How to write it:
- Start with a title line (`# `) that says the main thing that happened in
  plain words. Make it specific and lively. Do not start it with "Digest".
- Then write one or two short paragraphs, 80 to 120 words in all (not counting the "Heads up:" line). Put the
  work into two to four themes (for example "PDF export fixes", "test
  repairs"). Say what each theme means for the product or the people who use it.
- Do not list pull requests or issues one by one. Do not copy their titles.
  Name at most four pull request or issue numbers in the whole text, and only
  for the work that matters most.
- Use short, plain sentences in the active voice. No jargon. No emoji. No hype.
- Use only facts that are in the debrief. Do not add facts.
- Pictures: the debrief may have image lines like `![alt](https://...)`.
  Each picture belongs to the issue or pull request named before it. Choose up
  to {max_images} pictures that fit your story. Copy each one exactly as it is
  written in the debrief. Put it on its own line after the paragraph it
  supports. Never write an image link that is not in the debrief. If there
  is no fitting picture, use none.
- A section may say that data was not collected, timed out, or failed. Missing
  data is not an empty result. Close with one short line that starts with
  "Heads up:" and says in plain words what is missing. If nothing is missing,
  leave that line out.
- Output markdown with only these parts: `# ` for the title, plain
  paragraphs, picture lines, and at most three `- ` list items. No tables,
  no links, no code fences, no other headings.

Debrief for repo `{repo}`, file `{name}`:

BEGIN
{raw}
END
"""


class DigestError(Exception):
    """The model could not write a digest. The debrief is still published."""


class DigestBlocked(DigestError):
    """No model has quota now. This does not count as a failed try."""


def _client(connection: dict):
    """Redis client from a connection dict. A missing key means the default: localhost, no login.

    It waits 1 second at most and does not retry, so a page does not hang when Redis is down.
    """
    return slots_redis.debrief_client(
        connection.get("redis_host"),
        connection.get("redis_port"),
        connection.get("redis_username"),
        connection.get("redis_password"),
    )


def _key(machine: str, repo: str, name: str) -> str:
    return f"{KEY_PREFIX}{machine}:{repo}:{name}"


def strip_images(text: str, machine: str) -> str:
    """Replace each image the page cannot show with plain text.

    A GitHub attachment image stays: the dashboard fetches it by its id. A screenshot
    file stays on `machine`, so its link becomes text.
    """

    def swap(match) -> str:
        if roadmap.github_attachment_id(match.group(2)):
            return match.group(0)
        return f"({match.group(1) or 'screenshot'}: kept on {machine})"

    return debrief.IMAGE_LINK_RE.sub(swap, text)


def limit_images(text: str, raw: str) -> str:
    """Keep only images that the debrief itself has, and at most `MAX_IMAGES`. A model must not invent a URL.

    A removed image leaves a space, not nothing. Without the space, the text on both sides
    can join into a new image link. The loop repeats until the text stops changing.
    """
    allowed = {m.group(2) for m in debrief.IMAGE_LINK_RE.finditer(raw) if roadmap.github_attachment_id(m.group(2))}

    def one_pass(value: str) -> str:
        kept = 0

        def swap(match) -> str:
            nonlocal kept
            if match.group(2) in allowed and kept < MAX_IMAGES:
                kept += 1
                return match.group(0)
            return " "

        return debrief.IMAGE_LINK_RE.sub(swap, value)

    for _ in range(10):  # Each pass only removes text, so this ends. The bound is a safety net.
        after = one_pass(text)
        if after == text:
            break
        text = after
    return text


def count_shipped(raw: str) -> str:
    """Count the items in the Shipped section, for the prompt. A program counts better than a model.

    If the section says that data is missing or cut, the counts are not facts. Say so, so the
    model does not report "0 merged" for a gap.
    """
    prs = issues = commits = 0
    gap = False
    in_shipped = False
    for line in raw.splitlines():
        if line.startswith("## "):
            in_shipped = line[3:].strip() == "Shipped"
        elif in_shipped:
            prs += line.startswith("- PR #")
            issues += line.startswith("- Issue #")
            commits += line.startswith("- Commit ")
            gap = gap or line.startswith("- Not collected") or "list cut at" in line
    if gap:
        return "unknown. The Shipped data is missing or cut, so do not state any count"
    return f"{prs} pull requests merged, {issues} issues closed, {commits} other commits"


def is_quiet(raw: str) -> bool:
    """True if the debrief shows a period with no change and no missing data."""
    bullets: dict[str, list[str]] = {}
    section = None
    for line in raw.splitlines():
        if line.startswith("## "):
            section = line[3:].strip()
            if section in bullets:  # Ledger text with a newline can fake a heading. Do not trust it.
                return False
            bullets[section] = []
        elif section is not None and line.startswith("- "):
            bullets[section].append(line.rstrip())
    return all(bullets.get(name) == ["- None."] for name in QUIET_SECTIONS)


def pick_model() -> tuple[str, str]:
    """Return (model, effort) from `lupin route`. Raise `DigestError` if none can run."""
    try:
        choice = route.route(CATEGORY, SIZE)
    except Exception as exc:  # route reads live quota. Any failure there is not fatal.
        raise DigestError(f"route failed: {exc}") from exc
    if "wait_seconds" in choice:
        raise DigestBlocked("every model for digests is out of quota")
    model = choice["model"]
    if model.startswith(("bmo:", "local:")):
        raise DigestError(f"model {model} cannot run a digest here")
    return model, choice["effort"]


def _argv(model: str, effort: str, prompt: str) -> list[str]:
    """A `provider/model` selector runs with `omp`. A bare name is a Claude alias.

    The prompt holds text that other people wrote (PR titles, commit messages). So the
    model gets no tools: no built-in tools, no MCP servers, no extensions, no skills.
    `--auto-approve` is not used, because no tool call should happen.
    `omp` has no flag that turns MCP off. It may still connect MCP servers, but with
    no `--auto-approve` a tool call has no one to approve it. `route` gives a Claude
    alias for this category today, so the `omp` branch is not used yet.

    `claude --tools` takes a list. A flag must follow `--tools ""`, or `claude`
    reads the prompt as a tool name and fails with "Input must be provided".
    """
    if "/" in model:
        return [
            "omp", "-p", "--model", model, "--thinking", effort, "--no-tools",
            "--no-extensions", "--no-skills", "--no-rules", "--no-session",
            "--max-time", str(int(LLM_TIMEOUT_S)), prompt,
        ]
    return [
        "claude", "-p", "--model", model, "--effort", effort, "--tools", "",
        "--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence", prompt,
    ]


def make_digest(repo: str, name: str, raw: str) -> tuple[str, str]:
    """Return (digest markdown, model). Raise `DigestError` on any failure."""
    model, effort = pick_model()
    cut = raw.encode("utf-8")[:RAW_MAX_BYTES].decode("utf-8", errors="ignore")
    prompt = PROMPT.format(
        repo=repo, name=name, raw=cut, counts=count_shipped(raw), max_images=MAX_IMAGES
    )
    argv = _argv(model, effort, prompt)
    try:
        with tempfile.TemporaryDirectory() as cwd:
            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=LLM_TIMEOUT_S, cwd=cwd
            )
    except FileNotFoundError:
        raise DigestError(f"{argv[0]} not found") from None
    except subprocess.TimeoutExpired:
        raise DigestError(f"{argv[0]} timed out after {LLM_TIMEOUT_S:.0f}s") from None
    except (OSError, ValueError) as exc:  # No permission, argument too long, a NUL byte in the prompt.
        raise DigestError(f"{argv[0]} could not start: {exc}") from None
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:300]
        raise DigestError(f"{argv[0]} exited {proc.returncode}: {detail}")
    text = proc.stdout.strip()
    fenced = _FENCE_RE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    if not text:
        raise DigestError(f"{argv[0]} gave no text")
    return limit_images(text, raw), model


def _publish(client, machine: str, repo: str, name: str, raw: str, **fields) -> dict:
    entry = {
        "machine": machine,
        "repo": repo,
        "name": name,
        "raw": strip_images(raw, machine),
        "digest": None,
        "model": None,
        "error": None,
        "quiet": False,
        "tries": 0,
        "published_at": datetime.now(timezone.utc).isoformat(),
        **fields,
    }
    if entry["digest"]:
        entry["digest"] = strip_images(entry["digest"], machine)
    slots_redis._call_with_retry(
        lambda: client.set(_key(machine, repo, name), json.dumps(entry), ex=KEY_TTL_S)
    )
    return entry


def _valid(entry) -> bool:
    """True if `entry` is a well-formed stored entry. Redis is shared, so check every field."""
    if not isinstance(entry, dict):
        return False
    texts = all(isinstance(entry.get(f), str) for f in ("machine", "repo", "name", "raw"))
    optional = all(entry.get(f) is None or isinstance(entry[f], str) for f in ("digest", "model", "error"))
    tries = entry.get("tries", 0)
    return bool(
        texts
        and optional
        and isinstance(entry.get("quiet", False), bool)
        and isinstance(tries, int)
        and not isinstance(tries, bool)
        and tries >= 0
        and MACHINE_RE.fullmatch(entry["machine"])
        and debrief.REPO_RE.fullmatch(entry["repo"])
        and debrief.FILE_RE.fullmatch(entry["name"])
    )


def _parse(raw) -> dict | None:
    if raw is None:
        return None
    try:
        entry = json.loads(raw)
    except ValueError:
        return None
    return entry if _valid(entry) else None


def _get(client, key: str) -> dict | None:
    return _parse(slots_redis._call_with_retry(lambda: client.get(key)))


def _image_ids(text: str) -> list[str]:
    """GitHub attachment ids in the image links of `text`, in order, with no repeats."""
    ids: list[str] = []
    for match in debrief.IMAGE_LINK_RE.finditer(text):
        attachment_id = roadmap.github_attachment_id(match.group(2))
        if attachment_id and attachment_id.lower() not in ids:
            ids.append(attachment_id.lower())
    return ids


def _fetch_image(attachment_id: str):
    """Fetch an image from GitHub with this machine's `gh` login. Return (bytes, type) or None."""
    from . import serve  # Local import: serve imports this module.

    return serve.github_attachment(attachment_id)


def store_images(client, text: str) -> None:
    """Copy each image of a digest to Redis, so a machine without GitHub access can show it.

    A private repo's image needs the worker's `gh` login. PiHome has none for it. An image that
    is already stored is skipped. A failed or too big copy leaves a marker for `IMAGE_RETRY_S`.
    """
    for attachment_id in _image_ids(text):
        key = IMAGE_KEY_PREFIX + attachment_id
        if slots_redis._call_with_retry(lambda: client.ttl(key)) != -2:  # -2: no such key.
            continue
        try:
            got = _fetch_image(attachment_id)
        except Exception as exc:  # A bad image must not stop the others.
            print(f"lupin digest: image {attachment_id}: {exc}", file=sys.stderr)
            got = None
        if got and got[1] in IMAGE_TYPES and len(got[0]) <= IMAGE_MAX_BYTES:
            payload = {"type": got[1], "data": base64.b64encode(got[0]).decode("ascii")}
            ttl = KEY_TTL_S
        else:
            payload, ttl = {"missing": True}, IMAGE_RETRY_S
        slots_redis._call_with_retry(lambda: client.set(key, json.dumps(payload), ex=ttl))


def read_image(attachment_id: str, **connection) -> tuple[bytes, str] | None:
    """Return (bytes, content type) of a copied image, or None. Redis is shared, so check it all."""
    if not roadmap.ATTACHMENT_ID.fullmatch(attachment_id or ""):
        return None
    try:
        raw = slots_redis._call_with_retry(
            lambda: _client(connection).get(IMAGE_KEY_PREFIX + attachment_id.lower())
        )
    except _REDIS_ERRORS:
        return None
    try:
        payload = json.loads(raw) if raw else None
        if not isinstance(payload, dict) or payload.get("type") not in IMAGE_TYPES:
            return None
        data = base64.b64decode(payload.get("data") or "", validate=True)
    except (ValueError, TypeError, binascii.Error):
        return None
    return (data, payload["type"]) if data and len(data) <= IMAGE_MAX_BYTES else None


def _too_old(name: str, now: datetime) -> bool:
    """True if the debrief is too old to publish. Its Redis entry would expire and come back."""
    try:
        when = datetime.strptime(name[:15], debrief.STAMP_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return (now - when).total_seconds() > SYNC_MAX_AGE_S


def sync(
    root: Path,
    connection: dict,
    *,
    machine: str | None = None,
    limit: int = MAX_NEW_PER_SYNC,
    make=None,
    now: datetime | None = None,
) -> int:
    """Publish each local debrief that Redis lacks, or that has no digest yet.

    Newest debriefs go first. At most `limit` model calls run. A debrief past
    that limit is published without a digest and gets one in a later call.
    A quiet debrief (`is_quiet`) is published with no digest and takes no model call.
    A debrief older than `SYNC_MAX_AGE_S` is skipped. A problem with one debrief
    is printed and does not stop the others. A Redis problem ends the call.
    Returns the number of digests written.
    """
    machine = machine or socket.gethostname()
    make = make or make_digest  # Looked up here, not in the signature, so a test can replace it.
    now = now or datetime.now(timezone.utc)
    written = 0
    calls = 0
    blocked = False
    try:
        client = _client(connection)
        for repo, name in debrief.list_debriefs(root):
            if _too_old(name, now):
                continue
            key = _key(machine, repo, name)
            try:
                current = _get(client, key)
                if current and current.get("digest"):
                    store_images(client, current["digest"])  # Also fills in digests made before this existed.
                    continue
                if current and (current.get("quiet") or current.get("tries", 0) >= MAX_TRIES):
                    continue
                raw = debrief.read_debrief(root, repo, name)
                tries = current.get("tries", 0) if current else 0
                if is_quiet(raw):  # Nothing changed. No model call and no digest.
                    _publish(client, machine, repo, name, raw, quiet=True)
                    continue
                if blocked or calls >= limit:
                    if current is None:
                        _publish(client, machine, repo, name, raw)
                    continue
                calls += 1
                try:
                    text, model = make(repo, name, raw)
                except DigestBlocked as exc:
                    blocked = True  # Every other call would be blocked too.
                    _publish(client, machine, repo, name, raw, error=str(exc), tries=tries)
                except DigestError as exc:
                    _publish(client, machine, repo, name, raw, error=str(exc), tries=tries + 1)
                else:
                    entry = _publish(client, machine, repo, name, raw, digest=text, model=model, tries=tries + 1)
                    written += 1
                    store_images(client, entry["digest"])
            except _REDIS_ERRORS:
                raise
            except Exception as exc:  # One bad debrief must not stop the older ones.
                print(f"lupin digest: skipped {repo}/{name}: {exc}", file=sys.stderr)
    except _REDIS_ERRORS as exc:
        print(f"lupin digest: Redis problem: {exc}", file=sys.stderr)
    return written


def read_all(**connection) -> list[dict]:
    """Return every published entry, newest first. Return an empty list if Redis fails."""
    try:
        client = _client(connection)
        keys = slots_redis._call_with_retry(
            lambda: list(client.scan_iter(match=f"{KEY_PREFIX}*", count=slots_redis.SCAN_COUNT))
        )
        if not keys:
            return []

        def fetch():
            pipe = client.pipeline(transaction=False)  # One round trip for all keys.
            for key in keys:
                pipe.get(key)
            return pipe.execute()

        values = slots_redis._call_with_retry(fetch)
    except _REDIS_ERRORS:
        return []
    entries = [entry for entry in map(_parse, values) if entry]
    return sorted(entries, key=lambda e: (debrief._newest_first((e["repo"], e["name"])), e["machine"]), reverse=True)


def read_one(machine: str, repo: str, name: str, **connection) -> dict | None:
    """Return one published entry, or None."""
    if not (
        MACHINE_RE.fullmatch(machine or "")
        and debrief.REPO_RE.fullmatch(repo or "")
        and debrief.FILE_RE.fullmatch(name or "")
    ):
        return None
    try:
        return _get(_client(connection), _key(machine, repo, name))
    except _REDIS_ERRORS:
        return None
