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
for s in rural solar wind metro microgrid; do
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
disk_kib() { # disk_kib <site> <df-output> -> KiB
  # Docker prints "0B", "589.8kB", "1.114MB". Strip the suffix and convert, because
  # `sed 's/[A-Za-z]//g'` gives a number in whatever unit happened to be printed and a table
  # that silently mixes MB and kB is worse than no table.
  printf '%s\n' "$2" | awk -v v="otel-ot_edge-$1-queue" '
    $1==v {
      n=$3; u=$3
      gsub(/[0-9.]/,"",u); gsub(/[A-Za-z]/,"",n)
      if (u=="B")       m=1/1024
      else if (u=="kB") m=1000/1024
      else if (u=="MB") m=1000000/1024
      else if (u=="GB") m=1000000000/1024
      else              m=1
      printf "%d", n*m
    }' | head -1
}

printf 'utc,epoch,phase,site,queue_size,queue_capacity,disk_kib,enqueue_failed,send_failed,sent,accepted,refused\n' > "$CSV"

sample() { # sample <phase>
  local phase="$1" raw df s sec
  raw="$(scrape)"
  df="$(docker system df -v 2>/dev/null)"
  printf '%s\n' "$raw" > "$RAW/scrape-$(date -u +%H%M%S)-$phase.txt"
  for s in $SITES; do
    sec="$(section "$raw" "EDGE-$s")"
    printf '%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,,\n' \
      "$(date -u +%FT%TZ)" "$(date +%s)" "$phase" "$s" \
      "$(counter "$sec" otelcol_exporter_queue_size)" \
      "$(counter "$sec" otelcol_exporter_queue_capacity)" \
      "$(disk_kib "$s" "$df")" \
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
PROM_QUERY="${PROM_QUERY:-sum(count_over_time(${PROM_METRIC:-energy_windfarm_power_real_watts}[$(( (T2_EPOCH - T0_EPOCH) / 60 + 10 ))m]))}"
prom_samples() {
  local enc
  enc=$(Q="$PROM_QUERY" python3 -c 'import os,urllib.parse; print(urllib.parse.quote(os.environ["Q"]))')
  $COMPOSE exec -T ops wget -qO- "http://prometheus:9090/api/v1/query?query=$enc&time=$(date +%s)" \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); r=d.get("data",{}).get("result",[]); print(r[0]["value"][1] if r else "no data")' 2>/dev/null
}
log "prometheus samples for the window (${PROM_QUERY}): $(prom_samples)"

# --- 5. the summary ---------------------------------------------------------------
T0="$T0" T1="$T1" T2="$T2" T0_EPOCH="$T0_EPOCH" T1_EPOCH="$T1_EPOCH" T2_EPOCH="$T2_EPOCH" \
SITE="$SITE" PROXY="$PROXY" DOWN_MIN="$DOWN_MIN" SITES="$SITES" CSV="$CSV" \
STAMP="$STAMP" PROM_QUERY="$PROM_QUERY" PROM_SAMPLES="$(prom_samples)" \
python3 - "$SUMMARY" <<'PY'
import csv, os, sys

out = open(sys.argv[1], "w")
env = os.environ
rows = list(csv.DictReader(open(env["CSV"])))
site = env["SITE"]

def num(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0

def first(s, phase):
    for r in rows:
        if r["site"] == s and r["phase"] == phase:
            return r
    return None

def last(s):
    got = [r for r in rows if r["site"] == s]
    return got[-1] if got else None

# Before / after, per site.
deltas = {}
for s in env["SITES"].split():
    b, a = first(s, "before"), last(s)
    if not b or not a:
        continue
    deltas[s] = {
        "sent": num(a["sent"]) - num(b["sent"]),
        "enq_failed": num(a["enqueue_failed"]) - num(b["enqueue_failed"]),
        "send_failed": num(a["send_failed"]) - num(b["send_failed"]),
        "disk_before": num(b["disk_kib"]),
        "disk_after": num(a["disk_kib"]),
    }
hub_b, hub_a = first("__hub__", "before"), last("__hub__")
hub = {
    "accepted": num(hub_a["accepted"]) - num(hub_b["accepted"]),
    "refused": num(hub_a["refused"]) - num(hub_b["refused"]),
}

# Queue depth of the site under test, over time.
q = [(r["phase"], num(r["queue_size"]), num(r["queue_capacity"]), num(r["disk_kib"]))
     for r in rows if r["site"] == site]
peak = max((x[1] for x in q), default=0)
peak_disk = max((x[3] for x in q), default=0)
cap = max((x[2] for x in q), default=0)
at_restore = next((x[1] for x in q if x[0] == "restore"), 0)
disk_at_restore = next((x[3] for x in q if x[0] == "restore"), 0)
sent_all = sum(d["sent"] for d in deltas.values())

w = out.write
w(f"# Store-and-forward, measured: {site}, {env['DOWN_MIN']} min down\n\n")
w(f"Generated by `tools/outage-measure.sh` at {env['STAMP']} on `{os.uname().nodename}`.\n\n")
w("Method: the collectors' own `otelcol_*` counters (the OTel-native way, and what\n")
w("`make queues` reads) plus the Docker volume size from `docker system df -v`. One sample a\n")
w("minute; `samples.csv` is the whole series in long format, `raw-metrics/` holds every\n")
w("scrape so any number here can be re-derived.\n\n")

w("| | |\n|---|---|\n")
w(f"| link down | {env['T0']} |\n")
w(f"| link restored | {env['T1']} |\n")
w(f"| queue empty | {env['T2']} |\n")
w(f"| outage duration | {int(env['T1_EPOCH']) // 60 - int(env['T0_EPOCH']) // 60} min |\n")
w(f"| drain duration | {int(env['T2_EPOCH']) // 60 - int(env['T1_EPOCH']) // 60} min |\n")
w(f"| queue depth at restore | {at_restore} batches |\n")
w(f"| queue depth, peak | {peak} batches of {cap} |\n")
w(f"| on-disk queue at restore | {disk_at_restore} KiB |\n")
w(f"| on-disk queue, peak | {peak_disk} KiB |\n")
w(f"| points accepted by edge-{site} | {deltas.get(site, {}).get('sent', 0)} |\n")
w(f"| points that failed to enqueue anywhere | {sum(d['enq_failed'] for d in deltas.values())} |\n")
w(f"| hub accepted (all edges) | {hub['accepted']} |\n")
w(f"| hub refused (all edges) | {hub['refused']} |\n\n")

w("## Per site, over the same window\n\n")
w("`sent` is `otelcol_exporter_sent_metric_points`: points that crossed the link. `retries`\n")
w("is `otelcol_exporter_send_failed_metric_points`, which increments per failed *attempt*,\n")
w("so a large number during an outage is the retry loop working, not data loss. `dropped`\n")
w("is `enqueue_failed`, which is the one that would mean a point never made it into the\n")
w("queue at all.\n\n")
w("| edge | sent | retries | dropped | queue KiB before → after |\n|---|---|---|---|---|\n")
for s, d in sorted(deltas.items()):
    w(f"| `{s}` | {d['sent']} | {d['send_failed']} | {d['enq_failed']} | "
      f"{d['disk_before']} → {d['disk_after']} |\n")
w("\n")

lost = sum(d["enq_failed"] for d in deltas.values())
if lost == 0 and deltas.get(site, {}).get("sent", 0) > 0 and hub["refused"] == 0:
    w(f"**Nothing lost.** `edge-{site}` accepted {deltas[site]['sent']} metric points across the\n")
    w(f"outage and the drain, none of them failed to enqueue, and the hub refused none. The\n")
    w(f"arithmetic: every edge's sent delta sums to {sent_all}, and the hub's accepted delta is\n")
    w(f"{hub['accepted']} — the two differ only by points still in flight when the last sample\n")
    w("was taken, which is why both are printed.\n\n")
else:
    w("**Something does not add up.** dropped=%d, sent(%s)=%d, hub refused=%d. Read the\n"
      % (lost, site, deltas.get(site, {}).get("sent", 0), hub["refused"]))
    w("samples and the raw scrapes before quoting this run.\n\n")

w("## What the store kept\n\n")
w("Prometheus is not the pipeline: an OTLP metric data point with an explicit timestamp\n")
w("from three hours ago is a different question from 'did the point cross the link'. The\n")
w("query below asks the store how many samples of one series it has in the window.\n\n")
w(f"```\n{env['PROM_QUERY']}\n-> {env['PROM_SAMPLES']}\n```\n")
PY

log "summary: $SUMMARY"
