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

import json
import re
import socket
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import redis

from . import debrief, route, slots_redis

KEY_PREFIX = f"{slots_redis.PREFIX}debrief-digest:"
KEY_TTL_S = 30 * 24 * 60 * 60  # Redis drops an entry after 30 days. The file on the worker stays.
CATEGORY = "prose"  # `lupin route` category. Prose is what a digest is.
SIZE = "size-s"
LLM_TIMEOUT_S = 300.0
RAW_MAX_CHARS = 60_000  # More text than this is cut before it goes to the model.
MAX_NEW_PER_SYNC = 3  # Model calls in one `sync`. The rest wait for the next one.
MAX_TRIES = 3  # After this many failed model calls, `sync` stops trying for that debrief.

_REDIS_ERRORS = (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError)
MACHINE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_FENCE_RE = re.compile(r"^```[a-z]*\n(.*)\n```$", re.DOTALL)

PROMPT = """\
You write a short digest of one software project debrief. A person with no
time reads your digest. The debrief is below between the lines BEGIN and END.

Rules:
- Use only facts that are in the debrief. Do not add facts.
- A section may say that data was not collected, timed out, or failed. Then
  say in the digest that the data is missing. Do not say that nothing
  happened. Missing data is not an empty result.
- Write short, plain sentences. Do not use jargon.
- Output markdown with only these parts: `# ` for the title, `## ` for
  headings, `- ` for list items, and plain paragraphs. No tables, no links,
  no images, no code fences.
- Use these headings, in this order. Leave out a heading that has no content:
  `## Summary` (two or three sentences), `## Shipped`, `## Needs attention`,
  `## Next`.

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
    """Redis client from a connection dict. A missing key means the default: localhost, no login."""
    return slots_redis._client(
        connection.get("redis_host"),
        connection.get("redis_port"),
        connection.get("redis_username"),
        connection.get("redis_password"),
    )


def _key(machine: str, repo: str, name: str) -> str:
    return f"{KEY_PREFIX}{machine}:{repo}:{name}"


def strip_images(text: str, machine: str) -> str:
    """Replace each image link with plain text. The image file stays on `machine`."""
    return debrief.IMAGE_LINK_RE.sub(
        lambda m: f"({m.group(1) or 'screenshot'}: kept on {machine})", text
    )


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
    """A `provider/model` selector runs with `omp`. A bare name is a Claude alias. No tools are allowed."""
    if "/" in model:
        return [
            "omp", "-p", "--model", model, "--thinking", effort, "--tools", "",
            "--no-session", "--auto-approve", "--max-time", str(int(LLM_TIMEOUT_S)), prompt,
        ]
    return [
        "claude", "-p", "--model", model, "--effort", effort, "--tools", "",
        "--no-session-persistence", prompt,
    ]


def make_digest(repo: str, name: str, raw: str) -> tuple[str, str]:
    """Return (digest markdown, model). Raise `DigestError` on any failure."""
    model, effort = pick_model()
    prompt = PROMPT.format(repo=repo, name=name, raw=raw[:RAW_MAX_CHARS])
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
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:300]
        raise DigestError(f"{argv[0]} exited {proc.returncode}: {detail}")
    text = proc.stdout.strip()
    fenced = _FENCE_RE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    if not text:
        raise DigestError(f"{argv[0]} gave no text")
    return text, model


def _publish(client, machine: str, repo: str, name: str, raw: str, **fields) -> dict:
    entry = {
        "machine": machine,
        "repo": repo,
        "name": name,
        "raw": strip_images(raw, machine),
        "digest": None,
        "model": None,
        "error": None,
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


def _get(client, key: str) -> dict | None:
    raw = slots_redis._call_with_retry(lambda: client.get(key))
    if raw is None:
        return None
    try:
        entry = json.loads(raw)
    except ValueError:
        return None
    return entry if isinstance(entry, dict) else None


def sync(
    root: Path,
    connection: dict,
    *,
    machine: str | None = None,
    limit: int = MAX_NEW_PER_SYNC,
    make=make_digest,
) -> int:
    """Publish each local debrief that Redis lacks, or that has no digest yet.

    Newest debriefs go first. At most `limit` model calls run. A debrief past
    that limit is published without a digest and gets one in a later call.
    Returns the number of digests written. Raises nothing for a Redis outage.
    """
    machine = machine or socket.gethostname()
    written = 0
    try:
        client = _client(connection)
        for repo, name in debrief.list_debriefs(root):
            key = _key(machine, repo, name)
            current = _get(client, key)
            if current and (current.get("digest") or current.get("tries", 0) >= MAX_TRIES):
                continue
            raw = debrief.read_debrief(root, repo, name)
            tries = current.get("tries", 0) if current else 0
            if written >= limit:
                if current is None:
                    _publish(client, machine, repo, name, raw)
                continue
            try:
                text, model = make(repo, name, raw)
            except DigestBlocked as exc:
                _publish(client, machine, repo, name, raw, error=str(exc), tries=tries)
                written = limit  # Every other call would be blocked too.
            except DigestError as exc:
                _publish(client, machine, repo, name, raw, error=str(exc), tries=tries + 1)
                written += 1
            else:
                _publish(client, machine, repo, name, raw, digest=text, model=model, tries=tries + 1)
                written += 1
    except _REDIS_ERRORS:
        pass
    return written


def read_all(**connection) -> list[dict]:
    """Return every published entry, newest first. Return an empty list if Redis is down."""
    try:
        client = _client(connection)
        keys = slots_redis._call_with_retry(
            lambda: list(client.scan_iter(match=f"{KEY_PREFIX}*", count=slots_redis.SCAN_COUNT))
        )
        entries = []
        for key in keys:
            entry = _get(client, key)
            if entry and all(isinstance(entry.get(f), str) for f in ("machine", "repo", "name", "raw")):
                entries.append(entry)
    except _REDIS_ERRORS:
        return []
    # A bad name from Redis must not reach a page. Same checks as a local file.
    entries = [
        e for e in entries
        if MACHINE_RE.fullmatch(e["machine"])
        and debrief.REPO_RE.fullmatch(e["repo"])
        and debrief.FILE_RE.fullmatch(e["name"])
    ]
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
        client = _client(connection)
        return _get(client, _key(machine, repo, name))
    except _REDIS_ERRORS:
        return None
