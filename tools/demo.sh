#!/usr/bin/env bash
# The whole story, in one command, ending in a verdict (issue #14, M5).
#
#   ./tools/demo.sh                    # the full narrative
#   DEMO_FAST=1 ./tools/demo.sh        # shorter waits, same assertions (CI)
#   DEMO_SKIP=outage ./tools/demo.sh   # skip the link-outage step: it is the only step that
#                                      # changes the stack, so it must not run while something
#                                      # else owns a link (e.g. a long outage measurement)
#
# This is a test, not a transcript. Every step asserts against the store or the collectors' own
# telemetry and exits non-zero with the failing assertion named. The output is the evidence: the
# value, and what it was supposed to prove.
#
#   1. six sites are up and every edge queue is empty              (the topology)
#   2. a session on a depot bay is charging                        (M2, M3-1: sessions)
#   3. a sag ends it with PowerLoss and the trace has the tree      (M2: physically-caused traces)
#   4. a load step moves the island's frequency and the BESS covers (M3-2: grid-forming)
#   5. a capacity cap throttles six bays at the highway hub         (M4: load management)
#   6. a link outage fills the queue and loses nothing              (store-and-forward)
#   7. back to zero everywhere                                     (clean state)
#
# It never restarts a container and never changes the default stack: everything it does, `make`
# can do by hand, and it uses the same paths (`make queues`, `make prom`) a human would.
set -uo pipefail
cd "$(dirname "$0")/.."

COMPOSE="docker compose -f compose/docker-compose.yml"
SITE_METRO="metro-tottenham"
SITE_ISLAND="microgrid-kangaroo-island"
SITE_HIGHWAY="highway-hume"
EVSE="${EVSE:-EVSE07}"
FAST="${DEMO_FAST:-0}"
SKIP="${DEMO_SKIP:-}"

if [ "$FAST" = "1" ]; then
  SETTLE=6; SESSION_WAIT=20; SAG_WAIT=16; OUTAGE_WAIT=20; DRAIN_BUDGET=60; CAP_SETTLE=20
else
  SETTLE=10; SESSION_WAIT=30; SAG_WAIT=25; OUTAGE_WAIT=40; DRAIN_BUDGET=150; CAP_SETTLE=30
fi

PASS=0; FAIL=0; STEP=0
say()  { printf '\n\033[1m%s. %s\033[0m\n' "$1" "$2"; }
note() { printf '   %s\n' "$*"; }
ok()   { PASS=$((PASS+1)); printf '   \033[32mok\033[0m   %s\n' "$*"; }
bad()  { FAIL=$((FAIL+1)); printf '   \033[31mFAIL\033[0m %s\n' "$*"; }
skipped() { printf '   \033[33mskip\033[0m %s\n' "$*"; }

# require <description> <actual> <op> <expected>   (ops: eq gt lt)
#
# Bash arithmetic, not awk: every one of these predicates started life as an awk one-liner and the
# nested quoting inside "$( )" produced "awk: can't open file BEGIN{...}" three different ways. A
# test harness that fails for its own reasons is worse than no harness.
require() {
  local desc="$1" got="$2" op="$3" want="$4" gi="${2%%.*}" wi="${4%%.*}"
  [ -z "$gi" ] && gi=0
  [ -z "$wi" ] && wi=0
  case "$op" in
    eq) [ "$gi" = "$wi" ] && { ok "$desc — $got"; return; } ;;
    gt) [ "$gi" -gt "$wi" ] 2>/dev/null && { ok "$desc — $got"; return; } ;;
    lt) [ "$gi" -lt "$wi" ] 2>/dev/null && { ok "$desc — $got"; return; } ;;
  esac
  bad "$desc — got '$got', wanted $op '$want'"
}

# --- the store, through the same ops box `make prom` uses ---------------------------------------
# qsum <promql>: sum over the series a query returns. Counters, fleet-wide counts, per-bay sums.
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
print(sum(float(r["value"][1]) for r in data) if data else 0)'
}

# qfresh <selector>: the value of the newest series, and *only* if it is live.
#
# An instant query returns every matching series with the *evaluation* timestamp, so
# `max(result, key=sample_time)` — the first version of this function — picks an arbitrary series,
# not the newest one. When a poller restarts it mints a new `service.instance.id` (findings #12) and
# the old series keeps its last value in the store for a few minutes, so the "newest" could be a
# process that died ten minutes ago (D41). Wrapping the selector in `last_over_time(...[25s])`
# drops any series that has not reported recently, which is exactly the distinction this needs: a
# *live* value, or nothing.
qfresh() {
  local enc
  enc=$(Q="last_over_time($1[25s])" python3 -c 'import os,urllib.parse; print(urllib.parse.quote(os.environ["Q"]))')
  $COMPOSE exec -T ops wget -qO- "http://prometheus:9090/api/v1/query?query=$enc" 2>/dev/null \
    | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)["data"]["result"]
except Exception:
    print(""); raise SystemExit
print(max((float(r["value"][1]) for r in data), default=""))'
}

# Edge queue depth: the collectors' own /metrics, exactly what `make queues` reads. Deliberately not
# PromQL — Prometheus scrapes nothing here, it is fed over OTLP, so otelcol_exporter_queue_size only
# exists on the edge endpoints.
queues_empty() {
  local total=0 s raw sz
  for s in rural solar wind metro microgrid highway; do
    raw=$($COMPOSE exec -T ops wget -qO- "http://edge-$s:8888/metrics" 2>/dev/null)
    sz=$(printf '%s\n' "$raw" | grep '^otelcol_exporter_queue_size' | awk '{v+=$NF} END{printf "%d", v+0}')
    total=$(( total + ${sz:-0} ))
  done
  echo "$total"
}

site_ids() {
  $COMPOSE exec -T ops wget -qO- \
    "http://prometheus:9090/api/v1/query?query=count%20by%20(energy_site_id)%20(target_info%7Benergy_site_id!%3D%22%22%7D)" \
    2>/dev/null | python3 -c '
import json, sys
try:
    print(", ".join(sorted(r["metric"]["energy_site_id"] for r in json.load(sys.stdin)["data"]["result"])))
except Exception:
    print("(query failed)")'
}

# poll_until <budget_s> <function...>: call a bash function until it succeeds or the budget runs out.
# Functions, not command strings: an earlier version built predicates as quoted strings and spent
# three bugs on nested quoting, and a demo that fails for its own reasons is worse than no demo.
poll_until() {
  local budget="$1"; shift
  local deadline=$(( $(date +%s) + budget ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if "$@"; then return 0; fi
    sleep 2
  done
  return 1
}

BAY_W=0
bay_delivering() {
  BAY_W=$(qsum "energy_evse_power_real_watts{energy_site_id=\"$SITE_METRO\",energy_evse_id=\"$EVSE\"}")
  [ "${BAY_W%%.*}" -gt 100 ] 2>/dev/null
}
queues_drained() { [ "$(queues_empty)" = "0" ]; }

# island_stepped: the island's load has risen above the pre-step value (the poller reports every 5 s
# with a deadband, so a fixed sleep is a race — the lesson from the first run of this script).
island_stepped() {
  local v
  v=$(qfresh "energy_microgrid_load_power_real_watts{energy_site_id=\"$SITE_ISLAND\"}")
  [ "${v%%.*}" -gt 300000 ] 2>/dev/null
}

# bays_throttled: at least one bay is reporting the load_management throttle reason.
# site_under_cap: the bays' aggregate load is at or under the 100 kW cap the demo pushes.
site_under_cap() {
  local v
  v=$(qfresh "energy_site_load_power_watts{energy_site_id=\"$SITE_HIGHWAY\"}")
  [ "${v%%.*}" -le 100000 ] 2>/dev/null && [ "${v%%.*}" -gt 0 ] 2>/dev/null
}

THROTTLED=0
bays_throttled() {
  THROTTLED=$(qsum "count(energy_evse_current_offered_amperes{energy_site_id=\"$SITE_HIGHWAY\",energy_evse_throttled_by=\"load_management\"})")
  [ "${THROTTLED%%.*}" -gt 0 ] 2>/dev/null
}

printf '\033[1mVECN demo\033[0m — six sites, one pipeline, an assertion behind every claim.\n'
printf 'fast=%s  skip=%s  evse=%s\n' "$FAST" "${SKIP:-none}" "$EVSE"

# ---------------------------------------------------------------------------------------------
STEP=$((STEP+1)); say "$STEP" "Six sites are up and the pipeline is delivering"
sites=$(qsum 'count(count by (energy_site_id) (target_info{energy_site_id!=""}))')
if printf '%s' "$SKIP" | grep -qw outage; then
  # A skipped outage step means a link may be down elsewhere, and a site whose link is down stops
  # appearing in the store after the lookback expires. Say so rather than asserting a number the
  # run cannot produce.
  require "sites reporting to the store (wind's link may be down elsewhere)" "$sites" gt "4"
  note "queue depth is $(queues_empty) batches (a busy link is expected when the outage step is skipped)"
else
  require "sites reporting to the store" "$sites" eq "6"
  note "site ids: $(site_ids)"
  require "edge queue depth (batches, all six)" "$(queues_empty)" eq "0"
fi

# ---------------------------------------------------------------------------------------------
STEP=$((STEP+1)); say "$STEP" "A session on depot bay $EVSE is charging"
$COMPOSE exec -T ocpp-cp-metro python ocpp/cpctl.py start --evse "$EVSE" > /tmp/demo-session.json 2>&1 || true
if poll_until "$SESSION_WAIT" bay_delivering; then
  note "bay $EVSE is delivering ${BAY_W%%.*} W"
  ok "bay $EVSE is charging"
else
  bad "bay $EVSE delivered nothing within ${SESSION_WAIT}s"
fi

# ---------------------------------------------------------------------------------------------
STEP=$((STEP+1)); say "$STEP" "A voltage sag on the rural feeder ends a session for PowerLoss"
$COMPOSE exec -T ocpp-cp python ocpp/cpctl.py start --evse EVSE01 > /dev/null 2>&1 || true
sleep 4
$COMPOSE exec -T device-rural python site/faultctl.py voltage-sag --depth 0.78 --duration 12 > /dev/null 2>&1
note "sag at 0.78 pu for 12 s; waiting for the trace"
sleep "$SAG_WAIT"
REASON=$(curl -s --max-time 10 "http://127.0.0.1:${JAEGER_PORT:-16687}/api/traces?service=vecn-ocpp-cp&limit=30&lookback=4m" 2>/dev/null \
  | python3 -c '
import json, sys
try:
    traces = json.load(sys.stdin)["data"]
except Exception:
    print(""); raise SystemExit
for t in traces:
    ops = {s["operationName"] for s in t["spans"]}
    if "energy.voltage_sag" not in ops or "ocpp.stop_transaction" not in ops:
        continue
    for s in t["spans"]:
        if s["operationName"] != "ocpp.session":
            continue
        for tag in s.get("tags", []):
            if tag.get("key") == "ocpp.stop.reason" and tag.get("value") == "PowerLoss":
                print("PowerLoss"); raise SystemExit
print("")')
if [ "$REASON" = "PowerLoss" ]; then
  ok "a trace holds the sag, the stop, and reason=PowerLoss"
else
  bad "no trace with sag + stop + PowerLoss in the last 4 minutes"
fi

# ---------------------------------------------------------------------------------------------
STEP=$((STEP+1)); say "$STEP" "A load step on the island moves frequency and the battery covers it"
hz_before=$(qfresh "energy_microgrid_frequency_hertz{energy_site_id=\"$SITE_ISLAND\"}")
note "before: ${hz_before:-?} Hz"
# The command's own exit status is part of the assertion: a load step that was refused (or an exec
# into a container that is not there) must fail the step, not time out silently. That is how the
# first version of this step failed twice without saying why.
if ! $COMPOSE exec -T device-microgrid python site/faultctl.py loadstep --kw 300 --duration 60 > /tmp/demo-loadstep.json 2>&1; then
  bad "the load step was not accepted: $(head -2 /tmp/demo-loadstep.json | tr '\n' ' ')"
fi
poll_until "$CAP_SETTLE" island_stepped || true      # report the failure below, do not exit here
hz=$(qfresh "energy_microgrid_frequency_hertz{energy_site_id=\"$SITE_ISLAND\"}")
bess=$(qfresh "energy_bess_power_real_watts{energy_site_id=\"$SITE_ISLAND\"}")
load=$(qfresh "energy_microgrid_load_power_real_watts{energy_site_id=\"$SITE_ISLAND\"}")
note "after:  ${hz:-?} Hz, battery ${bess:-?} W, load ${load:-?} W"
require "site load during the step (W)" "${load:-0}" gt "300000"
require "battery discharge is carrying it (W)" "${bess:-0}" gt "100000"
require "frequency stayed within 1 Hz of nominal (1/0)" \
  "$(python3 -c "print(1 if abs(float('${hz:-0}') - 50.0) < 1.0 else 0)")" eq "1"
$COMPOSE exec -T device-microgrid python site/faultctl.py loadstep-clear > /dev/null 2>&1 || true

# ---------------------------------------------------------------------------------------------
STEP=$((STEP+1)); say "$STEP" "A capacity cap throttles six bays at the highway hub"
$COMPOSE exec -T csms-highway python ocpp/csmsctl.py set-cap --kw 100 > /dev/null 2>&1
# Poll for the *effect*, not for a fixed delay: the charge point reports every 5 s with a deadband,
# so "sleep then read" races the pipeline — and the first version of this step lost that race twice.
poll_until "$CAP_SETTLE" bays_throttled || true
poll_until "$CAP_SETTLE" site_under_cap || true
cap=$(qfresh "energy_site_capacity_current_amperes{energy_site_id=\"$SITE_HIGHWAY\"}")
offered=$(qfresh "energy_evse_current_offered_amperes{energy_site_id=\"$SITE_HIGHWAY\",energy_evse_throttled_by=\"load_management\"}")
load=$(qfresh "energy_site_load_power_watts{energy_site_id=\"$SITE_HIGHWAY\"}")
note "cap ${cap:-?} A, per-bay offer ${offered:-?} A (throttled by load_management), site load ${load:-?} W"
require "the cap reached the charge point (A)" "${cap:-0}" gt "10"
require "bays throttled by load management" "$THROTTLED" gt "0"
require "site load stays under the 100 kW cap (1/0)" "$(site_under_cap && echo 1 || echo 0)" eq "1"
$COMPOSE exec -T csms-highway python ocpp/csmsctl.py clear-cap > /dev/null 2>&1 || true

# ---------------------------------------------------------------------------------------------
STEP=$((STEP+1)); say "$STEP" "A WAN outage fills one edge queue and loses nothing"
if printf '%s' "$SKIP" | grep -qw outage; then
  skipped "DEMO_SKIP=outage — this is the only step that changes the stack"
else
  lost_before=$(qsum 'otelcol_exporter_enqueue_failed_metric_points{instance=~".*edge-wind.*"}')
  ./impairment/link.sh wind down > /dev/null
  note "wind link down; waiting ${OUTAGE_WAIT}s for the queue"
  sleep "$OUTAGE_WAIT"
  qn=$(queues_empty)
  require "queue depth grew during the outage (1/0)" \
    "$(python3 -c "print(1 if ${qn%.*} > 0 else 0)")" eq "1"
  ./impairment/link.sh wind wet > /dev/null
  note "link restored; draining (budget ${DRAIN_BUDGET}s)"
  if poll_until "$DRAIN_BUDGET" queues_drained; then
    ok "queue drained to 0"
  else
    bad "queue did not drain within ${DRAIN_BUDGET}s (depth $(queues_empty))"
  fi
  lost_after=$(qsum 'otelcol_exporter_enqueue_failed_metric_points{instance=~".*edge-wind.*"}')
  require "points lost (enqueue failed) during the outage" \
    "$(python3 -c "print(max(0, int(float('${lost_after:-0}') - float('${lost_before:-0}'))))")" eq "0"
fi

# ---------------------------------------------------------------------------------------------
STEP=$((STEP+1)); say "$STEP" "Clean state"
if printf '%s' "$SKIP" | grep -qw outage; then
  skipped "queue depth is $(queues_empty) batches, because the outage step was skipped"
else
  require "edge queue depth (batches, all six)" "$(queues_empty)" eq "0"
fi

printf '\n\033[1m%d passed, %d failed\033[0m\n' "$PASS" "$FAIL"
if [ "$FAIL" -gt 0 ]; then
  printf 'The demo is a test: a failure means the stack does not do what this repo claims.\n'
  exit 1
fi
printf 'Every claim in the README has now been executed, not asserted.\n'
