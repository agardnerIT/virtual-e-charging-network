#!/usr/bin/env bash
# Make an effect arrive before its cause, reproducibly (issue #4, plan.md D12/D23).
#
#   ./tools/clock-inversion.sh                 # +240 s on the rural meter, sag, print both orders
#   SKEW=600 ./tools/clock-inversion.sh        # push it further
#   SKEW=-300 ./tools/clock-inversion.sh       # the other direction: everything says "stale"
#
# What it does:
#   1. pushes the rural meter's own clock out by SKEW seconds, at runtime (no restart: a clock
#      that has drifted does not reboot the device);
#   2. runs the sag deep enough to end a session with PowerLoss - the same fault as `make spine`;
#   3. fetches the resulting trace from Jaeger and prints the SAME events in two orderings:
#        by receive time  - span start times, which are the charge point's clock;
#        by device time   - what the supply meter claims, carried on each span as
#                           energy.supply.device.timestamp;
#   4. resets the clock.
#
# Nothing is rewritten and nothing is "corrected". The point is that the pipeline carries both
# facts and the backend decides, and that one of the two decisions is wrong.
set -uo pipefail
cd "$(dirname "$0")/.."

SKEW="${SKEW:-240}"
JAEGER="${JAEGER:-http://127.0.0.1:16687}"
COMPOSE="docker compose -f compose/docker-compose.yml"
SITE="${SITE:-rural}"
EVSE="${EVSE:-EVSE01}"
CP_SVC="ocpp-cp"

log() { printf '%s\n' "$*"; }

# --- 1. the clock ---------------------------------------------------------------
log "==> setting the $SITE meter clock to $(printf '%+d' "$SKEW") s"
$COMPOSE exec -T device-$SITE python site/faultctl.py clock-skew --seconds "$SKEW" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin)["clock"]; print("    device claims %+.0fs (%s)" % (d["clock_skew_s"], d["device_time_note"]))'

# --- 2. the cause, and its effect -----------------------------------------------
log "==> starting a session on $EVSE"
$COMPOSE exec -T $CP_SVC python ocpp/cpctl.py start --evse "$EVSE" > /dev/null
sleep 4
log "==> sagging the supply to 0.78 pu: below the contactor threshold, so the session ends"
$COMPOSE exec -T device-$SITE python site/faultctl.py voltage-sag --depth 0.78 --duration 15 > /dev/null
log "==> waiting for the transaction to end and the trace to arrive"
sleep 22

# --- 3. the two orderings --------------------------------------------------------
TRACE_JSON="$(curl -s --max-time 10 "$JAEGER/api/traces?service=vecn-ocpp-cp&limit=20&lookback=5m")"
# A file, not a pipe: `cmd | python3 - <<'PY'` gives the heredoc the stdin, so the JSON never
# arrives and json.load fails with "Expecting value: line 1 column 1". Ten minutes lost to
# that once already.
TRACE_TMP="$(mktemp -t vecn-trace)"
printf '%s' "$TRACE_JSON" > "$TRACE_TMP"
SKEW="$SKEW" python3 - "$TRACE_TMP" <<'PY'
import json, os, sys, time

skew = float(os.environ.get("SKEW", "240"))
data = json.load(open(sys.argv[1]))
traces = data.get("data") or []
if not traces:
    print("no traces from vecn-ocpp-cp in the last 5 minutes - is the stack up?")
    raise SystemExit(1)

# The newest trace that actually contains the sag: that is the one with both events.
def spans_of(t):
    for batch in t.get("spans", []) if isinstance(t.get("spans"), list) else t.get("spans", []):
        pass
    # Jaeger's JSON is {"traceID":..., "spans":[...], "processes":{...}}
    return t.get("spans", [])

cands = []
for t in traces:
    spans = spans_of(t)
    names = {s.get("operationName") for s in spans}
    if "energy.voltage_sag" in names and "ocpp.stop_transaction" in names:
        start = min(s["startTime"] for s in spans)
        cands.append((start, t, spans))
if not cands:
    print("no trace with both a sag span and a stop span yet - the session may not have failed.")
    print("traces seen:", [sorted({s.get('operationName') for s in spans_of(t)}) for t in traces[:3]])
    raise SystemExit(1)

_, trace, spans = max(cands, key=lambda c: c[0])

def attr(s, key):
    for a in s.get("tags", []):
        if a.get("key") == key:
            return a.get("value")
    return None

# The two events whose order is the whole question. Both are spans in one trace, and each
# carries the clock of the thing that produced it:
#   the sag  - measured by the supply meter, so its *device* claim is the meter's clock
#              (energy.supply.device.timestamp) while its span start is the charge point's;
#   the stop - decided by the charge point, so its span start is the only time there is.
# A backend that sorts metric samples by their device timestamp and traces by their span time
# is therefore sorting two clocks. That is the bug this demonstrates.
def find(op):
    return next((s for s in spans if s.get("operationName") == op), None)

sag = find("energy.voltage_sag")
stop = find("ocpp.stop_transaction")
if sag is None or stop is None:
    print("the trace has no sag/stop pair - was the sag deep enough to end the session?")
    raise SystemExit(1)

def us(v):
    # Jaeger reports startTime in **microseconds** since the epoch; divide once. Dividing
    # twice prints a time nineteen days into the future, which is how this was noticed.
    return float(v) / 1e6

def iso(ts):
    if ts is None:
        return "        -        "
    return time.strftime("%H:%M:%S", time.gmtime(ts)) + f".{int((ts % 1) * 1000):03d}"

def attr(s, key):
    for a in s.get("tags", []):
        if a.get("key") == key:
            return a.get("value")
    return None

sag_recv = us(sag["startTime"])
stop_recv = us(stop["startTime"])
sag_dev = attr(sag, "energy.supply.device.timestamp")
sag_dev = float(sag_dev) if sag_dev is not None else None
skew_measured = attr(sag, "energy.supply.clock_skew_s")

print()
print(f"trace {trace.get('traceID')} - {len(spans)} spans, session {spans and find('ocpp.session') and find('ocpp.session').get('traceID')[:8]}")
print(f"the charge point measured the meter's clock while it read the supply: {skew_measured} s")
print()
print("ORDERED BY RECEIVE TIME - what the pipeline actually saw, all on the charge point's clock")
print("(and, for metrics, the store's sample time is the hub's receive time by construction)")
print(f"  {iso(sag_recv)}  sag begins (the cause)")
print(f"  {iso(stop_recv)}  session ends for PowerLoss (the effect)   +{stop_recv - sag_recv:.1f} s later")
print()
print("ORDERED BY DEVICE TIME - the clock of whatever measured each event, which is what a")
print("backend gets if it sorts metric samples by energy.device.timestamp and traces by span time")
if sag_dev is None:
    print("  the sag span has no meter clock, so the mixed-clock ordering cannot be built")
else:
    print(f"  {iso(stop_recv)}  session ends for PowerLoss (the effect)")
    print(f"  {iso(sag_dev)}  sag begins (the cause)                     {sag_dev - stop_recv:+.1f} s")
    print()
    if sag_dev > stop_recv:
        print(f"  >>> INVERTED. The cause is stamped {(sag_dev - stop_recv):.0f} s AFTER its effect, because")
        print(f"      the supply meter's clock is {skew:+.0f} s out and the device timestamp is trusted.")
        print("      Nothing was rewritten to make this happen; both facts came down the same pipeline.")
    else:
        print(f"  Order still holds: the cause is {(stop_recv - sag_dev):.0f} s before the effect. A")
        print("      clock that is BEHIND stretches the gap instead of reversing it - which is why")
        print("      this direction is the less obvious failure. Try SKEW=-600 and watch the gap,")
        print("      or SKEW=+240 (the default) for the inversion.")
print()
print("What each ordering is actually reading:")
print("  receive = the charge point's clock, twice. Consistent, correct, and seconds after the")
print("            event - the timestamps a backend should order by (plan.md D23).")
print("  device  = the meter's clock for the cause, the charge point's clock for the effect.")
print("            Two clocks in one ordering is the bug: nothing aligns them, and the pipeline")
print("            must not pretend otherwise by 'correcting' either one.")
print()
PY

# --- 4. show the quality consequence, then reset -------------------------------
log ""
log "==> the quality vocabulary's answer (a clock 4 min AHEAD is 'good' on every point):"
$COMPOSE exec -T ops wget -qO- \
  "http://prometheus:9090/api/v1/query?query=count%20by%20(otel_data_point_quality)%20(%7B__name__%3D~%22energy_.*%22%2Cenergy_site_id%3D%22rural-brokenhill%22%7D)" \
  2>/dev/null | python3 -c "
import json,sys
try:
    for r in json.load(sys.stdin)[\"data\"][\"result\"]:
        print(\"    %11s  %s series\" % (r[\"metric\"].get(\"otel_data_point_quality\",\"?\"), r[\"value\"][1]))
except Exception:
    print(\"    (quality breakdown unavailable)\")
"
log ""
log "==> resetting the $SITE meter clock"
$COMPOSE exec -T device-$SITE python site/faultctl.py clock-reset > /dev/null
log "done. A negative SKEW is the other half of the story: every point then reports quality=stale"
log "while the link is perfectly healthy - stale means 'the device clock is old', not 'comms-lost'."
