#!/usr/bin/env bash
# Soak the long-lived Python processes and record the RSS slope (issue #17).
#
#   ./tools/poller-soak.sh 60                 # 60 minutes, sampled every 30 s
#   MINUTES=10 SAMPLE_S=10 ./tools/poller-soak.sh   # rehearsal
#
# Why this exists: `docs/sizing.md` records the poller's memory as a series of surprises — 33
# MiB, then 208, then 232, then 32 — every one of them measured, every one at a different age
# of the process. A number that moves by 7x is not a measurement, it is a warm-up plus a
# question. The question is whether the slope is zero (plateau) or linear (leak).
#
# Two numbers per sample, deliberately, because they disagree by ~20 % and the difference is
# which one you are arguing about (docs/findings.md #35):
#   * `docker stats` — what `make stats` prints, and what D10's budget is written against;
#   * VmRSS from /proc/1/status — includes resident shared library pages.
#
# It restarts the processes it measures first, so "minute 0" means a fresh boot rather than
# whatever the process happened to be doing. That restart is the only thing it touches.
set -uo pipefail
cd "$(dirname "$0")/.."

MINUTES="${MINUTES:-${1:-60}}"
SAMPLE_S="${SAMPLE_S:-30}"
COMPOSE="docker compose -f compose/docker-compose.yml"
OUT="${OUT:-docs/measurements/poller-soak-$(date -u +%Y%m%dT%H%M%SZ).csv}"

# The long-lived Python applications: three pollers (one per device kind, so the register map
# differs), a charge point and its CSMS. Not the collectors — Go, and measured in docs/sizing.md.
TARGETS="${TARGETS:-poller-rural poller-solar poller-microgrid ocpp-cp csms}"

mkdir -p "$(dirname "$OUT")"
log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" | tee -a "${OUT%.csv}.log"; }

log "restarting: $TARGETS"
# shellcheck disable=SC2086
$COMPOSE restart $TARGETS > /dev/null 2>&1
for c in $TARGETS; do
  for _ in $(seq 1 30); do
    docker inspect -f '{{.State.Running}}' "otel-ot-$c-1" 2>/dev/null | grep -q true && break
    sleep 1
  done
done

printf 'utc,epoch_elapsed_s,container,docker_stats_mib,vmrss_mib\n' > "$OUT"
START=$(date +%s)
END=$((START + MINUTES * 60))
log "soaking for ${MINUTES}m, one sample every ${SAMPLE_S}s -> $OUT"

while [ "$(date +%s)" -lt "$END" ]; do
  now=$(date +%s)
  elapsed=$((now - START))
  # One `docker stats` for all of them; --no-stream so it returns.
  stats="$(docker stats --no-stream --format '{{.Name}} {{.MemUsage}}' 2>/dev/null)"
  for c in $TARGETS; do
    name="otel-ot-$c-1"
    dmem="$(printf '%s\n' "$stats" | awk -v n="$name" '$1==n {print $2}' | sed 's/MiB//; s/GiB/*1024/' | bc 2>/dev/null)"
    [ -z "$dmem" ] && dmem="$(printf '%s\n' "$stats" | awk -v n="$name" '$1==n {print $2}' | sed 's/MiB//')"
    rss="$(docker exec "$name" sh -c "awk '/^VmRSS:/ {printf \"%.2f\", \$2/1024}' /proc/1/status" 2>/dev/null)"
    printf '%s,%d,%s,%s,%s\n' "$(date -u +%FT%TZ)" "$elapsed" "$c" "${dmem:-}" "${rss:-}" >> "$OUT"
  done
  sleep "$SAMPLE_S"
done

log "done: $OUT"

python3 - "$OUT" <<'PY'
import csv, sys
from collections import defaultdict

rows = list(csv.DictReader(open(sys.argv[1])))
if not rows:
    print("no samples"); raise SystemExit
by = defaultdict(list)
for r in rows:
    by[r["container"]].append((int(r["epoch_elapsed_s"]), r["docker_stats_mib"], r["vmrss_mib"]))

def f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

print()
print(f"{'container':<18}{'docker stats MiB':>22}{'VmRSS MiB':>24}{'slope MiB/min':>16}")
print(f"{'':<18}{'first -> last':>22}{'first -> last':>24}{'docker / rss':>16}")
for c, samples in by.items():
    ds = [(t, f(d)) for t, d, _ in samples if f(d) is not None]
    rs = [(t, f(m)) for t, _, m in samples if f(m) is not None]
    def line(series):
        if len(series) < 2:
            return None
        (t0, v0), (t1, v1) = series[0], series[-1]
        slope = (v1 - v0) / max(1e-9, (t1 - t0) / 60.0)
        return f"{v0:.1f} -> {v1:.1f}", slope, v1 - v0
    d = line(ds); r = line(rs)
    print(f"{c:<18}{(d[0] if d else '?'):>22}{(r[0] if r else '?'):>24}"
          f"{(f'{d[1]:+.2f} / {r[1]:+.2f}' if d and r else '?'):>16}")
PY
