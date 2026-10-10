"""Recent updates: what changed in each repo lately.

The Overview page shows these as its "Recent updates" feed. The collector reads
the GitHub issue data that the dashboard already caches. It makes no new `gh`
call. It keeps the changes inside a time window, newest first. Each change has
one kind:

- Opened: a new issue.
- Closed: a closed issue.
- Comment: a new comment on an issue.
- Screenshot: a new comment that holds only images.
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timedelta
from urllib.parse import urlencode, urlsplit

from . import roadmap

WINDOW_HOURS = 12
MAX_UPDATES = 50
EXCERPT_CHARS = 200
# Chip labels, in display order. "Comments" keeps the Comment kind only.
# "Images" keeps every update that has an image.
FILTERS = ("All", "Comments", "Closed", "Opened", "Images")

_IMAGE_MARKDOWN = re.compile(r"!\[[^\]]*\]\([^)\s]*\)")


def _excerpt(text: str) -> str:
    """The first line of `text` with images removed, cut to a short length."""
    for line in _IMAGE_MARKDOWN.sub("", text).splitlines():
        line = re.sub(r"\s+", " ", line).strip().lstrip("#>-*• ").strip()
        if line:
            if len(line) > EXCERPT_CHARS:
                return line[:EXCERPT_CHARS] + "…"
            return line
    return ""


def _images(text: str) -> list[dict]:
    """GitHub attachment images in `text`. Other images are not shown."""
    found = []
    for match in roadmap.IMAGE.finditer(text):
        attachment_id = roadmap.github_attachment_id(match.group(2))
        if attachment_id:
            found.append({"id": attachment_id, "label": match.group(1) or "image"})
    return found


def _github_url(value) -> str:
    """`value` when it is an https github.com link. Otherwise an empty string."""
    try:
        parsed = urlsplit(str(value or ""))
    except ValueError:
        return ""
    if parsed.scheme == "https" and parsed.netloc == "github.com":
        return str(value)
    return ""


def _time(value) -> datetime | None:
    """A UTC time from an ISO string. Anything else gives None, even a number."""
    if not isinstance(value, str):
        return None
    try:
        return roadmap._parse_time(value)
    except OverflowError:
        return None


def _in_window(when: datetime | None, start: datetime, end: datetime) -> bool:
    return when is not None and start <= when < end


def collect_repo(
    repo: str, issues: list, comments: dict, start: datetime, end: datetime
) -> list[dict]:
    """The updates in one repo between `start` and `end`.

    `issues` holds open and closed issues. `comments` maps an issue number to
    its comments. Items that are not the expected shape are skipped.
    """
    updates = []
    titles = {}
    urls = {}
    for issue in issues if isinstance(issues, list) else []:
        if not isinstance(issue, dict) or not isinstance(issue.get("number"), int):
            continue
        number = issue["number"]
        title = str(issue.get("title") or "")
        url = _github_url(issue.get("url"))
        titles[number] = title
        urls[number] = url
        body = str(issue.get("body") or "")
        opened = _time(issue.get("createdAt"))
        if _in_window(opened, start, end):
            updates.append({
                "repo": repo, "number": number, "kind": "Opened", "when": opened,
                "title": title, "text": _excerpt(body), "url": url,
                "images": _images(body),
            })
        closed = _time(issue.get("closedAt"))
        if _in_window(closed, start, end):
            updates.append({
                "repo": repo, "number": number, "kind": "Closed", "when": closed,
                "title": title, "text": "", "url": url, "images": [],
            })
    for number, items in (comments.items() if isinstance(comments, dict) else []):
        if not isinstance(number, int) or not isinstance(items, list):
            continue
        for comment in items:
            if not isinstance(comment, dict):
                continue
            created = _time(comment.get("createdAt"))
            if not _in_window(created, start, end):
                continue
            body = str(comment.get("body") or "")
            text = _excerpt(body)
            images = _images(body)
            author = str(comment.get("author") or "")
            if images and not text:
                kind, text = "Screenshot", "New image on the issue."
            else:
                kind = "Comment"
                text = f"{author}: {text}" if author and text else text
            updates.append({
                "repo": repo, "number": number, "kind": kind, "when": created,
                "title": titles.get(number, ""),
                "text": text,
                "url": _github_url(comment.get("url")) or urls.get(number, ""),
                "images": images,
            })
    return updates


def collect(
    sources: dict[str, dict], now: datetime, hours: int = WINDOW_HOURS
) -> list[dict]:
    """The updates from every repo, newest first.

    `sources` maps a repo name to {"issues": [...], "comments": {...}}.
    """
    start = now - timedelta(hours=hours)
    updates = []
    for repo, data in sources.items():
        updates.extend(
            collect_repo(repo, data.get("issues"), data.get("comments"), start, now)
        )
    updates.sort(key=lambda item: item["when"], reverse=True)
    return updates


def filter_updates(updates: list[dict], repo: str = "", kind: str = "All") -> list[dict]:
    """Keep the updates of one repo and one chip. An empty `repo` keeps every repo."""
    kept = []
    for item in updates:
        if repo and item["repo"] != repo:
            continue
        if kind == "Images" and not item["images"]:
            continue
        if kind == "Comments" and item["kind"] != "Comment":
            continue
        if kind in ("Closed", "Opened") and item["kind"] != kind:
            continue
        kept.append(item)
    return kept


def _chip(label: str, repo: str, kind: str, active: bool) -> str:
    """A filter link. The JavaScript loader uses `data-src`. A plain click uses `href`."""
    query = {}
    if repo:
        query["repo"] = repo
    if kind != "All":
        query["kind"] = kind
    fragment = "/updates?" + urlencode(query) if query else "/updates"
    page = "/updates?" + urlencode({"full": "1", **query})
    return (
        f"<a class='updates-chip{' active' if active else ''}' "
        f"href='{html.escape(page, quote=True)}' "
        f"data-src='{html.escape(fragment, quote=True)}'>{html.escape(label)}</a>"
    )


def _render_update(item: dict) -> str:
    ref = html.escape(f"{item['repo']} #{item['number']}")
    if item["url"]:
        ref = (
            f"<a class='updates-ref dim' href='{html.escape(item['url'], quote=True)}' "
            f"target='_blank' rel='noopener' title='Open on GitHub'>{ref}</a>"
        )
    else:
        ref = f"<span class='updates-ref dim'>{ref}</span>"
    when = item["when"]
    stamp = when.strftime("%Y-%m-%d %H:%M UTC")
    parts = [
        "<div class='update'>"
        f"<span class='update-ago mono dim' data-since='{when.timestamp():.0f}'>{stamp}</span>"
        f"<span class='update-kind pill{' on' if item['kind'] == 'Closed' else ''}'>"
        f"{html.escape(item['kind'])}</span>"
        f"<div class='update-main'><div>{ref} <b>{html.escape(item['title'])}</b></div>"
    ]
    if item["text"]:
        parts.append(f"<div class='update-text'>{html.escape(item['text'])}</div>")
    if item["images"]:
        parts.append("<div class='update-images'>")
        for image in item["images"]:
            parts.append(
                f"<img src='/image?id={image['id']}' "
                f"alt='{html.escape(image['label'], quote=True)}' loading='lazy'>"
            )
        parts.append("</div>")
    parts.append("</div></div>")
    return "".join(parts)


def render_updates(
    updates: list[dict],
    *,
    repo: str = "",
    kind: str = "All",
    warnings: list[str] | tuple = (),
    hours: int = WINDOW_HOURS,
) -> str:
    """The feed as an HTML fragment: filter chips, the list, and any warnings.

    `updates` is the whole list from `collect`. This function applies the
    filters. The repo chips count the whole list, so the counts do not change
    when a chip is chosen.
    """
    counts: dict[str, int] = {}
    for item in updates:
        counts[item["repo"]] = counts.get(item["repo"], 0) + 1
    if repo:
        counts.setdefault(repo, 0)
    parts = [
        "<div class='updates-filters'>"
        + "".join(_chip(label, repo, label, label == kind) for label in FILTERS)
        + "</div>",
        "<div class='updates-filters'><span class='updates-label'>Repo</span>"
        + _chip("All repos", "", kind, not repo)
        + "".join(
            _chip(f"{name} {counts[name]}", name, kind, name == repo)
            for name in sorted(counts)
        )
        + "</div>",
    ]
    shown = filter_updates(updates, repo, kind)[:MAX_UPDATES]
    if shown:
        parts.append(
            "<div class='card updates-list'>"
            + "".join(_render_update(item) for item in shown)
            + "</div>"
        )
    elif updates:
        parts.append("<div class='card dim'>No updates match this filter.</div>")
    else:
        parts.append(f"<div class='card dim'>No updates in the last {hours} hours.</div>")
    parts.append(
        "<p class='dim updates-note'>Images come from GitHub attachments and load "
        "through the authenticated image route.</p>"
    )
    for warning in warnings:
        parts.append(f"<p class='dim updates-note'>{html.escape(str(warning))}</p>")
    return "".join(parts)


UPDATES_CSS = """
.updates-filters{display:flex;flex-wrap:wrap;gap:6px;align-items:center;font-size:12px;margin:0 0 10px}
.updates-label{font:400 12px var(--mono);letter-spacing:.06em;text-transform:uppercase;color:var(--ink3);margin-right:4px}
.updates-chip{padding:4px 11px;border:1px solid var(--line);border-radius:8px;background:var(--surface);color:var(--ink2);text-decoration:none}
.updates-chip:hover{filter:brightness(.92);text-decoration:none}
.updates-chip.active{background:var(--ink);border-color:var(--ink);color:var(--surface)}
.updates-list{padding:2px 18px}
.update{display:grid;grid-template-columns:96px 96px minmax(0,1fr);gap:16px;padding:14px 0;border-top:1px solid var(--line2);align-items:start}
.update:first-child{border-top:0}
.update-ago{font-size:12px;padding-top:3px}
.update-kind{justify-self:start}
.updates-ref{font:400 12px var(--mono);text-decoration:none}
.update-text{font-size:13px;color:var(--ink2);margin-top:3px;line-height:1.5;overflow-wrap:anywhere}
.update-images{display:flex;flex-wrap:wrap;gap:10px;margin-top:10px}
.update-images img{height:92px;max-width:100%;object-fit:cover;border-radius:11px;border:1px solid var(--line)}
.updates-note{font-size:12px;margin:8px 0 0}
@media(max-width:700px){.update{grid-template-columns:1fr;gap:6px}}
"""

# Fills `#updates` after first paint, so the Overview page never waits for
# GitHub. A click on a filter chip loads that chip's fragment in place.
LOADER_JS = """
(function(){
  var box=document.getElementById('updates');
  if(!box)return;
  function load(src){
    fetch(src,{credentials:'same-origin'}).then(function(response){
      if(!response.ok)throw new Error('status '+response.status);
      return response.text();
    }).then(function(html){
      box.innerHTML=html;
    }).catch(function(){
      box.innerHTML='';
      var note=document.createElement('p');
      note.className='dim';
      note.textContent='Recent updates did not load. ';
      var link=document.createElement('a');
      link.href=box.dataset.full;
      link.textContent='Open them on their own page.';
      note.appendChild(link);
      box.appendChild(note);
    });
  }
  box.addEventListener('click',function(event){
    var chip=event.target.closest&&event.target.closest('a[data-src]');
    if(!chip||!box.contains(chip))return;
    event.preventDefault();
    load(chip.dataset.src);
  });
  load(box.dataset.src);
})();
"""
