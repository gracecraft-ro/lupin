#!/usr/bin/env python3
"""List owner text on open pull requests that no later `Loop:` comment cites.

Owner text is a comment, review, or inline comment by the owner login whose body
does not start with `Loop:` or `Round N.` and is not empty. Any review state
counts, `COMMENTED` too. The head commit time is not used: a sync merge must not
hide an ask. An ask is answered when a later `Loop:` comment contains its id.

Usage:
  owner-asks.py --repo OWNER/REPO --owner LOGIN [--token-file FILE]

Exit 0 when every ask is answered. Exit 1 when an ask is unanswered. Exit 2 on a
usage error. The script never prints the token.
"""

import argparse
import json
import os
import re
import subprocess
import sys


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--repo",
        default=None,
        help="OWNER/REPO. Default: the repo of the current checkout.",
    )
    parser.add_argument("--owner", required=True, help="The owner's GitHub login.")
    parser.add_argument(
        "--token-file",
        default=os.environ.get("GH_TOKEN_FILE"),
        help="File that holds the token. Default: $GH_TOKEN_FILE.",
    )
    return parser.parse_args(argv)


def gh_env(args):
    env = dict(os.environ)
    if args.token_file:
        try:
            with open(args.token_file) as handle:
                env["GH_TOKEN"] = handle.read().strip()
        except OSError as error:
            print(f"owner-asks.py: cannot read --token-file: {error.strerror}", file=sys.stderr)
            sys.exit(2)
    return env


def make_gh(env):
    """Run gh. A failed read is fatal: a clean board must never hide a failed read."""

    def gh(*a):
        result = subprocess.run(["gh", *a], capture_output=True, text=True, env=env)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "no output"
            print(f"owner-asks.py: gh {' '.join(a)} failed: {detail}", file=sys.stderr)
            raise SystemExit(2)
        return result.stdout

    return gh


def paged(gh, path):
    out = gh("api", "--paginate", path)
    # --paginate on arrays concatenates JSON arrays: ][ -> ,
    out = re.sub(r"\]\s*\[", ",", out.strip() or "[]")
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        print(f"owner-asks.py: cannot read {path}", file=sys.stderr)
        raise SystemExit(2)


def own_text(body):
    """Owner text outside quoted lines: a reply that quotes a Loop: comment is still an ask."""
    return "\n".join(line for line in body.splitlines() if not line.lstrip().startswith(">")).strip()


def is_machine(body):
    """True when the text is machine-written.

    A worker can post under the owner login. Machine text starts with `Loop:` or
    `Round N.`, or it is long, headed, and names a commit, a verdict, or a
    command. Until loop work has its own GitHub login, this heuristic is the
    only way to tell the two apart.
    """
    body = own_text(body)
    if not body:
        return True
    if body.startswith("Loop:") or re.match(r"Round \d+\.", body) or body.startswith("@/"):
        return True
    machine = (
        len(body) > 700
        or "##" in body
        or re.search(r"\b[0-9a-f]{9,40}\b", body)
        or re.search(r"(?i)\b(verdict|command:|head `|head:|gate run|fix for|new head)", body)
        or re.search(r"\bR\d+-F\d+\b|^Re: |Not checked:|^Round \d", body)
    )
    return bool(machine)


def main(argv):
    args = parse_args(argv)
    env = gh_env(args)
    gh = make_gh(env)

    repo = args.repo
    if not repo:
        repo = gh("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner").strip()
        if not repo:
            print("owner-asks.py: --repo is required when the checkout has no repository", file=sys.stderr)
            return 2

    owner = args.owner
    listed = gh("pr", "list", "--repo", repo, "--state", "open", "--limit", "200", "--json", "number,isDraft")
    try:
        prs = json.loads(listed or "[]")
    except json.JSONDecodeError:
        print("owner-asks.py: cannot list the open pull requests", file=sys.stderr)
        return 2

    bad = 0
    for pr in prs:
        n = pr["number"]
        items = []
        for r in paged(gh, f"repos/{repo}/pulls/{n}/reviews"):
            items.append(("review", r["id"], r["submitted_at"] or "", r["body"] or "", r["user"]["login"]))
        for c in paged(gh, f"repos/{repo}/issues/{n}/comments"):
            items.append(("comment", c["id"], c["created_at"], c["body"] or "", c["user"]["login"]))
        for c in paged(gh, f"repos/{repo}/pulls/{n}/comments"):
            items.append(("inline", c["id"], c["created_at"], c["body"] or "", c["user"]["login"]))
        loops = [(t, b) for (_, _, t, b, _) in items if b.startswith("Loop:")]
        for kind, i, t, b, u in sorted(items, key=lambda item: item[2]):
            if u != owner or not b.strip() or is_machine(b):
                continue
            # "see my latest comment" style reviews are still asks, so list them.
            cited = any(loop_time > t and str(i) in loop_body for loop_time, loop_body in loops)
            if not cited:
                bad += 1
                print(f"UNANSWERED #{n} {kind} {i} {t[:16]}Z :: {b.strip()[:110]!r}")
    print(f"unanswered owner asks in {repo}: {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
