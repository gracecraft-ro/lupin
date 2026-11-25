#!/usr/bin/env bash
# Poll open pull requests and print a line only when a head or its newest review
# changes. The first sighting of a pull request is recorded without a line.
#
# Usage:
#   pr-watch.sh --repo OWNER/REPO [--interval SECONDS]
#
# Stop with Ctrl-C. A change line has this shape:
#   PR 123 CHANGED: abc123456 OPEN REVIEW_REQUIRED | 4567 APPROVED def789012 (was: ...)
#
# Token: set GH_TOKEN, or set GH_TOKEN_FILE to a file that holds the token.
set -u

usage() {
  sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
}

repo=""
interval=300
while [ $# -gt 0 ]; do
  case "$1" in
    --repo | --interval)
      opt=$1
      if [ $# -lt 2 ] || [ -z "$2" ]; then
        echo "pr-watch.sh: $opt needs a value" >&2
        exit 2
      fi
      val=$2
      shift 2
      if [ "$opt" = "--repo" ]; then repo=$val; else interval=$val; fi
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      echo "pr-watch.sh: unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [ -z "$repo" ]; then
  echo "pr-watch.sh: --repo OWNER/REPO is required" >&2
  exit 2
fi
case "$interval" in
  '' | *[!0-9]*)
    echo "pr-watch.sh: --interval needs a number" >&2
    exit 2
    ;;
esac

if [ -z "${GH_TOKEN:-}" ] && [ -n "${GH_TOKEN_FILE:-}" ]; then
  if [ ! -r "$GH_TOKEN_FILE" ]; then
    echo "pr-watch.sh: cannot read GH_TOKEN_FILE" >&2
    exit 2
  fi
  GH_TOKEN=$(cat "$GH_TOKEN_FILE")
  export GH_TOKEN
fi

state=$(mktemp -d)
trap 'rm -rf "$state"' EXIT
while true; do
  numbers=$(gh pr list --repo "$repo" --state open --limit 100 --json number --jq '.[].number' 2>/dev/null) || numbers=""
  for n in $numbers; do
    json=$(gh pr view "$n" --repo "$repo" --json headRefOid,state,reviewDecision 2>/dev/null) || continue
    head=$(printf '%s' "$json" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["headRefOid"][:9], d["state"], d.get("reviewDecision") or "-")')
    review=$(gh api "repos/$repo/pulls/$n/reviews" --jq 'last | "\(.id) \(.state) \(.commit_id[0:9])"' 2>/dev/null) || review="-"
    current="$head | $review"
    previous=""
    [ -f "$state/$n" ] && previous=$(cat "$state/$n")
    if [ -n "$previous" ] && [ "$current" != "$previous" ]; then
      echo "PR $n CHANGED: $current (was: $previous)"
    fi
    printf '%s' "$current" >"$state/$n"
  done
  sleep "$interval"
done
