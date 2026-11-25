#!/usr/bin/env bash
# Check that a visual pull request carries pictures on the pull request itself.
#
# Usage:
#   pr-evidence-check.sh --repo OWNER/REPO [PR ...]
#   pr-evidence-check.sh --repo OWNER/REPO --visual-glob REGEX [--visual-glob REGEX]...
#
# With no PR, the script checks every open pull request. A pull request is
# visual when a changed file matches a visual glob and is not under tests/.
# A visual pull request must have at least --min-pictures image attachments in
# a comment on the pull request, and a "User impact:" line in its body. A body
# that points the reader at the linked issue fails the check.
#
# Print one line per pull request: ok, FAIL, or n/a. Exit 0 when no pull
# request fails. Exit 1 when one fails. Exit 2 on a usage error.
#
# Token: set GH_TOKEN, or set GH_TOKEN_FILE to a file that holds the token.
# The script never prints the token.
set -u

usage() {
  sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
}

repo=""
min_pictures=2
user_globs=()
while [ $# -gt 0 ]; do
  case "$1" in
    --repo | --visual-glob | --min-pictures)
      opt=$1
      if [ $# -lt 2 ] || [ -z "$2" ]; then
        echo "pr-evidence-check.sh: $opt needs a value" >&2
        exit 2
      fi
      val=$2
      shift 2
      case "$opt" in
        --repo) repo=$val ;;
        --visual-glob) user_globs+=("$val") ;;
        --min-pictures) min_pictures=$val ;;
      esac
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    --)
      shift
      break
      ;;
    -*)
      echo "pr-evidence-check.sh: unknown flag: $1" >&2
      exit 2
      ;;
    *)
      break
      ;;
  esac
done

if [ -z "$repo" ]; then
  echo "pr-evidence-check.sh: --repo OWNER/REPO is required" >&2
  exit 2
fi
case "$min_pictures" in
  '' | *[!0-9]*)
    echo "pr-evidence-check.sh: --min-pictures needs a number" >&2
    exit 2
    ;;
esac

if [ -z "${GH_TOKEN:-}" ] && [ -n "${GH_TOKEN_FILE:-}" ]; then
  if [ ! -r "$GH_TOKEN_FILE" ]; then
    echo "pr-evidence-check.sh: cannot read GH_TOKEN_FILE" >&2
    exit 2
  fi
  GH_TOKEN=$(cat "$GH_TOKEN_FILE")
  export GH_TOKEN
fi

# A repository with other paths passes its own --visual-glob values.
globs=('^(src/ui|src/gl|src/styles|src/components|app/|site/|site-src/|docs/mockups|src/core/export)' '\.(svelte|vue|css|scss|html)$')
if [ ${#user_globs[@]} -gt 0 ]; then
  globs=("${user_globs[@]}")
fi
visual_re=$(printf '%s\n' "${globs[@]}" | paste -sd '|' -)

prs=("$@")
if [ ${#prs[@]} -eq 0 ]; then
  list=$(gh pr list --repo "$repo" --state open --limit 200 --json number --jq '.[].number') || exit 2
  mapfile -t prs <<<"$list"
fi

bad=0
for n in "${prs[@]}"; do
  [ -n "$n" ] || continue
  files=$(gh pr view "$n" --repo "$repo" --json files --jq '.files[].path') || {
    echo "error #$n: cannot read the changed files" >&2
    bad=1
    continue
  }
  vis=$(printf '%s\n' "$files" | grep -E "$visual_re" | grep -vE '^tests/' | head -1)
  if [ -z "$vis" ]; then
    echo "n/a   #$n (no visual files)"
    continue
  fi
  cnt=$(gh api --paginate "repos/$repo/issues/$n/comments" \
    --jq '[.[]|(.body|[match("user-attachments/assets/";"g")]|length)]|add // 0' |
    awk '{s+=$1} END{print s+0}') || cnt=0
  body=$(gh pr view "$n" --repo "$repo" --json body --jq .body) || body=""
  issue_only=$(printf '%s' "$body" | grep -ciE 'attached (in|on) the issue|issue comment')
  impact=$(printf '%s' "$body" | grep -ciE '^User impact:')
  msg=""
  [ "$cnt" -lt "$min_pictures" ] && msg="$msg pictures-on-the-PR=$cnt(need $min_pictures)"
  [ "$issue_only" -gt 0 ] && msg="$msg body-says-pictures-are-on-the-issue"
  [ "$impact" -lt 1 ] && msg="$msg no-'User impact:'-line"
  if [ -z "$msg" ]; then
    echo "ok    #$n pictures=$cnt (visual: $vis)"
  else
    echo "FAIL  #$n:$msg (visual: $vis)"
    bad=1
  fi
done
exit "$bad"
