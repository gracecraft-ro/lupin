#!/usr/bin/env python3
"""lupin serve - a mostly read-only web dashboard for the loopctl delegation loops.

The server binds a loopback or tailnet address (100.64.0.0/10); it refuses
to start on any other address. A tailnet bind relies on the headscale ACL
and the host firewall as its boundary (same model as this project's Redis
deployment, docs/redis-schema.md) -- the DNS-rebinding check below still
only accepts the Host header matching what was actually bound. The only
POST routes are /quest/start, /quest/stop (both only write to Redis via
quest.py), and /machines/slot-max (changes one Redis slot's max holder
count -- a validated slot name and a positive integer, nothing else); no
route starts a process with arguments built from the browser. Read probes
use fixed argv lists, run without a shell. GitHub attachment images use an
authenticated, fixed-host proxy; it sends the GitHub token only to
github.com and strips it before a validated storage redirect. None of
these write routes carry auth of their own -- a reverse proxy in front of
this server is expected to gate write access before a request reaches here.

Every other page reads loop state but does not change it. It does not
shell out to loopctl. The installed CLI can be older than this dashboard;
direct reads of tmux and systemd avoid version skew.

Python standard library only. GitHub image bytes are fetched only when the
browser requests a validated attachment ID.
"""

from __future__ import annotations

import html
import ipaddress
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from importlib import resources
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse, urlsplit

from . import claims, machines, quest, roadmap, slots_redis
from .slots import CoordinatorUnreachable
from .quota import (
    QuotaDuration,
    claude_usage,
    epoch_ms_to_local,
    omp_usage,
    quota_source_label,
    quota_usage,
)

ATTACHMENT_ID = roadmap.ATTACHMENT_ID
ATTACHMENT_REDIRECT_HOST = re.compile(
    r"github-production-user-asset-[a-z0-9-]+\.s3\.amazonaws\.com"
)
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_FORM_BYTES = 8 * 1024
IMAGE_TYPES = {"image/gif", "image/jpeg", "image/png", "image/webp"}
FAVICON = b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><path fill="#e11d48" d="M16 28S3 20.4 3 11.5A7.5 7.5 0 0 1 16 7.4a7.5 7.5 0 0 1 13 4.1C29 20.4 16 28 16 28Z"/></svg>"""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def github_attachment(attachment_id: str) -> tuple[bytes, str] | None:
    try:
        auth = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    token = auth.stdout.strip()
    auth.stdout = ""
    if auth.returncode != 0 or not token:
        return None

    opener = urllib.request.build_opener(NoRedirect())
    request = urllib.request.Request(
        f"https://github.com/user-attachments/assets/{attachment_id}",
        headers={"Authorization": f"token {token}", "Accept": "image/*"},
    )
    token = ""
    try:
        try:
            response = opener.open(request, timeout=15)
        except urllib.error.HTTPError as redirect:
            if redirect.code not in (301, 302, 303, 307, 308):
                redirect.close()
                return None
            location = redirect.headers.get("Location")
            redirect.close()
            try:
                parsed = urlsplit(location or "")
            except ValueError:
                return None
            try:
                port = parsed.port
            except ValueError:
                return None
            if (
                parsed.scheme != "https"
                or parsed.username is not None
                or parsed.password is not None
                or port not in (None, 443)
                or not ATTACHMENT_REDIRECT_HOST.fullmatch(parsed.hostname or "")
            ):
                return None
            response = opener.open(
                urllib.request.Request(location, headers={"Accept": "image/*"}),
                timeout=15,
            )
        with response:
            content_type = response.headers.get_content_type()
            if content_type not in IMAGE_TYPES:
                return None
            data = response.read(MAX_IMAGE_BYTES + 1)
            if len(data) > MAX_IMAGE_BYTES:
                return None
            return data, content_type
    except (OSError, TimeoutError, urllib.error.URLError):
        return None


STATE_DIR = "/var/lib/delegation-loop"
REPOS_FILE = os.path.join(STATE_DIR, "repos")
CODE_DIR = "/code"
LOOP_DOC = "docs/delegation-loop.md"
SESSION_PREFIX = "loop-"

# Tailscale's CGNAT range (100.64.0.0/10). A bind address in this range is a
# tailnet interface, gated by the headscale ACL and the host firewall -- the
# same trust boundary this project's Redis deployment already relies on
# (docs/redis-schema.md). Any other non-loopback address is still refused.
TAILNET_RANGE = ipaddress.ip_network("100.64.0.0/10")


def bind_allowed(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Loopback, or a tailnet address -- see TAILNET_RANGE above."""
    return addr.is_loopback or (addr.version == 4 and addr in TAILNET_RANGE)

# model-tiers.json -- the same file route.py routes a (category, size) pair
# with. It ships as package data, so read it the same way route.py does.
MODEL_TIERS_PATH = str(resources.files("lupin").joinpath("model-tiers.json"))
# The tier keys the data file uses, cheapest first. A category may be
# missing any of them; route.py escalates a missing tier to the next one up.
MODEL_TIER_ORDER = ("tier0", "tier1", "tier2")

# --------------------------------------------------------------------------
# running read-only probes
# --------------------------------------------------------------------------


def run(argv: list[str], timeout: float = 10.0) -> tuple[int, str]:
    """Run a fixed read-only probe. Never a shell, never browser input."""
    try:
        proc = subprocess.run(
            argv,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return 127, f"not found: {argv[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s: {' '.join(argv)}"
    out = proc.stdout
    if proc.stderr:
        out = out + ("\n" if out and not out.endswith("\n") else "") + proc.stderr
    return proc.returncode, out


# --------------------------------------------------------------------------
# reading state
# --------------------------------------------------------------------------


def enabled_repos() -> list[str]:
    try:
        with open(REPOS_FILE, encoding="utf-8") as fh:
            return [line.strip() for line in fh if line.strip()]
    except OSError:
        return []


def code_repos() -> list[dict]:
    """Every directory under /code, with whether it can run a loop."""
    out = []
    enabled = set(enabled_repos())
    try:
        names = sorted(
            d.name for d in os.scandir(CODE_DIR) if d.is_dir(follow_symlinks=True)
        )
    except OSError:
        names = []
    for name in names:
        loopable = os.path.isfile(os.path.join(CODE_DIR, name, LOOP_DOC))
        if not loopable:
            state = "no-doc"
        elif name in enabled:
            state = "enabled"
        else:
            state = "disabled"
        out.append({"repo": name, "state": state, "loopable": loopable})
    return out


def tmux_sessions() -> list[dict]:
    fmt = "#{session_name}\t#{session_created}\t#{session_attached}\t#{session_windows}\t#{session_activity}"
    rc, out = run(["tmux", "ls", "-F", fmt])
    sessions = []
    if rc != 0:
        return sessions
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        name, created, attached, windows, activity = parts[:5]
        sessions.append(
            {
                "name": name,
                "repo": name[len(SESSION_PREFIX):] if name.startswith(SESSION_PREFIX) else None,
                "created": int(created) if created.isdigit() else 0,
                "attached": attached == "1",
                "windows": int(windows) if windows.isdigit() else 0,
                "activity": int(activity) if activity.isdigit() else 0,
            }
        )
    return sorted(sessions, key=lambda s: (s["repo"] is None, s["name"]))


def session_tail(session: str, lines: int) -> str:
    rc, out = run(["tmux", "capture-pane", "-pt", session, "-S", f"-{lines}"])
    return out if rc == 0 else f"(could not read pane: {out.strip()})"


def timers() -> list[dict]:
    """Delegation timers, from systemd's own JSON. Times are epoch seconds."""
    rc, out = run(["systemctl", "list-timers", "--all", "--no-pager", "--output=json"])
    if rc != 0:
        return []
    try:
        rows = json.loads(out)
    except json.JSONDecodeError:
        return []
    result = []
    for row in rows:
        unit = row.get("unit") or ""
        if unit != "delegation-loop.timer" and not unit.startswith("delegation-loop-once-"):
            continue
        nxt = row.get("next")
        last = row.get("last")
        result.append(
            {
                "unit": unit,
                "activates": row.get("activates") or "",
                "next": nxt / 1e6 if isinstance(nxt, (int, float)) and nxt else None,
                "last": last / 1e6 if isinstance(last, (int, float)) and last else None,
            }
        )
    return sorted(result, key=lambda t: (t["next"] is None, t["next"] or 0))


def oneoff_repositories(unit: str) -> str:
    """Read repo arguments from a one-off timer's launch command."""
    rc, output = run(
        ["systemctl", "show", unit, "--property=ExecStart", "--value"]
    )
    if rc != 0:
        return unit
    match = re.search(r"(?:^|\s)argv\[\]=(.+?)(?:\s*;\s*[^;{}]+=|}\s*$)", output)
    if not match:
        return unit
    try:
        argv = shlex.split(match.group(1))
    except ValueError:
        return unit
    launcher = next(
        (index for index, arg in enumerate(argv) if arg.endswith("/delegation-launch")),
        None,
    )
    if launcher is None:
        return unit
    args = argv[launcher + 1 :]
    repos = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in ("--note", "--platform"):
            index += 2
        elif arg.startswith("--"):
            return unit
        else:
            repos.extend(args[index:])
            break
    return ", ".join(repos) if repos else unit


def timer_repository(unit: str) -> str:
    if unit == "delegation-loop.timer":
        return "all enabled repos"
    return oneoff_repositories(unit)


def timer_active() -> bool:
    rc, _ = run(["systemctl", "is-active", "--quiet", "delegation-loop.timer"])
    return rc == 0


def fleet_state(connection: dict) -> dict:
    """Machines and claims from the cross-machine Redis registry (issue
    #15). Degrades the same way the local readers above do: a Redis outage
    returns empty data and an error string instead of raising -- claims has
    no local fallback either way (see claims.py), so a claims-only failure
    just leaves that part empty without blanking the machines list.

    `repos` for `claims.claims_for` is built the same way
    `roadmap_cli.build_roadmap` already does: `enabled_repos()` for the
    short names, `roadmap._repo_identity()` to resolve each one's GitHub
    owner from its local checkout. A repo with no local checkout (or no
    `gh` access) is silently skipped, same as `/roadmap` already tolerates.
    """
    try:
        machine_list = machines.machines(connection)
    except machines.CoordinatorUnreachable as exc:
        return {"machines": [], "claims": {}, "fleet_error": str(exc)}

    full_names = {}
    for repo in enabled_repos():
        owner, _name, _warning = roadmap._repo_identity(os.path.join(CODE_DIR, repo))
        if owner:
            full_names[repo] = f"{owner}/{repo}"

    claims_data: dict = {}
    if full_names:
        try:
            claims_data = claims.claims_for(
                list(full_names.values()),
                redis_host=connection.get("redis_host"),
                redis_port=connection.get("redis_port"),
                redis_username=connection.get("redis_username"),
                redis_password=connection.get("redis_password"),
            )
        except claims.CoordinatorUnreachable:
            pass
    return {"machines": machine_list, "claims": claims_data, "fleet_error": None}


def gather(peek_lines: int, connection: dict | None = None) -> dict:
    sessions = tmux_sessions()
    for s in sessions:
        s["tail"] = session_tail(s["name"], peek_lines) if s["repo"] else ""
    state = {
        "now": time.time(),
        "enabled": enabled_repos(),
        "repos": code_repos(),
        "sessions": sessions,
        "timers": timers(),
        "timer_active": timer_active(),
    }
    state.update(fleet_state(connection or {}))
    return state


# --------------------------------------------------------------------------
# html
# --------------------------------------------------------------------------

CSS = """
:root{
--ok:#1f7a6a;--warn:#d2512e;--ink:#14201e;--bg:#f2f6f5;--surface:#ffffff;
--side:#e4eeec;--ink2:#4a5b58;--ink3:#5f706d;--line:#d3e0dd;--line2:#e3ecea;
--track:#dae6e3;--warnbg:#fff0ea;--warnline:#f6cdbd;--warnink:#a03c1a;
--term:#10201e;--termink:#dfece9;--frame:#c3d3cf;--idle:#9fb0ac;
--lav:#26636b;--lavbg:#dcebe8;--okbg:#e1f1ee;
/* The mockup loads these two from Google Fonts. We don't load that file
(CSP blocks it), so the names below are unused and every browser falls
through to the system font right after them. */
--sans:Nunito,"Segoe UI Rounded",ui-rounded,-apple-system,"Segoe UI",system-ui,sans-serif;
--mono:"Geist Mono",ui-monospace,"SF Mono","Cascadia Code","Roboto Mono",monospace;
/* legacy names: src/lupin/roadmap.py's own CSS still refers to these */
--fg:var(--ink);--dim:var(--ink3);--card:var(--surface);--accent:var(--ok);--code:var(--track);
}
:root[data-theme="dark"]{
--ok:#4cc2ad;--warn:#ef8a5c;--ink:#e8f1ef;--bg:#101615;--surface:#172120;
--side:#0c1110;--ink2:#a9bcb8;--ink3:#8da29d;--line:#273532;--line2:#1e2927;
--track:#222f2c;--warnbg:#35211a;--warnline:#5e3626;--warnink:#f5b394;
--term:#0a100f;--termink:#dbe8e5;--frame:#2f3f3b;--idle:#72857f;
--lav:#7fc7cf;--lavbg:#1c2c2c;--okbg:#18291f;
}
*{box-sizing:border-box}
html,body{margin:0}
body{background:var(--bg);color:var(--ink);font:15px/1.5 var(--sans)}
a{color:var(--ok);text-decoration:none}
a:hover{color:var(--ink);text-decoration:underline}
.shell{display:flex;min-height:100vh}
.side{width:200px;flex:none;background:var(--side);border-right:1px solid var(--line);
padding:22px 14px;display:flex;flex-direction:column;position:sticky;top:0;
height:100vh;overflow:auto}
.brand{font:700 19px var(--mono);padding:0 8px 22px 8px}
.navlinks{display:grid;gap:2px}
.navlink{display:flex;align-items:center;gap:9px;padding:8px 10px;border-radius:10px;
font-size:14px;color:var(--ink)}
.navlink svg{color:var(--ink2)}
.navlink:hover{background:var(--line2);text-decoration:none}
.navlink.active{background:var(--lavbg);font-weight:600}
.content{flex:1;min-width:0;display:flex;flex-direction:column}
.topbar{display:flex;align-items:center;gap:14px;padding:12px 28px;
border-bottom:1px solid var(--line);font-size:13px;color:var(--ink2)}
.topbar .sp{flex:1}
.autolabel{display:flex;align-items:center;gap:6px;cursor:pointer}
.iconbtn{width:32px;height:32px;flex:none;padding:0;display:flex;align-items:center;
justify-content:center;border-radius:10px;border:1px solid var(--line);
background:var(--surface);color:var(--ink);cursor:pointer}
.iconbtn:hover{filter:brightness(.94)}
.iconbtn:focus-visible{outline:2px solid var(--ok);outline-offset:2px}
.iconbtn .icon-sun{display:none}
:root[data-theme="dark"] .iconbtn .icon-sun{display:inline-flex}
:root[data-theme="dark"] .iconbtn .icon-moon{display:none}
main{max-width:1500px;padding:1.5rem 1.75rem 4rem;flex:1;min-width:0}
h1{font-size:1.25rem;margin:0}
h2{font-size:.8rem;margin:2rem 0 .6rem;color:var(--ink2);font-family:var(--mono);
text-transform:uppercase;letter-spacing:.06em}
header{display:flex;gap:1rem;align-items:center;flex-wrap:wrap;
border-bottom:1px solid var(--line);padding-bottom:.8rem;margin-bottom:.2rem}
header h1{display:flex;align-items:center;gap:10px;font:600 21px var(--mono)}
header h1 svg{color:var(--ok)}
header .sp{flex:1}
header a{font-size:13px}
.dim{color:var(--ink3)}
.card{background:var(--surface);border:1px solid var(--line);border-radius:18px;
box-shadow:0 3px 0 var(--line);padding:.9rem 1.1rem;margin-bottom:.7rem}
.row{display:flex;gap:.8rem;align-items:center;flex-wrap:wrap}
.pill{font-size:.75rem;padding:.15rem .6rem;border-radius:99px;
border:1px solid var(--line);color:var(--ink2)}
.pill.on{color:var(--ok);border-color:var(--ok)}
.pill.off{color:var(--warn);border-color:var(--warn)}
.big{font-size:1.05rem;font-weight:600}
pre{background:var(--term);color:var(--termink);border-radius:14px;
padding:.6rem .7rem;overflow-x:auto;font:400 12px/1.5 var(--mono);margin:.6rem 0 0;
max-height:16rem;white-space:pre}
table{border-collapse:collapse;width:100%;font-size:14px}
td,th{text-align:left;padding:.5rem .6rem;border-bottom:1px solid var(--line2)}
th{color:var(--ink3);font-weight:500;font-size:.72rem;letter-spacing:.04em;
text-transform:uppercase}
.section-head{display:flex;align-items:center;gap:7px;margin:1.8rem 0 .7rem}
.section-head svg{color:var(--ink2)}
.section-head h2{margin:0}
.stat-row{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}
.stat{display:flex;flex-direction:column;gap:5px}
.stat svg{color:var(--ink2)}
.stat-label{font-size:.72rem;letter-spacing:.06em;text-transform:uppercase;
color:var(--ink3);font-family:var(--mono);display:flex;align-items:center;gap:6px}
.stat-value{font-size:26px;font-weight:600}
.stat-value.mono{font-family:var(--mono)}
.stat-note{font-size:.8rem;color:var(--ink2)}
.stat.warn{background:var(--warnbg);border-color:var(--warnline);box-shadow:none}
.stat.warn .stat-label,.stat.warn .stat-value{color:var(--warnink)}
.stat.ok .stat-value{color:var(--ok)}
.loop-grid{display:grid;gap:10px}
.loop-card{display:block;color:inherit}
.loop-card:hover{border-color:var(--ink3);text-decoration:none}
.loop-head{display:flex;align-items:center;gap:10px;font-size:13px;
color:var(--ink2);flex-wrap:wrap}
.loop-head b{font-size:15px;color:var(--ink);font-weight:600}
.dot{width:8px;height:8px;border-radius:2px;background:var(--ok);flex:none}
.dot.idle{background:var(--idle)}
.loop-tail{margin:10px 0 0;max-height:4.6em}
.loop-empty{display:flex;gap:14px;align-items:center}
@media(max-width:860px){
.shell{flex-direction:column}
.side{width:auto;height:auto;position:static;flex-direction:row;align-items:center;
gap:14px;padding:12px 16px;overflow-x:auto}
.navlinks{display:flex;flex-direction:row;gap:4px}
.navlink span{display:none}
main{padding:1rem 1rem 3rem}
.stat-row{grid-template-columns:1fr}
}
.quota-heading{display:flex;justify-content:space-between;align-items:baseline;
gap:1rem;flex-wrap:wrap}
.scroll{overflow-x:auto}
.quota-summary{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));
gap:1rem;margin:1.5rem 0}
.quota-summary-key{font-size:.75rem;color:var(--dim);letter-spacing:.06em;
text-transform:uppercase}
.quota-summary-value{font-size:1.15rem;font-weight:600;margin-top:.35rem}
.quota-summary-note{font-size:.8rem;color:var(--dim);margin-top:.15rem}
.quota-legend{display:flex;gap:1.25rem;flex-wrap:wrap;color:var(--dim);
font-size:.85rem;margin:1.25rem 0}
.quota-legend-item{display:flex;align-items:center;gap:.5rem}
.quota-legend-used{width:28px;height:8px;border-radius:4px;background:var(--accent)}
.quota-legend-time{width:2px;height:14px;background:var(--fg)}
.quota-legend-ahead{width:28px;height:8px;border-radius:4px;background:var(--warn)}
.quota-groups{display:grid;gap:.8rem}
.quota-group{padding:1.2rem 1.4rem .4rem}
.quota-group-heading{display:flex;justify-content:space-between;align-items:baseline;
gap:.75rem;flex-wrap:wrap;margin-bottom:.4rem}
.quota-group-heading h3{margin:0;font-size:1.1rem}
.quota-row{display:grid;grid-template-columns:minmax(90px,120px) minmax(0,1fr)
minmax(130px,170px);gap:1.5rem;align-items:center;padding:1rem 0;
border-top:1px solid var(--line)}
.quota-window{font-weight:500}
.quota-status{font-size:.8rem;color:var(--dim);margin-top:.2rem}
.quota-status.ahead{color:var(--warn)}
.quota-status.under{color:var(--accent)}
.quota-values{display:flex;justify-content:space-between;gap:1rem;
font-size:.85rem;color:var(--dim);margin-bottom:.45rem}
.quota-values strong{font-size:1.2rem;color:var(--fg)}
.quota-meter{position:relative;height:9px;border-radius:5px;background:var(--line)}
.quota-meter-used{position:absolute;inset:0 auto 0 0;border-radius:5px;
background:var(--accent)}
.quota-meter-used.ahead{background:var(--warn)}
.quota-meter-elapsed{position:absolute;top:-4px;bottom:-4px;width:2px;
border-radius:1px;background:var(--fg)}
.quota-reset{text-align:right}
.quota-reset-left{font:500 1rem ui-monospace,monospace}
.quota-reset-at{font:400 .75rem ui-monospace,monospace;color:var(--dim);margin-top:.2rem}
@media(max-width:700px){.quota-row{grid-template-columns:1fr;gap:.5rem}
.quota-reset{text-align:left}}
.tier-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:.8rem}
.tier-card{margin-bottom:0;display:flex;flex-direction:column;gap:.45rem}
.tier-heading{display:flex;justify-content:space-between;align-items:baseline;gap:.75rem}
.tier-heading h3{margin:0;font-size:1.05rem}
.tier-row{display:grid;grid-template-columns:3.5rem minmax(0,1fr);gap:.75rem;
align-items:baseline;padding-top:.5rem;border-top:1px solid var(--line)}
.tier-name{font-size:.75rem;letter-spacing:.06em;text-transform:uppercase;
color:var(--dim)}
.tier-picks{display:flex;flex-wrap:wrap;gap:.4rem;align-items:baseline}
.tier-pick{display:inline-flex;gap:.4rem;align-items:baseline;
border:1px solid var(--line);border-radius:6px;padding:.1rem .45rem;
background:var(--code)}
.tier-model{font:500 .9rem ui-monospace,monospace}
.tier-arrow{color:var(--dim)}
.tier-note{margin:.15rem 0 0;font-size:.85rem}
.issue-details{border-top:1px solid var(--line);margin-top:.5rem;padding-top:.35rem}
.issue-details summary{cursor:pointer}
.issue-body{white-space:pre-wrap;overflow-wrap:anywhere;margin:.35rem 0}
.activity-comments{padding-left:1.5rem}
.err{color:#9b2226}
"""

JS = """
// Live-tick the relative times, and reload on a timer if the box is ticked.
function fmt(s){s=Math.max(0,Math.round(s));
 var d=Math.floor(s/86400),h=Math.floor(s%86400/3600),
     m=Math.floor(s%3600/60),x=s%60;
 if(d)return d+"d "+h+"h"; if(h)return h+"h "+m+"m";
 if(m)return m+"m "+x+"s"; return x+"s";}
function tick(){var now=Date.now()/1000;
 document.querySelectorAll("[data-since]").forEach(function(e){
   e.textContent=fmt(now-parseFloat(e.dataset.since))+" ago";});
 document.querySelectorAll("[data-until]").forEach(function(e){
   var d=parseFloat(e.dataset.until)-now;
   e.textContent=d>0?("in "+fmt(d)):("overdue by "+fmt(-d));});}
setInterval(tick,1000);tick();
var box=document.getElementById("auto");
if(box){box.checked=localStorage.getItem("lupin-auto")==="1";
 box.addEventListener("change",function(){
   localStorage.setItem("lupin-auto",box.checked?"1":"0");});
 setInterval(function(){if(box.checked)location.reload();},10000);}
document.querySelectorAll("[data-once-repo]").forEach(function(row){
 var when=row.querySelector("input"),command=row.querySelector("code"),
     status=row.querySelector("[data-copy-status]");
 function update(){command.textContent="loopctl once "+row.dataset.onceRepo+" "+when.value;}
 when.addEventListener("input",update);
 row.querySelector("button").addEventListener("click",function(){
   Promise.resolve().then(function(){
     return navigator.clipboard.writeText(command.textContent);
   }).then(function(){status.textContent="copied";},function(){
     status.textContent="copy failed";
   }).then(function(){setTimeout(function(){status.textContent="";},2000);});
 });
});
var themeBtn=document.getElementById("theme-toggle");
if(themeBtn){themeBtn.addEventListener("click",function(){
  var root=document.documentElement;
  var next=root.getAttribute("data-theme")==="dark"?"light":"dark";
  root.setAttribute("data-theme",next);
  try{localStorage.setItem("lupin-theme",next);}catch(e){}});}
"""

# Runs before the stylesheet paints, so the page never flashes the wrong
# theme. No network access, no state beyond one localStorage key.
THEME_BOOTSTRAP = """<script>(function(){try{
var t=localStorage.getItem("lupin-theme");
if(!t){t=matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light";}
document.documentElement.setAttribute("data-theme",t);
}catch(e){}})();</script>"""

# (nav key, path, label, icon path data) -- icon paths are the same ones the
# design mockup uses for these pages, so the sidebar and page headers agree.
NAV_ITEMS = [
    ("overview", "/", "Overview", "M3 11l9-8 9 8M5 10v10h14V10"),
    ("roadmap", "/roadmap", "Roadmap", "M5 21V4M5 4h12l-2 4 2 4H5"),
    ("usage", "/usage", "Usage", "M5 20V10M12 20V4M19 20v-7"),
    (
        "models",
        "/model-tiers",
        "Models",
        "M7 7h10v10H7zM9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3",
    ),
    (
        "machines",
        "/machines",
        "Machines",
        "M4 4h16v6H4zM4 14h16v6H4zM8 7h.01M8 17h.01",
    ),
]
SUN_ICON = "M12 8a4 4 0 100 8 4 4 0 000-8zM12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"
MOON_ICON = "M20 13.5A8.5 8.5 0 1110.5 4a6.5 6.5 0 009.5 9.5zM18 2v3M16.5 3.5h3"


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def icon(d: str, size: int = 16) -> str:
    """A stroke-style icon, matching the mockup's svg icons. `d` is always
    one of the fixed path strings above, never caller-supplied text."""
    return (
        f'<svg aria-hidden="true" width="{size}" height="{size}" viewBox="0 0 24 24" '
        'fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" '
        f'stroke-linejoin="round" style="flex:none"><path d="{d}"></path></svg>'
    )


def render_nav(active: str) -> str:
    links = "".join(
        f"<a class='navlink{' active' if key == active else ''}' href='{href}'>"
        f"{icon(path)}<span>{esc(label)}</span></a>"
        for key, href, label, path in NAV_ITEMS
    )
    return (
        "<nav class=side><div class=brand>lupin</div>"
        f"<div class=navlinks>{links}</div><div style='flex:1'></div></nav>"
    )


def render_topbar() -> str:
    return (
        "<div class=topbar>"
        "<label class=autolabel><input type=checkbox id=auto> auto-refresh</label>"
        "<span class=sp></span>"
        "<button type=button id=theme-toggle class=iconbtn aria-label='Toggle dark mode' "
        "title='Toggle dark mode'>"
        f"<span class=icon-sun>{icon(SUN_ICON, 17)}</span>"
        f"<span class=icon-moon>{icon(MOON_ICON, 17)}</span>"
        "</button></div>"
    )


def page(
    title: str, body: str, extra_css: str = "", extra_js: str = "", active: str = ""
) -> bytes:
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        f"{THEME_BOOTSTRAP}"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<link rel=icon href='/favicon.ico' type='image/svg+xml'>"
        f"<title>{esc(title)}</title><style>{CSS}{extra_css}</style></head>"
        f"<body><div class=shell>{render_nav(active)}<div class=content>"
        f"{render_topbar()}<main>{body}</main></div></div>"
        f"<script>{JS}{extra_js}</script></body></html>"
    ).encode("utf-8")


def render_dashboard(state: dict) -> bytes:
    loops = [s for s in state["sessions"] if s["repo"]]
    others = [s for s in state["sessions"] if not s["repo"]]
    enabled = set(state["enabled"])

    recurring = [t for t in state["timers"] if t["unit"] == "delegation-loop.timer"]
    oneoffs = [t for t in state["timers"] if t["unit"] != "delegation-loop.timer"]
    nxt = recurring[0]["next"] if recurring and recurring[0]["next"] else None

    no_doc = [r for r in state["repos"] if r["state"] == "no-doc"]
    attention = []
    if not state["timer_active"]:
        attention.append("timer paused")
    if no_doc:
        attention.append(f"{len(no_doc)} repo(s) missing docs/delegation-loop.md")

    body = [f'<header><h1>{icon("M3 11l9-8 9 8M5 10v10h14V10")}Overview</h1></header>']

    # ---- the three questions the overview exists to answer ---------------
    body.append('<div class="stat-row">')
    body.append(
        '<div class="card stat">'
        f'<div class="stat-label">{icon("M17 2l4 4-4 4M3 11V9a3 3 0 013-3h15M7 22l-4-4 4-4M21 13v2a3 3 0 01-3 3H3", 13)}Live loops</div>'
        f'<div class="stat-value">{esc(len(loops))}</div>'
        f'<div class="stat-note">{esc(len(enabled))} repos enabled</div></div>'
    )
    if nxt:
        body.append(
            '<div class="card stat">'
            f'<div class="stat-label">{icon("M12 7v5l3 2M12 3a9 9 0 100 18 9 9 0 000-18z", 13)}Next run</div>'
            f'<div class="stat-value mono" data-until="{nxt:.0f}"></div>'
            f'<div class="stat-note">{esc(time.strftime("%a %H:%M:%S %Z", time.localtime(nxt)))}</div></div>'
        )
    else:
        body.append(
            '<div class="card stat">'
            f'<div class="stat-label">{icon("M12 7v5l3 2M12 3a9 9 0 100 18 9 9 0 000-18z", 13)}Next run</div>'
            '<div class="stat-value">None scheduled</div></div>'
        )
    body.append(
        f'<div class="card stat {"warn" if attention else "ok"}">'
        f'<div class="stat-label">{icon("M12 3l10 18H2zM12 10v5M12 18h.01", 13)}Needs attention</div>'
        f'<div class="stat-value">{esc("; ".join(attention)) if attention else "All clear"}</div></div>'
    )
    body.append("</div>")

    # ---- live sessions ----------------------------------------------------
    body.append(
        f'<div class="section-head">{icon("M17 2l4 4-4 4M3 11V9a3 3 0 013-3h15M7 22l-4-4 4-4M21 13v2a3 3 0 01-3 3H3", 15)}<h2>Live loops</h2></div>'
    )
    if not loops:
        body.append(
            "<div class='card loop-empty dim'>No loop session is open."
            + (
                f" Next run <span data-until='{nxt:.0f}'></span>."
                if nxt
                else " No next run scheduled."
            )
            + "</div>"
        )
    body.append('<div class="loop-grid">')
    for s in loops:
        dot = "ok" if s["attached"] or s["activity"] else "idle"
        body.append(
            f"<a class='card loop-card' href='/peek?repo={esc(s['repo'])}&lines=400'>"
        )
        body.append('<div class="loop-head">')
        body.append(f"<span class='dot {dot}'></span>")
        body.append(f"<b>{esc(s['repo'])}</b>")
        body.append(f"<span class=dim>started <span data-since='{s['created']}'></span></span>")
        body.append(f"<span class=dim>output <span data-since='{s['activity']}'></span></span>")
        if s["attached"]:
            body.append("<span class='pill on'>attached</span>")
        if s["repo"] not in enabled:
            body.append("<span class='pill off'>not in the scheduled set</span>")
        body.append("</div>")
        body.append(f"<pre class=loop-tail>{esc(s['tail'].rstrip() or '(no output)')}</pre>")
        body.append("</a>")
    body.append("</div>")

    if others:
        names = ", ".join(esc(s["name"]) for s in others)
        body.append(f"<div class='card dim'>Other tmux sessions (not loops): {names}</div>")

    # ---- coming up --------------------------------------------------------
    body.append(
        f'<div class="section-head">{icon("M12 7v5l3 2M12 3a9 9 0 100 18 9 9 0 000-18z", 15)}<h2>Coming up</h2></div>'
    )
    body.append("<div class='card scroll'><table>")
    body.append("<tr><th>repository</th><th>next</th><th>at</th><th>last</th></tr>")
    for t in recurring + oneoffs:
        when = f"<span data-until='{t['next']:.0f}'></span>" if t["next"] else "<span class=dim>-</span>"
        at = time.strftime("%a %H:%M:%S %Z", time.localtime(t["next"])) if t["next"] else "-"
        last = f"<span data-since='{t['last']:.0f}'></span>" if t["last"] else "<span class=dim>never</span>"
        body.append(
            f"<tr><td>{esc(timer_repository(t['unit']))}</td><td>{when}</td>"
            f"<td class=dim>{esc(at)}</td><td class=dim>{last}</td></tr>"
        )
    body.append("</table></div>")

    # ---- fleet (issue #15) -------------------------------------------
    body.append(
        f'<div class="section-head">{icon("M17 2l4 4-4 4M3 11V9a3 3 0 013-3h15M7 22l-4-4 4-4M21 13v2a3 3 0 01-3 3H3", 15)}<h2>Fleet</h2></div>'
    )
    fleet_error = state.get("fleet_error")
    if fleet_error:
        body.append(f"<div class='card dim'>Fleet registry unreachable: {esc(fleet_error)}</div>")
    else:
        fleet_machines = state.get("machines", [])
        if not fleet_machines:
            body.append("<div class='card dim'>No machines registered.</div>")
        else:
            body.append("<div class='card scroll'><table>")
            body.append("<tr><th>machine</th><th>state</th><th>version</th><th>heartbeat</th></tr>")
            for m in fleet_machines:
                pill = {
                    "online": "<span class='pill on'>online</span>",
                    "offline": "<span class='pill off'>offline</span>",
                }.get(m["state"], f"<span class=pill>{esc(m['state'])}</span>")
                version = esc(m.get("version") or "-")
                if m.get("version_mismatch"):
                    version += " <span class=pill>mismatch</span>"
                body.append(
                    f"<tr><td>{esc(m['name'])}</td><td>{pill}</td>"
                    f"<td class=dim>{version}</td><td class=dim>{esc(m.get('heartbeat') or '-')}</td></tr>"
                )
            body.append("</table></div>")

        fleet_claims = state.get("claims", {})
        if not fleet_claims:
            body.append("<div class='card dim'>No claimed issues.</div>")
        else:
            body.append("<div class='card scroll'><table>")
            body.append("<tr><th>issue</th><th>claimed by</th><th>host</th></tr>")
            for target, info in sorted(fleet_claims.items()):
                body.append(
                    f"<tr><td>{esc(target)}</td><td>{esc(info.get('session', '-'))}</td>"
                    f"<td class=dim>{esc(info.get('host', '-'))}</td></tr>"
                )
            body.append("</table></div>")

    # ---- repos --------------------------------------------------------
    body.append(
        f'<div class="section-head">{icon("M6 3v12M18 9a3 3 0 100-6 3 3 0 000 6zM6 21a3 3 0 100-6 3 3 0 000 6zM18 9a9 9 0 01-9 9", 15)}<h2>Repos</h2></div>'
    )
    body.append("<div class='card scroll'><table>")
    body.append(
        "<tr><th>repo</th><th>state</th><th>session</th><th>roadmap</th>"
        "<th>one-off command</th></tr>"
    )
    live = {s["repo"] for s in loops}
    for r in state["repos"]:
        pill = {
            "enabled": "<span class='pill on'>enabled</span>",
            "disabled": "<span class='pill off'>disabled</span>",
            "no-doc": "<span class=pill>no docs/delegation-loop.md</span>",
        }[r["state"]]
        sess = "live" if r["repo"] in live else "<span class=dim>-</span>"
        queue = (
            f"<a href='/roadmap?repo={quote(r['repo'], safe='')}'>open</a>"
            if r["loopable"]
            else "<span class=dim>-</span>"
        )
        once = ""
        if r["state"] == "enabled" and r["loopable"]:
            command = f"loopctl once {r['repo']} now"
            once = (
                f"<span data-once-repo='{esc(r['repo'])}'>"
                f"<code>{esc(command)}</code> "
                "<label>when <input value=now aria-label='one-off loop time'></label> "
                "<button type=button>copy</button> "
                "<span class=dim data-copy-status aria-live=polite></span></span>"
            )
        body.append(
            f"<tr><td>{esc(r['repo'])}</td><td>{pill}</td><td>{sess}</td>"
            f"<td>{queue}</td><td>{once}</td></tr>"
        )
    body.append("</table></div>")

    body.append(
        "<p class=dim style='margin-top:2rem'>Read-only. To change anything - "
        "start a loop, stop one, change the schedule - use "
        "<code>loopctl</code> over SSH. See docs/loopctl-gui-scope.md for "
        "why writes are not here yet. For recent issue activity across "
        "repos, see <a href='/roadmap'>Roadmap</a>.</p>"
    )
    return page("Overview", "".join(body), active="overview")


def time_until_reset(reset_at_ms, now_ms=None) -> str:
    if not isinstance(reset_at_ms, (int, float)):
        return "-"
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    remaining_ms = reset_at_ms - now_ms
    if remaining_ms <= 0:
        return "now"
    if remaining_ms < 60_000:
        return "<1m"
    remaining_minutes = (remaining_ms + 59_999) // 60_000
    days, remainder = divmod(remaining_minutes, 24 * 60)
    hours, minutes = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    return " ".join(parts)


def time_remaining_pct(row: dict, now_ms=None) -> float | None:
    duration = row.get("duration", QuotaDuration.OTHER)
    if not isinstance(duration, QuotaDuration):
        try:
            duration = QuotaDuration(duration)
        except (TypeError, ValueError):
            return None
    reset_at_ms = row.get("resets_at")
    if duration.milliseconds is None or not isinstance(reset_at_ms, (int, float)):
        return None
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    return min(100, max(0, (reset_at_ms - now_ms) / duration.milliseconds * 100))


def quota_duration_label(row: dict) -> str:
    duration = row.get("duration", QuotaDuration.OTHER)
    if isinstance(duration, QuotaDuration) and duration is not QuotaDuration.OTHER:
        return duration.label
    return row.get("label", "Other")


def quota_progress_pct(row: dict) -> float | None:
    used = row.get("used_pct")
    return min(100, max(0, used)) if isinstance(used, (int, float)) else None


def quota_elapsed_pct(row: dict, now_ms=None) -> float | None:
    remaining = time_remaining_pct(row, now_ms)
    return None if remaining is None else 100 - remaining


def quota_status(row: dict, now_ms=None) -> tuple[str, str]:
    used = quota_progress_pct(row)
    elapsed = quota_elapsed_pct(row, now_ms)
    if used is None or elapsed is None:
        return "Status unavailable", ""
    if used > elapsed + 5:
        return "Ahead of pace", "ahead"
    if used < elapsed - 15:
        return "Under pace", "under"
    return "On pace", ""


def render_quota_summary(rows: list[dict], now_ms: int) -> str:
    measured = [row for row in rows if quota_progress_pct(row) is not None]
    ahead = [row for row in measured if quota_status(row, now_ms)[1] == "ahead"]
    most_used = max(measured, key=quota_progress_pct, default=None)
    resets = [
        row for row in rows
        if isinstance(row.get("resets_at"), (int, float))
    ]
    next_reset = min(resets, key=lambda row: row["resets_at"], default=None)
    attention = (
        ", ".join(f"{row['provider']} {quota_duration_label(row)}" for row in ahead)
        if ahead else "None"
    )
    most_value = (
        f"{most_used['provider']} · {quota_duration_label(most_used)}"
        if most_used else "Not available"
    )
    most_note = (
        f"{quota_progress_pct(most_used):.0f}% used" if most_used else "No quota data"
    )
    next_value = (
        f"{next_reset['provider']} · {quota_duration_label(next_reset)}"
        if next_reset else "Not available"
    )
    next_note = (
        f"Resets in {time_until_reset(next_reset['resets_at'], now_ms)}"
        if next_reset else "No reset time"
    )
    items = (
        ("Needs attention", attention, "Usage is ahead of time elapsed" if ahead else "No quota is ahead of pace"),
        ("Most used", most_value, most_note),
        ("Next reset", next_value, next_note),
    )
    return "<div class=quota-summary>" + "".join(
        "<div class=quota-summary-item>"
        f"<div class=quota-summary-key>{esc(key)}</div>"
        f"<div class=quota-summary-value>{esc(value)}</div>"
        f"<div class=quota-summary-note>{esc(note)}</div></div>"
        for key, value, note in items
    ) + "</div>"


def render_quota_row(row: dict, now_ms: int) -> str:
    duration = row.get("duration", QuotaDuration.OTHER)
    duration_value = duration.value if isinstance(duration, QuotaDuration) else str(duration)
    reset_at_ms = row.get("resets_at")
    reset_timestamp = "" if reset_at_ms is None else str(reset_at_ms)
    used = quota_progress_pct(row)
    elapsed = quota_elapsed_pct(row, now_ms)
    status, status_class = quota_status(row, now_ms)
    available_text = "--" if used is None else f"{max(0, 100 - used):.0f}%"
    used_text = "--" if used is None else f"{used:.0f}%"
    used_width = "0%" if used is None else f"{used:.1f}%"
    elapsed_marker = (
        "" if elapsed is None
        else f"<div class=quota-meter-elapsed style='left:{elapsed:.1f}%' "
        "title='time elapsed in window'></div>"
    )
    aria_label = "quota usage unavailable"
    if used is not None and elapsed is not None:
        aria_label = f"{used:.0f}% used, {elapsed:.0f}% of window elapsed"
    elif used is not None:
        aria_label = f"{used:.0f}% used"
    return (
        f"<div class=quota-row data-window-duration='{esc(duration_value)}' "
        f"data-resets-at-ms='{esc(reset_timestamp)}'>"
        "<div><div class=quota-window>"
        f"{esc(quota_duration_label(row))}</div>"
        f"<div class='quota-status {status_class}'>{esc(status)}</div></div>"
        "<div>"
        f"<div class=quota-values><span><strong>{available_text}</strong> available</span>"
        f"<span>{used_text} used</span></div>"
        f"<div class=quota-meter role=img aria-label='{esc(aria_label)}'>"
        f"<div class='quota-meter-used {status_class}' style='width:{used_width}'></div>"
        f"{elapsed_marker}</div></div>"
        f"<div class=quota-reset><div class=quota-reset-left>"
        f"{esc(time_until_reset(reset_at_ms, now_ms))}</div>"
        f"<div class=quota-reset-at>{esc(epoch_ms_to_local(reset_at_ms))}</div></div>"
        "</div>"
    )


def render_usage() -> bytes:
    quota_rows = quota_usage()
    now_ms = int(time.time() * 1000)
    fetched = next((row["generated_at"] for row in quota_rows if "generated_at" in row), None)
    body = [
        f'<header><h1>{icon("M5 20V10M12 20V4M19 20v-7")}Usage</h1></header>',
        "<div class=quota-heading><h2>Quota</h2>"
        f"<span class=dim>Data timestamp: {esc(fetched) if fetched else 'not available'}</span></div>",
        render_quota_summary(quota_rows, now_ms),
        "<div class=quota-legend>"
        "<span class=quota-legend-item><span class=quota-legend-used></span>used</span>"
        "<span class=quota-legend-item><span class=quota-legend-time></span>"
        "time elapsed in window</span>"
        "<span class=quota-legend-item><span class=quota-legend-ahead></span>"
        "used faster than time</span></div><div class=quota-groups>",
    ]
    groups: dict[str, list[dict]] = {}
    for row in quota_rows:
        groups.setdefault(row["provider"], []).append(row)
    for provider, rows in groups.items():
        body.append(
            f"<section class='card quota-group'><div class=quota-group-heading>"
            f"<h3>{esc(provider)}</h3><span class=dim>"
            f"{esc(quota_source_label(provider))}</span></div>"
        )
        for row in rows:
            if "error" in row or "note" in row:
                message = row.get("error", row.get("note"))
                body.append(f"<p class=dim>{esc(message)}</p>")
            else:
                body.append(render_quota_row(row, now_ms))
        body.append("</section>")
    body.append(
        "</div><p class=dim>Bars show quota used; the marker shows time elapsed "
        "in the window. The reset time appears at the right. OpenCode Go uses "
        "omp or Orca's usage API; Claude uses Anthropic's OAuth usage API. "
        "OpenAI uses omp or Codex's latest local snapshot, which only updates "
        "when Codex writes a session event.</p>"
        "<h2>7-day totals</h2>"
        "<p class=dim>Sources are read locally. Claude's local cache reports "
        "one combined token total per day, not an input/output split, and no "
        "daily cost -- shown in the input-tokens column with cost as "
        "'not tracked'.</p>"
        "<div class='card scroll'><table><tr><th>provider</th>"
        "<th>input tokens</th><th>output tokens</th><th>cost</th>"
        "<th>period</th><th>source</th><th>last update</th></tr>"
    )
    rows = claude_usage() + omp_usage()
    for row in rows:
        if "error" in row:
            body.append(
                f"<tr><td>{esc(row['provider'])}</td><td colspan=3>"
                f"{esc(row['error'])}</td><td>last 7 days</td>"
                f"<td>{esc(row['source'])}</td><td>-</td></tr>"
            )
            continue
        output_tokens = "-" if row["output_tokens"] is None else esc(row["output_tokens"])
        cost = "not tracked" if row["cost"] is None else f"${row['cost']:.2f}"
        body.append(
            f"<tr><td>{esc(row['provider'])}</td>"
            f"<td>{esc(row['input_tokens'])}</td>"
            f"<td>{output_tokens}</td>"
            f"<td>{cost}</td><td>{esc(row['period'])}</td>"
            f"<td>{esc(row['source'])}</td><td>{esc(row['last_update'])}</td></tr>"
        )
    body.append("</table></div>")
    return page(
        "Agent usage", "".join(body), extra_css="main{max-width:none}", active="usage"
    )


def unavailable_tiers(error: Exception) -> list[dict]:
    return [{
        "error": f"unavailable ({type(error).__name__})",
        "source": MODEL_TIERS_PATH,
    }]


def model_tiers() -> list[dict]:
    """Read MODEL_TIERS_PATH, one row per task category."""
    try:
        with open(MODEL_TIERS_PATH, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError) as error:
        return unavailable_tiers(error)
    if not isinstance(raw, dict):
        return [{"error": "no categories in the file", "source": MODEL_TIERS_PATH}]
    rows = []
    for category, entry in raw.items():
        # Keys starting with "_" are file comments, not categories.
        if category.startswith("_") or not isinstance(entry, dict):
            continue
        tiers = entry.get("tiers")
        rows.append({
            "category": category,
            "source": entry.get("source", "not recorded"),
            "last_verified": entry.get("last_verified", "-"),
            "tiers": tiers if isinstance(tiers, dict) else {},
            "note": entry.get("note", ""),
        })
    return rows


def render_tier_picks(tiers: dict) -> str:
    """One row per tier: its ordered fallback chain, or that it has none."""
    rows = []
    for tier in MODEL_TIER_ORDER:
        entries = tiers.get(tier)
        picks = [
            pick
            for pick in (entries if isinstance(entries, list) else [])
            if isinstance(pick, dict)
        ]
        chain = "<span class=tier-arrow>&rarr;</span>".join(
            "<span class=tier-pick>"
            f"<span class=tier-model>{esc(pick.get('model', '-'))}</span>"
            f"<span class=dim>{esc(pick.get('effort', '-'))}</span>"
            "</span>"
            for pick in picks
        ) or "<span class='tier-pick dim'>none</span>"
        rows.append(
            f"<div class=tier-row><span class=tier-name>{esc(tier)}</span>"
            f"<div class=tier-picks>{chain}</div></div>"
        )
    return "".join(rows)


def render_model_tiers() -> bytes:
    rows = model_tiers()
    body = [
        f'<header><h1>{icon("M7 7h10v10H7zM9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3")}Models</h1></header>',
        "<h2>Routing by task category</h2>",
        "<p class=dim>Read from <code>"
        f"{esc(MODEL_TIERS_PATH)}</code>. Each tier is an ordered fallback "
        "chain: the first entry is tried first, then the next. A category "
        "with no entry for a tier escalates to the next tier up.</p>",
        "<div class=tier-grid>",
    ]
    for row in rows:
        if "error" in row:
            body.append(
                "<section class='card tier-card'>"
                f"<p class=dim>{esc(row['source'])}</p>"
                f"<p class=dim>{esc(row['error'])}</p></section>"
            )
            continue
        note = row["note"]
        body.append(
            "<section class='card tier-card'>"
            "<div class=tier-heading>"
            f"<h3>{esc(row['category'])}</h3>"
            f"<span class=dim>verified {esc(row['last_verified'])}</span></div>"
            f"<div class=dim>{esc(row['source'])}</div>"
            f"{render_tier_picks(row['tiers'])}"
            + (f"<p class='tier-note dim'>{esc(note)}</p>" if note else "")
            + "</section>"
        )
    if not rows:
        body.append("<div class='card dim'>No task categories.</div>")
    body.append("</div>")
    return page("Model tiers", "".join(body), active="models")


def _heartbeat_epoch(stamp: str | None) -> float | None:
    """`machines()`'s `heartbeat` field is an ISO stamp; the page's
    `data-since` ticker (see `JS` above) wants epoch seconds, same as
    `tmux_sessions()`'s `created`/`activity` fields.
    """
    if not stamp:
        return None
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _slot_controls(slot: str, current_max: int) -> str:
    """Two tiny forms, not one with a number input -- a GET-free "fewer" /
    "more" button pair needs no JS and matches the mockup's control shape.
    Posting the already-computed next value (not a +1/-1 delta) means the
    route has no read-modify-write race to get wrong.
    """
    fewer = max(1, current_max - 1)
    more = current_max + 1
    fewer_disabled = " disabled" if current_max <= 1 else ""
    return (
        "<form method=post action='/machines/slot-max' style='display:inline'>"
        f"<input type=hidden name=slot value='{esc(slot)}'>"
        f"<input type=hidden name=max value='{fewer}'>"
        f"<button type=submit{fewer_disabled}>fewer slots</button></form> "
        "<form method=post action='/machines/slot-max' style='display:inline'>"
        f"<input type=hidden name=slot value='{esc(slot)}'>"
        f"<input type=hidden name=max value='{more}'>"
        "<button type=submit>more slots</button></form>"
    )


def render_machines(records: list[dict], slot_status: dict) -> bytes:
    """The fleet machine list (issue #20).

    `slot_status` is a live `slots_redis.status()` read, not each record's
    own `slots` field -- that field is a snapshot taken at the machine's
    last `join`/`heartbeat` call (`machines.py`'s `_write_record`), so it
    would hide a slot-max change made through this page's own controls
    until the next heartbeat (up to 30s, longer if the heartbeat loop isn't
    running). `slots_redis.status()` has no such lag.

    v1 has exactly one fleet slot (`bmo`), shared by the whole fleet, not
    partitioned per machine -- so the same live numbers are shown, and the
    same controls apply, on every machine's card. Changing a slot's max
    from any one card changes it everywhere.
    """
    body = [f'<header><h1>{icon("M4 4h16v6H4zM4 14h16v6H4zM8 7h.01M8 17h.01")}Machines</h1></header>']
    if not records:
        body.append("<div class='card dim'>No machine has joined the fleet yet.</div>")
    for record in sorted(records, key=lambda r: r["name"]):
        state = record["state"]
        pill_class = {"online": "on", "offline": "off"}.get(state, "")
        body.append("<div class=card>")
        body.append("<div class=row>")
        body.append(f"<span class=big>{esc(record['name'])}</span>")
        body.append(f"<span class='pill {pill_class}'>{esc(state)}</span>")
        if record["version_mismatch"]:
            body.append(f"<span class='pill off'>version mismatch: {esc(record['version'] or '-')}</span>")
        else:
            body.append(f"<span class=dim>{esc(record['version'] or '-')}</span>")
        hb = _heartbeat_epoch(record.get("heartbeat"))
        if hb is not None:
            body.append(f"<span class=dim>heartbeat <span data-since='{hb:.0f}'></span></span>")
        else:
            body.append("<span class=dim>no heartbeat</span>")
        body.append("</div>")
        if slot_status:
            body.append("<table><tr><th>slot</th><th>holders</th><th>max</th><th></th></tr>")
            for slot_name, info in sorted(slot_status.items()):
                used = info.get("holders", 0)
                slot_max = info.get("max")
                controls = _slot_controls(slot_name, slot_max if slot_max is not None else 1)
                body.append(
                    f"<tr><td>{esc(slot_name)}</td><td>{esc(used)}</td>"
                    f"<td>{esc(slot_max) if slot_max is not None else '-'}</td>"
                    f"<td>{controls}</td></tr>"
                )
            body.append("</table>")
        else:
            body.append("<p class=dim>No slot data reported.</p>")
        body.append("</div>")
    return page("Machines", "".join(body), active="machines")


def render_error(msg: str) -> bytes:
    return page(
        "error",
        "<header><h1>Rejected</h1><span class=sp></span>"
        "<a href='/'>back</a></header>"
        f"<div class=card><p class=err>{esc(msg)}</p></div>",
        active="overview",
    )


# --------------------------------------------------------------------------
# server
# --------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "lupin"
    sys_version = ""
    peek_lines = 25
    allowed_hosts: set = set()
    # Named `fleet_connection`, not `connection` -- `socketserver`'s own
    # `BaseRequestHandler` already sets `self.connection` to the live
    # client socket, which would otherwise shadow this class attribute on
    # every real request (a bug caught by the real-HTTP tests, not the
    # mocked-Handler ones, since those never call setup()). Used by issue
    # #15's fleet data, issue #19's quest POST routes, and issue #20's
    # Machines page alike.
    fleet_connection: dict = {}

    def reply(self, body: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # Images load from the same-origin attachment route.
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; "
            "script-src 'unsafe-inline'; img-src 'self'; base-uri 'none'",
        )
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def reply_json(self, obj) -> None:
        body = json.dumps(obj, indent=2).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, location: str) -> None:
        """303: the browser re-GETs `location` instead of re-submitting
        the form that landed here (standard post/redirect/get)."""
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def host_ok(self) -> bool:
        """Block DNS rebinding: only the names we bound to are accepted."""
        host = (self.headers.get("Host") or "").strip().lower()
        return host in self.allowed_hosts

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def do_GET(self):  # noqa: N802
        if not self.host_ok():
            self.reply(render_error("bad Host header"), 421)
            return
        url = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}

        if url.path == "/favicon.ico":
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(FAVICON)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(FAVICON)
        elif url.path == "/":
            self.reply(render_dashboard(gather(self.peek_lines, self.fleet_connection)))
        elif url.path == "/roadmap":
            # roadmap.py owns this page's content; it only knows the shell's
            # 4-argument page_fn contract, so pin the "Roadmap" nav entry
            # here rather than changing that contract.
            def roadmap_page(title, body, extra_css="", extra_js=""):
                return page(title, body, extra_css, extra_js, active="roadmap")

            repos = roadmap.repository_names(code_repos())
            selected = query.get("repo", "").strip()
            if selected and selected not in repos:
                self.reply(render_error("unknown or non-loopable repository"), 404)
                return
            state = "closed" if query.get("state") == "closed" else "open"
            if state == "closed":
                issues_by_repo = {
                    name: roadmap.cached_github(
                        name, os.path.join(CODE_DIR, name), "closed"
                    )
                    for name in ([selected] if selected else repos)
                }
                body = roadmap.render_completed_page(
                    selected, repos, issues_by_repo, roadmap_page
                )
            elif selected:
                model = roadmap.cached_model(selected, os.path.join(CODE_DIR, selected))
                quest_state = self.quest_state(query.get("quest", "").strip())
                body = roadmap.render_page(selected, repos, model, roadmap_page, quest_state)
            else:
                models = {
                    repo: roadmap.cached_combined_model(repo, os.path.join(CODE_DIR, repo))
                    for repo in repos
                }
                body = roadmap.render_combined_page(repos, models, roadmap_page)
            self.reply(body)
        elif url.path == "/usage":
            self.reply(render_usage())
        elif url.path == "/model-tiers":
            self.reply(render_model_tiers())
        elif url.path == "/machines":
            try:
                records = machines.machines(self.fleet_connection)
                slot_status = slots_redis.status(**self.fleet_connection)
            except machines.CoordinatorUnreachable:
                self.reply(render_error("cannot reach the machine registry"), 502)
                return
            self.reply(render_machines(records, slot_status))
        elif url.path == "/api/state":
            self.reply_json(gather(self.peek_lines, self.fleet_connection))
        elif url.path == "/peek":
            self.do_peek(query)
        elif url.path == "/image":
            self.do_image(query)
        elif url.path == "/healthz":
            self.reply(b"ok")
        else:
            self.reply(render_error("no such page"), 404)

    def do_image(self, query: dict) -> None:
        attachment_id = query.get("id", "")
        if not ATTACHMENT_ID.fullmatch(attachment_id):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        image = github_attachment(attachment_id)
        if image is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        data, content_type = image
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=300")
        self.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def do_peek(self, query: dict) -> None:
        repo = query.get("repo", "").strip()
        if not repo or "/" in repo or repo in (".", ".."):
            self.reply(render_error("bad repo name"), 400)
            return
        lines = query.get("lines", "60").strip() or "60"
        if not lines.isdigit() or not (1 <= int(lines) <= 5000):
            self.reply(render_error("lines must be a number from 1 to 5000"), 400)
            return
        session = f"{SESSION_PREFIX}{repo}"
        out = session_tail(session, int(lines))
        body = (
            f"<header><h1>{esc(repo)}</h1><span class=sp></span>"
            "<a href='/'>back to dashboard</a></header>"
            f"<pre style='max-height:none'>{esc(out.rstrip() or '(no output)')}</pre>"
        )
        self.reply(page(f"peek {repo}", body, active="overview"))

    def quest_state(self, quest_id: str) -> dict | None:
        """Read `quest:<quest_id>` and work out which of its issues are
        still claimed (in progress) versus released (done, closed, or
        merged). Returns `None` if there's no id, no such quest, or Redis
        can't be reached -- the roadmap page just skips the progress card
        in that case rather than failing the whole (read-only) page.
        """
        if not quest_id:
            return None
        try:
            record = quest.read_quest(quest_id, self.fleet_connection)
        except CoordinatorUnreachable:
            return None
        if record is None:
            return None
        targets = record.get("targets", [])
        owner_repos = sorted({target.rpartition("#")[0] for target in targets})
        try:
            held = claims.claims_for(owner_repos, **self.fleet_connection) if owner_repos else {}
        except CoordinatorUnreachable:
            held = {}
        holder = f"quest:{quest_id}"
        pending, done = [], []
        for number, target in zip(record.get("issues", []), targets):
            if held.get(target, {}).get("session") == holder:
                pending.append(number)
            else:
                done.append(number)
        return {
            "id": quest_id,
            "machine": record.get("machine"),
            "state": record.get("state"),
            "pending": pending,
            "done": done,
            "total": len(record.get("issues", [])),
        }

    def do_POST(self):  # noqa: N802
        if not self.host_ok():
            self.reply(render_error("bad Host header"), 421)
            return
        url = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.reply(render_error("bad Content-Length header"), 400)
            return
        length = max(0, min(length, MAX_FORM_BYTES))
        raw = self.rfile.read(length) if length else b""
        try:
            form = parse_qs(raw.decode("utf-8"))
        except UnicodeDecodeError:
            self.reply(render_error("request body must be utf-8"), 400)
            return

        if url.path == "/quest/start":
            self.do_quest_start(form)
        elif url.path == "/quest/stop":
            self.do_quest_stop(form)
        elif url.path == "/machines/slot-max":
            self.do_set_slot_max(form)
        else:
            self.reply(render_error("no such page"), 404)

    def do_quest_start(self, form: dict) -> None:
        repo = form.get("repo", [""])[0].strip()
        try:
            issue_numbers = [int(value) for value in form.get("issue", [])]
        except ValueError:
            self.reply(render_error("bad issue number"), 400)
            return
        if not issue_numbers:
            self.reply(render_error("select at least one issue to start a quest"), 400)
            return
        try:
            result = quest.start(issue_numbers, enabled_repos(), connection=self.fleet_connection)
        except quest.QuestError as exc:
            self.reply(render_error(f"quest start failed: {exc}"), 400)
            return
        except CoordinatorUnreachable as exc:
            self.reply(render_error(f"cannot reach the redis coordinator: {exc}"), 502)
            return
        query = f"quest={quote(result['id'], safe='')}"
        if repo:
            query = f"repo={quote(repo, safe='')}&{query}"
        self.redirect(f"/roadmap?{query}")

    def do_quest_stop(self, form: dict) -> None:
        quest_id = form.get("id", [""])[0].strip()
        repo = form.get("repo", [""])[0].strip()
        if not quest_id:
            self.reply(render_error("missing quest id"), 400)
            return
        try:
            quest.stop(quest_id, connection=self.fleet_connection)
        except quest.QuestError as exc:
            self.reply(render_error(f"quest stop failed: {exc}"), 400)
            return
        except CoordinatorUnreachable as exc:
            self.reply(render_error(f"cannot reach the redis coordinator: {exc}"), 502)
            return
        self.redirect(f"/roadmap?repo={quote(repo, safe='')}" if repo else "/roadmap")

    def do_set_slot_max(self, form: dict) -> None:
        slot = form.get("slot", [""])[0].strip()
        raw_max = form.get("max", [""])[0].strip()
        try:
            # int(), not raw_max.isdigit(): isdigit() also accepts Unicode
            # digits like superscript two ('²') that int() then
            # can't parse, which used to crash this handler.
            max_value = int(raw_max)
        except ValueError:
            max_value = None
        if not slot or max_value is None or max_value < 1:
            self.reply(render_error("bad slot-max request"), 400)
            return
        try:
            slots_redis.set_max(slot, max_value, **self.fleet_connection)
        except machines.CoordinatorUnreachable:
            self.reply(render_error("cannot reach the machine registry"), 502)
            return
        self.send_response(303)
        self.send_header("Location", "/machines")
        self.send_header("Content-Length", "0")
        self.end_headers()


def _roadmap_rows(model: dict) -> list[dict]:
    buckets = {
        number: stage["name"]
        for stage in model["stages"]
        for number in stage["numbers"]
    }
    rows = []
    for node in model["nodes"]:
        number = node["number"]
        incoming = [
            edge for edge in model["edges"]
            if edge["to"] == number and edge["kind"] in {"depends", "parent", "split"}
        ]
        rows.append(
            {
                "number": number,
                "priority": node["priority"],
                "size": node["size"],
                "bucket": buckets.get(number, ""),
                "comments": len(node["comments"]),
                "deps": sorted(
                    {edge["from"] for edge in incoming if edge["kind"] == "depends"}
                ),
                "parents": sorted(
                    {edge["from"] for edge in incoming if edge["kind"] in {"parent", "split"}}
                ),
                "title": node["title"],
                "body": node["body"],
                "commentText": [comment["body"] for comment in node["comments"]],
            }
        )
    return rows


def _print_roadmap(repo: str, model: dict, verbose: bool, as_json: bool) -> None:
    rows = _roadmap_rows(model)
    if as_json:
        if not verbose:
            for row in rows:
                del row["title"]
                del row["body"]
                del row["commentText"]
        print(json.dumps({"repo": repo, "issues": rows}, ensure_ascii=False, indent=2))
        return
    for row in rows:
        refs = []
        if row["deps"]:
            refs.append("deps:" + ",".join(f"#{number}" for number in row["deps"]))
        if row["parents"]:
            refs.append("parent:" + ",".join(f"#{number}" for number in row["parents"]))
        suffix = " " + " ".join(refs) if refs else ""
        print(
            f"#{row['number']} {row['bucket']} {row['priority']} {row['size']} "
            f"{row['comments']} comments{suffix}"
        )
        if verbose:
            node = next(node for node in model["nodes"] if node["number"] == row["number"])
            print(f"  {row['title']}")
            print(f"  {row['body']}")
            for index, comment in enumerate(node["comments"], 1):
                print(f"  Comment {index}: {comment['body']}")


def main(argv: list[str] | None = None) -> int:
    """Serve the dashboard, or print one repository's roadmap.

    `argv` is the argument list without the leading subcommand name. The
    caller owns the argument surface (see `lupin.cli`), so this only parses
    what it needs and ignores `None` to read sys.argv.
    """
    import argparse

    ap = argparse.ArgumentParser(prog="lupin serve", add_help=False)
    ap.add_argument("--bind", default="127.0.0.1", help="loopback or a tailnet address")
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--peek-lines", type=int, default=25, help="tail lines shown per loop")
    ap.add_argument("--roadmap", metavar="REPO", help="print a repository roadmap and exit")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--json", action="store_true")
    # Fleet data (issue #15, also used by issue #19's quest POST routes and
    # issue #20's Machines page): same flags and env-var fallback as
    # cli.py's `_fleet_connection_args`, kept in sync by hand since serve.py
    # parses its own argv independently of cli.py (see this function's
    # docstring) and importing cli.py here would be circular.
    ap.add_argument("--redis-host", default=os.environ.get("LUPIN_REDIS_HOST"))
    ap.add_argument(
        "--redis-port", type=int,
        default=int(os.environ["LUPIN_REDIS_PORT"]) if os.environ.get("LUPIN_REDIS_PORT") else None,
    )
    ap.add_argument("--redis-username", default=os.environ.get("LUPIN_REDIS_USERNAME"))
    ap.add_argument("--redis-password", default=os.environ.get("LUPIN_REDIS_PASSWORD"))
    ap.add_argument(
        "--config-path", default=os.environ.get("LUPIN_FLEET_CONFIG"),
        help="fleet config file (default: $LUPIN_FLEET_CONFIG or ~/.config/lupin/fleet.json)",
    )
    args, _unknown = ap.parse_known_args(argv)

    if args.roadmap:
        repos = roadmap.repository_names(code_repos())
        if args.roadmap not in repos:
            print(f"lupin: unknown repository {args.roadmap!r}", file=sys.stderr)
            return 2
        model = roadmap.cached_model(args.roadmap, os.path.join(CODE_DIR, args.roadmap))
        _print_roadmap(args.roadmap, model, args.verbose, args.json)
        return 0


    try:
        addr = ipaddress.ip_address(args.bind)
    except ValueError:
        print(f"lupin: --bind must be an IP address, got {args.bind!r}", file=sys.stderr)
        return 2
    if not bind_allowed(addr):
        print(
            f"lupin: refusing to bind {args.bind}. This dashboard is meant "
            "for loopback or a tailnet address (100.64.0.0/10) only. Use an "
            "SSH forward, or a tailnet ACL, to reach it from another machine.",
            file=sys.stderr,
        )
        return 2

    class Server(ThreadingHTTPServer):
        address_family = socket.AF_INET6 if addr.version == 6 else socket.AF_INET
        daemon_threads = True

    Handler.peek_lines = args.peek_lines
    Handler.allowed_hosts = {
        f"127.0.0.1:{args.port}",
        f"localhost:{args.port}",
        f"[::1]:{args.port}",
        f"{args.bind}:{args.port}",
    }
    Handler.fleet_connection = machines.resolve_connection(
        redis_host=args.redis_host,
        redis_port=args.redis_port,
        redis_username=args.redis_username,
        redis_password=args.redis_password,
        config_path=args.config_path,
    )

    httpd = Server((args.bind, args.port), Handler)
    print(f"lupin on http://{args.bind}:{args.port}", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
