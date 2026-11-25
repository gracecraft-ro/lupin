#!/usr/bin/env bash
# Append one quota sample and print the burn per hour for each 7-day window.
#
# Usage:
#   spend-log.sh [--log FILE] [--quota-check PATH]
#
# --log defaults to $XDG_STATE_HOME/lupin/quota-samples.tsv.
# --quota-check defaults to quota-check.sh in this script's directory.
#
# Exit 0 on success. Exit 2 on a usage error or when quota-check.sh fails.
set -u

usage() {
  sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
}

script_dir=$(cd -- "$(dirname -- "$0")" && pwd)
state_home=${XDG_STATE_HOME:-$HOME/.local/state}
log=$state_home/lupin/quota-samples.tsv
quota_check=$script_dir/quota-check.sh
while [ $# -gt 0 ]; do
  case "$1" in
    --log | --quota-check)
      opt=$1
      if [ $# -lt 2 ] || [ -z "$2" ]; then
        echo "spend-log.sh: $opt needs a value" >&2
        exit 2
      fi
      val=$2
      shift 2
      if [ "$opt" = "--log" ]; then log=$val; else quota_check=$val; fi
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      echo "spend-log.sh: unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [ ! -x "$quota_check" ]; then
  echo "spend-log.sh: cannot run $quota_check" >&2
  exit 2
fi

now=$(date +%s)
out=$("$quota_check" 2>/dev/null) || {
  echo "spend-log.sh: quota-check failed" >&2
  exit 2
}
printf '%s\n' "$out" | awk -v t="$now" '
  / 7 days / {
    pct = ""
    for (i = 1; i <= NF; i++) {
      if ($i == "days") {
        pct = $(i + 1)
        sub("%", "", pct)
      }
    }
    if (pct != "") print t "\t" $1 "\t" pct
  }' >>"$log"

python3 - "$log" "$now" <<'PY'
import collections, sys

log, now = sys.argv[1], int(sys.argv[2])
rows = []
with open(log) as handle:
    for line in handle:
        fields = line.rstrip("\n").split("\t")
        if len(fields) != 3:
            continue
        rows.append(fields)
by_provider = collections.defaultdict(list)
for t, provider, value in rows:
    by_provider[provider].append((int(t), float(value)))
for provider, samples in by_provider.items():
    current = samples[-1]
    for hours in (1, 3):
        old = [row for row in samples if now - row[0] >= hours * 3600 - 600]
        if not old:
            continue
        previous = old[-1]
        span = (current[0] - previous[0]) / 3600
        if span > 0:
            print(f"BURN {provider} 7d: {current[1]:.0f}% left, "
                  f"{(previous[1] - current[1]) / span:.2f} %/h over {span:.1f} h")
PY
