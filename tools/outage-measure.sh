#!/usr/bin/env bash
# Measure store-and-forward through a real outage (issue #3, plan.md D11/D16/D26).
#
#   ./tools/outage-measure.sh wind 150          # wind link down for 150 minutes, then drain
#   OUTAGE_DIR=/tmp/r SAMPLE_S=15 ./tools/outage-measure.sh solar 2   # short rehearsal
#
# What it does, in order, and nothing else:
#   1. records every edge's exporter counters and the hub's receiver counters;
#   2. takes one site's link down;
#   3. samples, once a minute: each edge's queue depth and capacity, the on-disk queue size,
#      and the failure counters that would show a dropped point;
#   4. restores the link and keeps sampling until the queue is empty (or DRAIN_MAX_MIN);
#   5. writes summary.md, samples.csv (long format) and every raw scrape into $OUTAGE_DIR.
#
# Why the hub counter is not the loss test on its own: `otelcol_receiver_accepted_metric_points`
# on the hub is aggregate across all five edges and carries no sender attribute. So the
# end-to-end check is *the sum of every edge's sent delta against the hub's accepted delta*
# over the same window, and the per-site check is that edge's sent delta against its
# enqueue-failure delta. Both are printed, with their arithmetic, so a reader can disagree.
#
# It reads only the collectors' own telemetry (the OTel-native way, same method as
# `make queues`) plus Docker's volume sizes. No new container, no new dependency.
set -uo pipefail

SITE="${1:?usage: outage-measure.sh <rural|solar|wind|metro|microgrid> <minutes-down>}"
DOWN_MIN="${2:?usage: outage-measure.sh <site> <minutes-down>}"
DRAIN_MAX_MIN="${DRAIN_MAX_MIN:-45}"
SAMPLE_S="${SAMPLE_S:-60}"

cd "$(dirname "$0")/.."

COMPOSE="docker compose -f compose/docker-compose.yml"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUTAGE_DIR="${OUTAGE_DIR:-outages/${STAMP}-${SITE}}"
mkdir -p "$OUTAGE_DIR" "$OUTAGE_DIR/raw-metrics"
RAW="$OUTAGE_DIR/raw-metrics"
CSV="$OUTAGE_DIR/samples.csv"
SUMMARY="$OUTAGE_DIR/summary.md"

case "$SITE" in
  rural) PROXY=rural-wan ;;
  solar) PROXY=solar-wan ;;
  wind)  PROXY=wind-wan ;;
  metro) PROXY=metro-wan ;;
  microgrid) PROXY=microgrid-wan ;;
  *) echo "unknown site: $SITE" >&2; exit 1 ;;
esac

# Every edge that is actually running. Detected, not hard-coded, so this works on the
# four-site stack and on the five-site one without editing the script.
SITES=""
for s in rural solar wind metro microgrid highway; do
  if docker ps --format '{{.Names}}' | grep -qx "otel-ot-edge-$s-1"; then SITES="$SITES $s"; fi
done
SITES="${SITES# }"
[ -z "$SITES" ] && { echo "no edge collectors are running - is the stack up?" >&2; exit 1; }

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" | tee -a "$OUTAGE_DIR/run.log"; }

# One exec for every scrape. `docker compose exec` costs about a second on this host and a
# minute-by-minute sample should not spend six of them.
scrape() {
  local cmd="" s
  for s in $SITES; do
    cmd="$cmd; echo '###EDGE-$s'; wget -qO- http://edge-$s:8888/metrics"
  done
  cmd="$cmd; echo '###HUB'; wget -qO- http://hub:8889/metrics"
  $COMPOSE exec -T ops sh -c "${cmd#; }" 2>/dev/null
}

# section <raw> <marker>: the part of one scrape belonging to one endpoint.
section() { printf '%s\n' "$1" | sed -n "/^###$2\$/,/^###/p"; }

# counter <text> <metric-name-prefix>: sum a possibly-labelled counter. Prefix, not exact
# name: contrib 0.130 exports `otelcol_exporter_queue_size__batches_` and
# `otelcol_exporter_sent_metric_points__datapoints__total`, so the unit suffix is part of
# the name on the wire (which is why `make queues` greps by prefix too).
counter() {
  printf '%s\n' "$1" | grep -E "^$2" | grep -v '^#' | awk '{s+=$NF} END{printf "%d", s+0}'
}

# All the queue volumes at once, in KiB. One `docker system df -v` per sample, not five.
disk_kib() { # disk_kib <site> -> KiB, measured with du inside a throwaway container
  # NOT `docker system df -v`: it over-reports a volume by about 2x here, because it counts
  # allocated blocks including filesystem overhead (findings #59). `du` counts the files, which is
  # the number a reader means by "the queue is this big".
  local v="otel-ot_edge-$1-queue"
  docker run --rm -v "$v":/q:ro alpine du -sk /q 2>/dev/null | awk '{printf "%d", $1}'
}

printf 'utc,epoch,phase,site,queue_size,queue_capacity,disk_kib,enqueue_failed,send_failed,sent,accepted,refused\n' > "$CSV"

sample() { # sample <phase>
  local phase="$1" raw df s sec
  raw="$(scrape)"
  printf '%s\n' "$raw" > "$RAW/scrape-$(date -u +%H%M%S)-$phase.txt"
  for s in $SITES; do
    sec="$(section "$raw" "EDGE-$s")"
    printf '%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,,\n' \
      "$(date -u +%FT%TZ)" "$(date +%s)" "$phase" "$s" \
      "$(counter "$sec" otelcol_exporter_queue_size)" \
      "$(counter "$sec" otelcol_exporter_queue_capacity)" \
      "$(disk_kib "$s")" \
      "$(counter "$sec" otelcol_exporter_enqueue_failed_metric_points)" \
      "$(counter "$sec" otelcol_exporter_send_failed_metric_points)" \
      "$(counter "$sec" otelcol_exporter_sent_metric_points)" >> "$CSV"
  done
  sec="$(section "$raw" HUB)"
  printf '%s,%s,%s,%s,,,,,,,%s,%s\n' \
    "$(date -u +%FT%TZ)" "$(date +%s)" "$phase" "__hub__" \
    "$(counter "$sec" otelcol_receiver_accepted_metric_points)" \
    "$(counter "$sec" otelcol_receiver_refused_metric_points)" >> "$CSV"
}

# --- 1. before -------------------------------------------------------------------
sample before
T0=$(date -u +%FT%TZ); T0_EPOCH=$(date +%s)
log "sites: $SITES"
log "outage: site=$SITE proxy=$PROXY for ${DOWN_MIN}m"

# --- 2. the outage ---------------------------------------------------------------
./impairment/link.sh "$SITE" down | while read -r l; do log "$l"; done
log "link down"

while [ "$(date +%s)" -lt $((T0_EPOCH + DOWN_MIN * 60)) ]; do
  sample down
  sleep "$SAMPLE_S"
done

# --- 3. the drain -----------------------------------------------------------------
sample restore
T1=$(date -u +%FT%TZ); T1_EPOCH=$(date +%s)
log "restore: $T1"

./impairment/link.sh "$SITE" wet | while read -r l; do log "$l"; done
log "link restored; draining"

# The first three minutes of a drain are the interesting part, so sample them faster - a
# drain that finished inside one sample interval is a number nobody can act on.
DRAIN_N=0
while [ "$(date +%s)" -lt $((T1_EPOCH + DRAIN_MAX_MIN * 60)) ]; do
  sample drain
  q=$(awk -F, -v s="$SITE" '$4==s {v=$5} END{print v+0}' "$CSV")
  [ "$q" = "0" ] && break
  DRAIN_N=$((DRAIN_N + 1))
  if [ "$DRAIN_N" -le 12 ]; then sleep $((SAMPLE_S / 4 > 0 ? SAMPLE_S / 4 : 5)); else sleep "$SAMPLE_S"; fi
done
sample drained
T2=$(date -u +%FT%TZ); T2_EPOCH=$(date +%s)
log "drained: queue at 0 after $(( (T2_EPOCH - T1_EPOCH) / 60 )) min"

# --- 4. what the store itself kept -----------------------------------------------
# Prometheus is not the pipeline, but it is what a reader will query, and an OTLP receiver
# with an explicit (old) timestamp is a different question from "did the point cross the
# link". Record the answer for one wind/wind-like series rather than asserting it.
# One series of the site under test, so the "what the store kept" line means something whichever
# site is being measured. (The default used to be the wind farm's, which quietly made the line
# meaningless for the other five.)
case "$SITE" in
  wind)      PROM_DEFAULT=energy_windfarm_power_real_watts ;;
  solar)     PROM_DEFAULT=energy_inverter_power_real_watts ;;
  metro|highway|rural) PROM_DEFAULT=energy_meter_power_real_watts ;;
  microgrid) PROM_DEFAULT=energy_microgrid_frequency_hertz ;;
  *)         PROM_DEFAULT=energy_meter_power_real_watts ;;
esac
PROM_QUERY="${PROM_QUERY:-sum(count_over_time(${PROM_METRIC:-$PROM_DEFAULT}[$(( (T2_EPOCH - T0_EPOCH) / 60 + 10 ))m]))}"
prom_samples() {
  local enc
  enc=$(Q="$PROM_QUERY" python3 -c 'import os,urllib.parse; print(urllib.parse.quote(os.environ["Q"]))')
  $COMPOSE exec -T ops wget -qO- "http://prometheus:9090/api/v1/query?query=$enc&time=$(date +%s)" \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); r=d.get("data",{}).get("result",[]); print(r[0]["value"][1] if r else "no data")' 2>/dev/null
}
log "prometheus samples for the window (${PROM_QUERY}): $(prom_samples)"

# --- 5. the summary ---------------------------------------------------------------
# A separate program, on purpose: a run of hours must be re-analysable without re-running it, and
# the counter-reset handling belongs in one place (a collector restart mid-run resets its cumulative
# counters, and a naive `last - first` then reports nonsense or, worse, loss). It writes summary.md
# next to samples.csv, and it takes the site explicitly rather than guessing from the queue depths.
python3 tools/outage-summary.py "$OUTAGE_DIR" "$SITE" > "$SUMMARY"
log "summary: $SUMMARY"
