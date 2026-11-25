#!/usr/bin/env bash
# Print SPEND, HOLD, STOP, PACE, and MEM lines for the provider quota.
#
# Usage:
#   quota-check.sh [--samples FILE] [--pace-provider PROVIDER]
#                  [--low-mb N] [--tight-mb N]
#
# Read `lupin quota` and print one line per window:
#   STOP   no quota left
#   SPEND  spend down to the 10% owner reserve
#   SPEND  spend to 0: the window resets in under one hour
#   HOLD   the reserve is all that is left
# Claude gets THROTTLE instead of SPEND, because the 7-day window governs it.
#
# The PACE line compares the measured burn rate with the sustainable rate
# (percent left divided by hours to reset). Samples are appended to --samples.
# The default path is $XDG_STATE_HOME/lupin/quota-samples.tsv.
#
# The MEM line reads MemAvailable and swap. The --low-mb and --tight-mb values
# must match the machine's memory.
#
# Exit 0 on success. Exit 2 on a usage error or a parse error.
set -u

usage() {
  sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
}

state_home=${XDG_STATE_HOME:-$HOME/.local/state}
samples=$state_home/lupin/quota-samples.tsv
pace_provider=claude
low_mb=4000
tight_mb=8000
while [ $# -gt 0 ]; do
  case "$1" in
    --samples | --pace-provider | --low-mb | --tight-mb)
      opt=$1
      if [ $# -lt 2 ] || [ -z "$2" ]; then
        echo "quota-check.sh: $opt needs a value" >&2
        exit 2
      fi
      val=$2
      shift 2
      case "$opt" in
        --samples) samples=$val ;;
        --pace-provider) pace_provider=$val ;;
        --low-mb) low_mb=$val ;;
        --tight-mb) tight_mb=$val ;;
      esac
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      echo "quota-check.sh: unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

quota=$(lupin quota 2>&1) || {
  echo "quota-check.sh: lupin quota failed" >&2
  exit 2
}
printf '%s\n' "$quota" | python3 -c '
import re, sys

for line in sys.stdin:
    m = re.match(r"(\S+)\s+(?:open|BLOCKED)\s+(\S+(?: \S+)?)\s+(\d+)% left\s+resets in (.+?)\s+\(", line)
    if not m:
        continue
    prov, window, left, resets = m.group(1), m.group(2), int(m.group(3)), m.group(4)
    mins = 0
    for n, u in re.findall(r"(\d+)([dhm])", resets):
        mins += int(n) * {"d": 1440, "h": 60, "m": 1}[u]
    if left == 0:
        verdict = "STOP (empty)"
    elif mins <= 60:
        verdict = "SPEND to 0 (last hour)"
    elif left > 10:
        verdict = "SPEND to 10% reserve"
    else:
        verdict = "HOLD (reserve)"
    if prov == "claude" and verdict.startswith("SPEND"):
        verdict = "THROTTLE: see PACE line (7d governs), do not spend to 0"
    print(f"{prov:12} {window:8} {left:3}% left, resets {resets:14} -> {verdict}")
'

# PACE: sustainable burn = percent left / hours to reset. Each sample file row
# is (epoch, provider, percent left) for a 7-day window.
QUOTA_TEXT=$quota python3 - "$samples" "$pace_provider" <<'PY'
import os, re, sys, time

samples, provider = sys.argv[1], sys.argv[2]
text = os.environ.get("QUOTA_TEXT", "")
pattern = rf"^{re.escape(provider)}\s+\S+\s+7 days\s+(\d+)% left\s+resets in (.+?)\s+\("
m = re.search(pattern, text, re.M)
if not m:
    sys.exit(0)
left = int(m.group(1))
mins = sum(int(n) * {"d": 1440, "h": 60, "m": 1}[u] for n, u in re.findall(r"(\d+)([dhm])", m.group(2)))
now = int(time.time())
rows = []
if os.path.exists(samples):
    with open(samples) as handle:
        for line in handle:
            fields = line.rstrip("\n").split("\t")
            if len(fields) != 3 or fields[1] != provider:
                continue
            rows.append((int(fields[0]), int(fields[2])))
if not rows or now - rows[-1][0] > 300:
    directory = os.path.dirname(samples)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(samples, "a") as handle:
        handle.write(f"{now}\t{provider}\t{left}\n")
    rows.append((now, left))
hours_left = mins / 60
sustain = left / hours_left if hours_left else 0
old = [row for row in rows if 7200 <= now - row[0] <= 21600]
if old:
    t0, l0 = old[0]
    burn = (l0 - left) / ((now - t0) / 3600)
    verdict = "TOO FAST: throttle" if burn > sustain * 1.2 else "ok"
    print(f"PACE {provider} 7d: {left}% left, {hours_left:.0f}h to reset. "
          f"Sustainable {sustain:.2f} %/h. Measured {burn:.2f} %/h over {(now - t0) / 3600:.1f} h -> {verdict}")
else:
    print(f"PACE {provider} 7d: {left}% left, {hours_left:.0f}h to reset. "
          f"Sustainable {sustain:.2f} %/h. (no 2h sample yet)")
PY

if command -v free >/dev/null 2>&1; then
  free -m | awk -v low="$low_mb" -v tight="$tight_mb" '
    /^Mem:/ {avail = $7; total = $2}
    /^Swap:/ {swap_used = $3; swap_total = $2}
    END {
      if (avail < low) v = "LOW: close finished panes, cap the workers at 3"
      else if (avail < tight) v = "TIGHT: cap the workers at 5"
      else v = "ok"
      printf "MEM available %d MB of %d MB, swap used %d of %d MB -> %s\n", avail, total, swap_used, swap_total, v
    }'
else
  echo "MEM unavailable: no free command on this system"
fi
omp_n=$(pgrep -fc 'omp --model' 2>/dev/null) || omp_n=0
echo "MEM omp processes: ${omp_n}"
