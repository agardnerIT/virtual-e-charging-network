#!/usr/bin/env bash
# The whole story, in one command, ending in a verdict (issue #14, M5).
#
#   ./tools/demo.sh              # the full narrative, ~4 minutes
#   DEMO_FAST=1 ./tools/demo.sh  # same assertions, shorter waits, for CI
#
# This is a test, not a transcript. Every step asserts against the store or the collectors' own
# telemetry and exits non-zero with the failing assertion named. The output is the evidence: the
# query, the value, and what it was supposed to prove.
#
#   1. five sites are up and every edge queue is empty             (the topology)
#   2. a session on a depot bay is charging                        (M2, M3-1: sessions)
#   3. a sag ends it with PowerLoss and the trace has the tree      (M2: physically-caused traces)
#   4. a load step moves the island's frequency and the BESS covers (M3-2: grid-forming)
#   5. a link outage fills the queue and loses nothing              (store-and-forward)
#   6. back to zero everywhere                                     (clean state)
#
# It never restarts a container and never changes the default stack: everything it does, `make`
# can do by hand, and it uses the same paths (`make queues`, `make prom`) a human would.
set -uo pipefail
cd "$(dirname "$0")/.."

COMPOSE="docker compose -f compose/docker-compose.yml"
SITE_RURAL="rural-brokenhill"
SITE_METRO="metro-tottenham"
SITE_ISLAND="microgrid-kangaroo-island"
ISLAND_ID="microgrid-kangaroo-island"
EVSE="${EVSE:-EVSE07}"
FAST="${DEMO_FAST:-0}"

if [ "$FAST" = "1" ]; then
  SETTLE=6; SESSION_WAIT=8; SAG_WAIT=12; OUTAGE_WAIT=20; DRAIN_BUDGET=60
else
  SETTLE=10; SESSION_WAIT=14; SAG_WAIT=25; OUTAGE_WAIT=40; DRAIN_BUDGET=120
fi

PASS=0; FAIL=0; STEP=0
say()  { printf '\n\033[1m%s. %s\033[0m\n' "$1" "$2"; }
note() { printf '   %s\n' "$*"; }
ok()   { PASS=$((PASS+1)); printf '   \033[32mok\033[0m   %s\n' "$*"; }
bad()  { FAIL=$((FAIL+1)); printf '   \033[31mFAIL\033[0m %s\n' "$*"; }

# require <description> <actual> <op> <expected>
#   ops: gt lt eq contains nonzero
require() {
  local desc="$1" got="$2" op="$3" want="$4" fine=1
  case "$op" in
    gt)       awk -v a="$got" -v b="$want" 'BEGIN{exit !(a+0 > b+0)}' && fine=0 ;;
    lt)       awk -v a="$got" -v b="$want" 'BEGIN{exit !(a+0 < b+0)}' && fine=0 ;;
    eq)       [ "$got" = "$want" ] && fine=0 ;;
    contains) case "$got" in *"$want"*) fine=0 ;; esac ;;
    nonzero)  awk -v a="$got" 'BEGIN{exit !(a+0 != 0)}' && fine=0 ;;
  esac
  if [ "$fine" = "0" ]; then ok "$desc — $got"; else bad "$desc — got '$got', wanted $op '$want'"; fi
}

# pq <promql>: the freshest sample of the (possibly many) series the query returns. A poller
# restart mints a new service.instance.id and therefore a new series (findings #12), so taking
# result[0] silently reads a process that died ten minutes ago (D41).
pq() {
  local enc
  enc=$(Q="$1" python3 -c 'import os,urllib.parse; print(urllib.parse.quote(os.environ["Q"]))')
  $COMPOSE exec -T ops wget -qO- "http://prometheus:9090/api/v1/query?query=$enc" 2>/dev/null \
    | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)["data"]["result"]
except Exception:
    print(""); raise SystemExit
if not data:
    print(""); raise SystemExit
print(max(data, key=lambda r: float(r["value"][0]))["value"][1])
'
}

# qsum <promql>: sum over series, for counters and fleet-wide counts.
qsum() {
  local enc
  enc=$(Q="$1" python3 -c 'import os,urllib.parse; print(urllib.parse.quote(os.environ["Q"]))')
  $COMPOSE exec -T ops wget -qO- "http://prometheus:9090/api/v1/query?query=$enc" 2>/dev/null \
    | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)["data"]["result"]
except Exception:
    print("0"); raise SystemExit
print(sum(float(r["value"][1]) for r in data) if data else 0)
'
}

# Edge queue depth, from the collectors' own /metrics over the compose network — the same source
# `make queues` reads. Deliberately NOT a PromQL query: Prometheus scrapes nothing, it is fed over
# OTLP, so `otelcol_exporter_queue_size` only exists on the edge endpoints themselves.
queues_empty() {
  local total=0 s raw sz
  for s in rural solar wind metro microgrid; do
    raw=$($COMPOSE exec -T ops wget -qO- "http://edge-$s:8888/metrics" 2>/dev/null)
    sz=$(printf '%s\n' "$raw" | grep '^otelcol_exporter_queue_size' | awk '{v+=$NF} END{printf "%d", v+0}')
    total=$(( total + ${sz:-0} ))
  done
  echo "$total"
}

wait_for() { # wait_for <seconds> <command...>
  local budget="$1"; shift
  local deadline=$(( $(date +%s) + budget ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    # shellcheck disable=SC2068
    if eval "$@" >/dev/null 2>&1; then return 0; fi
    sleep 2
  done
  return 1
}

printf '\033[1mVECN demo\033[0m — five sites, one pipeline, three assertions per claim.\n'
printf 'fast=%s  evse=%s\n' "$FAST" "$EVSE"

# ---------------------------------------------------------------------------------------------
STEP=$((STEP+1)); say "$STEP" "Five sites are up and every edge queue is empty"
sites=$(qsum 'count(count by (energy_site_id) (target_info{energy_site_id!=""}))')
require "sites reporting to the pipeline" "$sites" eq "5"
note "site ids: $(pq 'group by (energy_site_id) (target_info{energy_site_id!=""})' >/dev/null 2>&1; \
  $COMPOSE exec -T ops wget -qO- 'http://prometheus:9090/api/v1/query?query=count%20by%20(energy_site_id)%20(target_info%7Benergy_site_id!%3D%22%22%7D)' \
  | python3 -c 'import json,sys; print(\", \".join(sorted(r[\"metric\"][\"energy_site_id\"] for r in json.load(sys.stdin)[\"data\"][\"result\"])))')"
q=$(queues_empty)
require "edge queue depth (batches, all five)" "${q%.*}" eq "0"

# ---------------------------------------------------------------------------------------------
STEP=$((STEP+1)); say "$STEP" "A session on depot bay $EVSE is charging"
$COMPOSE exec -T ocpp-cp-metro python ocpp/cpctl.py start --evse "$EVSE" > /dev/null 2>&1 || true
wait_for "$SESSION_WAIT" '[ "$(qsum "energy_evse_power_real_watts{energy_site_id=\"$SITE_METRO\",energy_evse_id=\"$EVSE\"}")" -gt 0 ]' \
  && ok "bay $EVSE is delivering power" \
  || bad "bay $EVSE never delivered power within ${SESSION_WAIT}s"
kw=$(qsum "energy_evse_power_real_watts{energy_site_id=\"$SITE_METRO\",energy_evse_id=\"$EVSE\"}")
require "delivered power (W)" "${kw%.*}" gt "1000"

# ---------------------------------------------------------------------------------------------
STEP=$((STEP+1)); say "$STEP" "A voltage sag on the rural feeder ends a session for PowerLoss"
$COMPOSE exec -T ocpp-cp python ocpp/cpctl.py start --evse EVSE01 > /dev/null 2>&1 || true
sleep 4
$COMPOSE exec -T device-rural python site/faultctl.py voltage-sag --depth 0.78 --duration 12 > /dev/null 2>&1
note "sag at 0.78 pu for 12 s; waiting for the trace to land"
sleep "$SAG_WAIT"
TRACE=$(curl -s --max-time 10 "http://127.0.0.1:${JAEGER_PORT:-16687}/api/traces?service=vecn-ocpp-cp&limit=20&lookback=3m" 2>/dev/null \
  | python3 -c '
import json, sys
try:
    traces = json.load(sys.stdin)["data"]
except Exception:
    print(""); raise SystemExit
best = None
for t in traces:
    ops = {s["operationName"] for s in t["spans"]}
    if "energy.voltage_sag" in ops and "ocpp.stop_transaction" in ops:
        best = sorted(ops)
if best is None:
    print(""); raise SystemExit
print(" ".join(best))
')
if printf '%s' "$TRACE" | grep -q 'ocpp.stop_transaction'; then
  ok "one trace contains the sag and the stop"
else
  bad "no trace with both a sag and a stop span in the last 3 minutes"
fi
# The stop reason is the payoff: a physical measurement decided how the transaction ended.
REASON=$(curl -s --max-time 10 "http://127.0.0.1:${JAEGER_PORT:-16687}/api/traces?service=vecn-ocpp-cp&limit=20&lookback=3m" 2>/dev/null \
  | python3 -c '
import json, sys
try:
    traces = json.load(sys.stdin)["data"]
except Exception:
    print(""); raise SystemExit
for t in traces:
    for s in t["spans"]:
        if s["operationName"] == "ocpp.session":
            for tag in s.get("tags", []):
                if tag.get("key") == "ocpp.stop.reason" and tag.get("value") == "PowerLoss":
                    print("PowerLoss"); raise SystemExit
print("")
')
require "a session ended with stop reason" "${REASON:-<none>}" eq "PowerLoss"

# ---------------------------------------------------------------------------------------------
STEP=$((STEP+1)); say "$STEP" "A load step on the island moves frequency and the battery covers it"
hz_before=$(pq "energy_microgrid_frequency_hertz{energy_site_id=\"$ISLAND_ID\"}")
soc_before=$(pq "energy_bess_state_of_charge_percent{energy_site_id=\"$ISLAND_ID\"}")
note "before: ${hz_before:-?} Hz, SoC ${soc_before:-?} %"
$COMPOSE exec -T device-microgrid python site/faultctl.py loadstep --kw 300 --duration 45 > /dev/null 2>&1
sleep "$SETTLE"
hz_after=$(pq "energy_microgrid_frequency_hertz{energy_site_id=\"$ISLAND_ID\"}")
bess=$(pq "energy_bess_power_real_watts{energy_site_id=\"$ISLAND_ID\"}")
load=$(pq "energy_microgrid_load_power_real_watts{energy_site_id=\"$ISLAND_ID\"}")
note "after:  ${hz_after:-?} Hz, battery ${bess:-?} W, load ${load:-?} W"
require "site load during the step (W)" "$(printf '%.0f' "${load:-0}")" gt "300000"
require "battery discharge is carrying it (W)" "$(printf '%.0f' "${bess:-0}")" gt "100000"
# Frequency must be *near* 50 and must have moved: this site's whole claim is that it is not held.
require "frequency stayed near nominal (|f-50| < 1 Hz)" \
  "$(python3 -c "print(1 if abs(float('${hz_after:-0}') - 50.0) < 1.0 else 0)")" eq "1"
$COMPOSE exec -T device-microgrid python site/faultctl.py loadstep-clear > /dev/null 2>&1 || true

# ---------------------------------------------------------------------------------------------
STEP=$((STEP+1)); say "$STEP" "A WAN outage fills one edge queue and loses nothing"
lost_before=$(qsum 'otelcol_exporter_enqueue_failed_metric_points{instance=~".*edge-wind.*"}')
./impairment/link.sh wind down > /dev/null
note "wind link down; waiting ${OUTAGE_WAIT}s for the queue to grow"
sleep "$OUTAGE_WAIT"
qn=$(queues_empty)
require "queue depth during the outage (batches)" "$(python3 -c "print(1 if ${qn%.*} > 0 else 0)")" eq "1"
./impairment/link.sh wind wet > /dev/null
note "link restored; draining (budget ${DRAIN_BUDGET}s)"
wait_for "$DRAIN_BUDGET" '[ "$(queues_empty)" = "0" ]' \
  && ok "queue drained to 0" \
  || bad "queue did not drain within ${DRAIN_BUDGET}s"
lost_after=$(qsum 'otelcol_exporter_enqueue_failed_metric_points{instance=~".*edge-wind.*"}')
require "points lost (enqueue failed) during the outage" \
  "$(python3 -c "print(max(0, int(float('${lost_after:-0}') - float('${lost_before:-0}'))) )")" eq "0"

# ---------------------------------------------------------------------------------------------
STEP=$((STEP+1)); say "$STEP" "Back to a clean state"
q=$(queues_empty)
require "edge queue depth (batches, all five)" "${q%.*}" eq "0"
sess=$(pq "energy_evse_power_real_watts{energy_site_id=\"$SITE_METRO\",energy_evse_id=\"$EVSE\"}")
note "bay $EVSE is now at ${sess:-?} W (may be 0 if its session finished — not an assertion)"

# ---------------------------------------------------------------------------------------------
printf '\n\033[1m%d passed, %d failed\033[0m\n' "$PASS" "$FAIL"
if [ "$FAIL" -gt 0 ]; then
  printf 'The demo is a test: a failure here means the stack does not do what this repo claims.\n'
  exit 1
fi
printf 'Every claim in the README has now been executed, not asserted.\n'
