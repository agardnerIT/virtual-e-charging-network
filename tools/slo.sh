#!/usr/bin/env bash
# The four SLOs, from the store, with targets (M5, #13).
#
#   ./tools/slo.sh
#
# Query first, target second: each SLO is a PromQL expression over telemetry that already exists,
# and the target is stated next to it so it can be argued with. Nothing here writes to the stack.
#
# **These are lifetime ratios, not windowed ones**, and that is deliberate. The CSMS and the charge
# point keep their session counters in memory (D34), so "since this process started" is the only
# window that means anything: `increase(counter[1h])` on a counter that has incremented once
# returns 0, because Prometheus cannot see the step before the first sample it scraped. Found the
# hard way — an SLO board that says "n/a" for a healthy stack is worse than no board. Windowed
# versions are one function away once the counters are durable.
#
# The interesting one is power-quality exposure: it counts sessions in which the supply derate
# engaged, divided by sessions stopped. That is the number a network operator would ask for after
# a week of sags, and it is not derivable from any single session.
set -uo pipefail
cd "$(dirname "$0")/.."

WINDOW="${WINDOW:-1h}"
COMPOSE="docker compose -f compose/docker-compose.yml"

q() { # q <promql> -> a number, or "n/a"
  local enc
  enc=$(Q="$1" python3 -c 'import os,urllib.parse; print(urllib.parse.quote(os.environ["Q"]))')
  $COMPOSE exec -T ops wget -qO- "http://prometheus:9090/api/v1/query?query=$enc" 2>/dev/null \
    | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)["data"]["result"]
except Exception:
    print("n/a"); raise SystemExit
if not data:
    print("n/a"); raise SystemExit
v = data[0]["value"][1]
try:
    f = float(v)
    print("n/a" if f != f else (f"{f:.4f}" if abs(f) < 1000 else f"{f:.0f}"))
except ValueError:
    print(v)
'
}

row() { # row <name> <value> <target text> <PASS condition as awk>
  local name="$1" value="$2" target="$3" cond="$4" mark="--"
  # $cond is an awk expression over `v`, e.g. 'v >= 0.95'. (Using `$1` here reads an empty
  # BEGIN field and marks a perfect 1.0 as FAIL — which it did, once.)
  if [ "$value" != "n/a" ] && awk -v v="$value" "BEGIN{exit !($cond)}"; then mark=$'\033[32mPASS\033[0m'
  elif [ "$value" = "n/a" ]; then mark=$'\033[33m  --\033[0m'
  else mark=$'\033[31mFAIL\033[0m'; fi
  printf '%-4s %-34s %-10s %s\n' "$mark" "$name" "$value" "$target"
}

STOPPED='sum(ocpp_transactions_stopped_total{ocpp_stop_reason="Local"})'
ALLSTOP='sum(ocpp_transactions_stopped_total)'
WINDOW="since the charge point and CSMS processes started"

printf '\nSLOs %s (session-level metrics; the CSMS is in-memory by design, D34)\n\n' "$WINDOW"
printf '     %-34s %-10s %s\n' "SLO" "value" "target"

row "session success rate" \
    "$(q "($STOPPED) / ($ALLSTOP)")" \
    ">= 0.95" 'v >= 0.95'

row "time to first power, p95" \
    "$(q "histogram_quantile(0.95, sum by (le) (energy_session_time_to_first_power_seconds_bucket))")" \
    "<= 30 s" 'v <= 30'

row "delivered / requested energy" \
    "$(q "sum(energy_session_energy_delivered_kW_h_total) / sum(energy_session_energy_requested_kW_h_total)")" \
    ">= 0.90" 'v >= 0.90'

row "power-quality events per 100 sessions" \
    "$(q "100 * (sum(energy_session_power_quality_affected_total) or vector(0)) / ($ALLSTOP)")" \
    "<= 5" 'v <= 5'

printf '\n'
printf 'session success   = ocpp_transactions_stopped{reason="Local"} / all stopped\n'
printf 'time to first power = histogram_quantile over energy_session_time_to_first_power_seconds\n'
printf 'delivered/requested = energy_session_energy_delivered / .requested (a derate shows as < 1)\n'
printf 'PQ exposure       = sessions where the supply derate engaged / sessions stopped x 100\n'
printf '\nThe targets are this repo'"'"'s, not an industry standard: they are set where the demo can\n'
printf 'pass on a quiet day and fail under make spine. docs/alarms.md records the reasoning.\n'
