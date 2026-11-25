#!/usr/bin/env bash
# Kill dev servers that a finished worker left behind.
#
# Usage:
#   reap-vite.sh [--keep PATH]... [--age-hours N] [--dry-run]
#
# Match every process that runs node_modules/.bin/vite. Kill a process when it
# is older than --age-hours and its command line does not contain a --keep
# path. Keep the shared checkout's server so that an open preview keeps working.
# Print the swap use after the kill.
#
# Exit 0 on success. Exit 2 on a usage error.
set -u

usage() {
  sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'
}

age_hours=6
dry_run=0
keeps=()
while [ $# -gt 0 ]; do
  case "$1" in
    --keep | --age-hours)
      opt=$1
      if [ $# -lt 2 ] || [ -z "$2" ]; then
        echo "reap-vite.sh: $opt needs a value" >&2
        exit 2
      fi
      val=$2
      shift 2
      if [ "$opt" = "--keep" ]; then keeps+=("$val"); else age_hours=$val; fi
      ;;
    --dry-run)
      dry_run=1
      shift
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      echo "reap-vite.sh: unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

case "$age_hours" in
  '' | *[!0-9]*)
    echo "reap-vite.sh: --age-hours needs a number" >&2
    exit 2
    ;;
esac
max_age=$((age_hours * 3600))

# One --keep path becomes an awk variable. Zero paths keep nothing.
keep_re=""
if [ ${#keeps[@]} -gt 0 ]; then
  keep_re=$(printf '%s\n' "${keeps[@]}" | paste -sd '|' -)
fi

list=$(mktemp)
# pgrep reports no process age, and the age is the whole point. Read etimes
# from ps instead.
# shellcheck disable=SC2009
ps -eo pid,etimes,args | grep "[n]ode_modules/.bin/vite" | awk -v age="$max_age" -v keep="$keep_re" '
  $2 > age {
    if (keep != "" && index($0, keep) != 0) next
    print $1
  }' >"$list"
count=$(wc -l <"$list")
while read -r pid; do
  [ -n "$pid" ] || continue
  if [ "$dry_run" -eq 1 ]; then
    echo "would kill $pid"
  else
    kill "$pid" && echo "killed $pid"
  fi
done <"$list"
rm -f "$list"
echo "matched $count dev servers older than ${age_hours}h"

if command -v free >/dev/null 2>&1; then
  free -m | awk '/Swap/ {print "swap used", $3, "of", $2, "MB"}'
fi
