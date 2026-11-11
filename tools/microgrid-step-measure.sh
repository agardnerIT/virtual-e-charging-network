#!/usr/bin/env bash
# Record what a load step does to the islanded microgrid, from the store (issue #2).
#
#   ./tools/microgrid-step-measure.sh                 # 300 kW step for 60 s, sampled every 2 s
#   KW=380 DURATION=90 ./tools/microgrid-step-measure.sh
#
# Every number here comes from Prometheus (`make prom`'s path, through the ops box), not from
# the device. The one exception is the step command itself, which goes into the device's
# control endpoint the same way `make step` does.
#
# Two things this does that a naive query does not:
#
#   * It picks the *freshest sample per metric*. A poller restart mints a new
#     `service.instance.id`, which is a new Prometheus series (docs/findings.md #12), so an
#     instant query returns several series for one logical signal and taking the first one
#     silently gives you a value from a process that died ten minutes ago. Caught the hard way.
#   * It records the columns a domain reader needs in one row: frequency, the battery's power
#     and SoC, the load step, and the hold reason. A frequency number without the battery's
#     state does not say whether the grid former did its job.
set -uo pipefail
cd "$(dirname "$0")/.."

KW="${KW:-300}"
DURATION="${DURATION:-60}"
SAMPLE_S="${SAMPLE_S:-2}"
SAMPLES="${SAMPLES:-40}"
SITE_ID="${SITE_ID:-microgrid-kangaroo-island}"
COMPOSE="docker compose -f compose/docker-compose.yml"
OUT="${OUT:-docs/measurements/microgrid-step-$(date -u +%Y%m%dT%H%M%SZ).csv}"
mkdir -p "$(dirname "$OUT")"

# freshest <promql>: the value of the series with the newest sample. Prints "none" on no data.
freshest() {
  local enc
  enc=$(Q="$1" python3 -c 'import os,urllib.parse; print(urllib.parse.quote(os.environ["Q"]))')
  $COMPOSE exec -T ops wget -qO- "http://prometheus:9090/api/v1/query?query=$enc" 2>/dev/null \
    | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)["data"]["result"]
except Exception:
    print("none"); raise SystemExit
if not data:
    print("none"); raise SystemExit
newest = max(data, key=lambda r: float(r["value"][0]))
print(newest["value"][1])
'
}

sel() { echo "$1{energy_site_id=\"$SITE_ID\"}"; }

echo "utc,hz,bess_kw,soc_pct,load_kw,ev_kw,source_kw,hold_reason" > "$OUT"
for i in $(seq 1 "$SAMPLES"); do
  if [ "$i" = "3" ]; then
    $COMPOSE exec -T device-microgrid python site/faultctl.py loadstep \
      --kw "$KW" --duration "$DURATION" > /dev/null 2>&1
    echo "load step +${KW} kW for ${DURATION}s at $(date -u +%FT%TZ)"
  fi
  hz=$(freshest "$(sel energy_microgrid_frequency_hertz)")
  bess=$(freshest "$(sel energy_bess_power_real_watts)")
  soc=$(freshest "$(sel energy_bess_state_of_charge_percent)")
  load=$(freshest "$(sel energy_microgrid_load_power_real_watts)")
  ev=$(freshest "$(sel energy_microgrid_load_power_ev_watts)")
  hold=$(freshest "$(sel energy_bess_hold_reason_ratio)")
  src=$(freshest "sum(energy_microgrid_source_power_real_watts{energy_site_id=\"$SITE_ID\"})")
  # Prometheus returns float64 strings; round here so the CSV is readable and a diff is
  # meaningful (49.910000000000004 is not a better measurement than 49.910).
  r3() { printf '%.3f' "${1:-0}" 2>/dev/null || echo "$1"; }
  r0() { printf '%.0f' "${1:-0}" 2>/dev/null || echo "$1"; }
  printf '%s,%s,%s,%s,%s,%s,%s,%s\n' "$(date -u +%FT%TZ)" "$(r3 "$hz")" "$(r0 "$bess")" \
    "$(r3 "$soc")" "$(r0 "$load")" "$(r0 "$ev")" "$(r0 "$src")" "$(r0 "$hold")" >> "$OUT"
  sleep "$SAMPLE_S"
done

python3 - "$OUT" <<'PY'
import csv, sys
rows = list(csv.DictReader(open(sys.argv[1])))
def f(r, k):
    try:
        return float(r[k])
    except (TypeError, ValueError):
        return float("nan")
hz = [f(r, "hz") for r in rows if r["hz"] not in ("", "none")]
load = [f(r, "load_kw") for r in rows if r["load_kw"] not in ("", "none")]
bess = [f(r, "bess_kw") for r in rows if r["bess_kw"] not in ("", "none")]
soc = [f(r, "soc_pct") for r in rows if r["soc_pct"] not in ("", "none")]
hold = [r["hold_reason"] for r in rows]
print()
print(f"rows: {len(rows)}   file: {sys.argv[1]}")
if hz:
    print(f"frequency   min {min(hz):.3f} Hz   max {max(hz):.3f} Hz   "
          f"span {max(hz) - min(hz):.3f} Hz")
if load:
    print(f"site load   min {min(load)/1000:.0f} kW   max {max(load)/1000:.0f} kW")
if bess:
    print(f"battery     min {min(bess)/1000:.0f} kW   max {max(bess)/1000:.0f} kW")
if soc:
    print(f"SoC         {min(soc):.1f}% -> {max(soc):.1f}%")
print(f"hold reason values seen: {sorted(set(hold))}  (0 none, 1 soc-high, 2 soc-low, 3 power-limit)")
PY
