#!/usr/bin/env bash
# Lint the newest formal review on a pull request against the code-review format.
#
# Usage:
#   review-lint.sh --repo OWNER/REPO PR [--review-id ID]
#
# Read the newest APPROVED or CHANGES_REQUESTED review, or the review that
# --review-id names, and print ok or FAIL for each required part of the format.
# The parts are the first Round line, Summary:, Model:, Verdict:, one command
# or probe, a caller grep, and Not checked:.
#
# Exit 0 when every part passes. Exit 1 when a part fails. Exit 2 on a usage
# error or a read error.
#
# Token: set GH_TOKEN, or set GH_TOKEN_FILE to a file that holds the token.
# The script never prints the token.
set -u

usage() {
  sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'
}

repo=""
pr=""
review_id=""
while [ $# -gt 0 ]; do
  case "$1" in
    --repo | --review-id)
      opt=$1
      if [ $# -lt 2 ] || [ -z "$2" ]; then
        echo "review-lint.sh: $opt needs a value" >&2
        exit 2
      fi
      val=$2
      shift 2
      if [ "$opt" = "--repo" ]; then repo=$val; else review_id=$val; fi
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    -*)
      echo "review-lint.sh: unknown flag: $1" >&2
      exit 2
      ;;
    *)
      pr=$1
      shift
      ;;
  esac
done

if [ -z "$repo" ] || [ -z "$pr" ]; then
  echo "review-lint.sh: usage: review-lint.sh --repo OWNER/REPO PR [--review-id ID]" >&2
  exit 2
fi

if [ -z "${GH_TOKEN:-}" ] && [ -n "${GH_TOKEN_FILE:-}" ]; then
  if [ ! -r "$GH_TOKEN_FILE" ]; then
    echo "review-lint.sh: cannot read GH_TOKEN_FILE" >&2
    exit 2
  fi
  GH_TOKEN=$(cat "$GH_TOKEN_FILE")
  export GH_TOKEN
fi

if [ -n "$review_id" ]; then
  body=$(gh api "repos/$repo/pulls/$pr/reviews/$review_id" --jq .body) || exit 2
else
  body=$(gh pr view "$pr" --repo "$repo" --json reviews \
    --jq '[.reviews[]|select(.state=="APPROVED" or .state=="CHANGES_REQUESTED")]|last|.body') || exit 2
fi
if [ -z "$body" ]; then
  echo "review-lint.sh: no formal review on #$pr" >&2
  exit 2
fi

bad=0
chk() {
  label=$1
  pattern=$2
  if printf '%s' "$body" | grep -qE "$pattern"; then
    echo "ok   $label"
  else
    echo "FAIL $label"
    bad=1
  fi
}

chk "Round line first" '^Round [0-9]+\. LEVEL: size-[a-z]+\. Target:'
chk "Summary line" 'Summary:'
chk "Model line" '^Model:'
chk "Verdict line" 'Verdict:'
chk "a command or a probe" '(grep|npx|npm|pytest|playwright|command|probe|tried|input|empty|zero)'
chk "callers checked" '(grep|callers)'
chk "Not checked line" 'Not checked:'
printf '%s' "$body" | head -1 | cut -c1-90
exit "$bad"
