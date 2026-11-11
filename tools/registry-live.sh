#!/usr/bin/env bash
# #27 — validate the *running stack's* telemetry against the registry, not just the emitters.
#
#   make registry-live              # 60 s on the live stack, exits non-zero on a violation
#   make registry-live DURATION=180  # longer window, more cold metrics
#   make registry-live WRONG=1      # the deliberate-wrong-metric test from #27's definition of done
#
# `make registry-check` asks "does the registry agree with the code that emits?" — it reads the edge
# config and the application source. This asks the stronger question: "does it agree with what the
# stack is actually sending?" Only this one catches a rename or a unit change made *inside* the
# pipeline, and only this one sees the attributes that arrive on a data point at runtime.
#
# The tap is an overlay, not a default: a second `--config` file for the hub
# (collector/hub/registry-tap.yaml) that adds one exporter and one pipeline. `make up` does not read
# it, and the trap below puts the hub back on its normal config on every exit path.
set -uo pipefail
cd "$(dirname "$0")/.."

BASE="docker compose -f compose/docker-compose.yml"
TAP="$BASE -f compose/registry-live.yml"
ADMIN=http://127.0.0.1:14320
DURATION="${DURATION:-60}"
WRONG="${WRONG:-0}"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
SUFFIX=""; [[ "$WRONG" == "1" ]] && SUFFIX="-wrong"
# The full report is 9 MiB of JSON: it belongs in scratch, like every other long run's raw output
# (`outages/`, gitignored). What is committed is the summary a reader can check the claim against.
mkdir -p outages
FULL="outages/registry-live-${STAMP}${SUFFIX}.json"
SUMMARY="docs/measurements/registry-live-${STAMP}${SUFFIX}.json"

say() { printf '%s\n' "$*"; }
die() { printf '%s\n' "$*" >&2; exit 2; }

restore() {
  # Order matters: the hub first, so a failed restore is loud while weaver is still there to blame.
  $BASE up -d --no-deps hub >/dev/null 2>&1
  docker rm -sf vecn-weaver >/dev/null 2>&1
}
trap restore EXIT

# Refuse to check a stack that is not running: this target measures the live thing, and a silent
# "0 findings" against an empty stream is the worst possible result.
docker ps --format '{{.Names}}' | grep -qx otel-ot-hub-1 \
  || die "the hub is not running — 'make up' first. This check taps a live stack, it does not build one."

say "== weaver live-check, ${DURATION}s on the live stack"
# Unconditional: a leftover weaver from an interrupted run keeps serving the *old* report on the
# admin port, and a tap that silently reads yesterday's findings is worse than no tap. This cost one
# debugging round to learn.
docker rm -sf vecn-weaver >/dev/null 2>&1
WRONG="$WRONG" $TAP up -d --wait weaver >/dev/null 2>&1
docker ps --format '{{.Names}}' | grep -qx vecn-weaver \
  || { docker logs vecn-weaver 2>&1 | tail -20 >&2; die "weaver did not start (see above)"; }

# Recreate the hub with the tap. The edge queues absorb the restart: nothing is lost, which is the
# same property `make outage-check` demonstrates the hard way.
WRONG="$WRONG" $TAP up -d --no-deps hub >/dev/null 2>&1

say "   collecting"
for i in $(seq "$DURATION"); do
  if (( i % 15 == 0 )); then printf '   %ss\n' "$i"; fi
  sleep 1
done

# POST /stop blocks until the report is built; with --output http the report then stays served.
# Every wait here is bounded: an unbounded curl or `docker wait` turns a broken tap into a hung
# terminal, and the first version of this script did exactly that.
CURL=(curl -sf --max-time 120)
"${CURL[@]}" -X POST "$ADMIN/stop" >/dev/null 2>&1 || die "weaver never received anything — is the hub exporting?"
"${CURL[@]}" "$ADMIN/report" -o "$FULL" || die "no report from weaver"
"${CURL[@]}" -X POST "$ADMIN/shutdown" >/dev/null 2>&1
for _ in $(seq 60); do
  [[ "$(docker inspect -f '{{.State.Running}}' vecn-weaver 2>/dev/null)" == "false" ]] && break
  sleep 1
done
WEAVER_EXIT=$(docker inspect -f '{{.State.ExitCode}}' vecn-weaver 2>/dev/null || echo 99)

say "== findings"
python3 - "$FULL" "$SUMMARY" "$DURATION" "$WRONG" "$STAMP" <<'PY'
import collections, json, sys

path, summary_path, duration, wrong, stamp = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] == "1", sys.argv[5]
report = json.load(open(path))
stats = report["statistics"]
levels = stats["advice_level_counts"]
by_id = stats["advice_type_counts"]

print(f"   {stats['total_entities']:>7} entities   "
      f"{stats['total_advisories']:>7} advisories   "
      f"violation={levels.get('violation', 0)} "
      f"improvement={levels.get('improvement', 0)} "
      f"information={levels.get('information', 0)}")
for advice_id, count in sorted(by_id.items(), key=lambda kv: -kv[1]):
    print(f"   {count:>7}  {advice_id}")

# Name what is left holding a violation, so a failure explains itself without opening the JSON.
# weaver's message is generic for a few advice types ("Metric does not exist in the registry"), so
# the sample it arrived on is the useful half — `energy.meter.humidity`, not just "a metric".
found = collections.Counter()
def find_violations(node, where):
    if isinstance(node, dict):
        for advice in (node.get("live_check_result") or {}).get("all_advice") or []:
            if advice["level"] == "violation":
                found[(where, advice["id"], advice["message"])] += 1
        for key, value in node.items():
            find_violations(value, where)
    elif isinstance(node, list):
        for value in node:
            find_violations(value, where)
for sample in report["samples"]:
    where = (sample.get("metric") or {}).get("name") or "resource"
    find_violations(sample, where)

if found:
    print("\n   violations:")
    for (where, advice_id, message), count in found.most_common(15):
        print(f"   {count:>7}  {advice_id}: {where} — {message[:96]}")
else:
    print("\n   no violations")

summary = {
    "utc": stamp,
    "window_seconds": int(duration),
    "deliberately_wrong_metric": wrong,
    "entities": stats["total_entities"],
    "advisories": stats["total_advisories"],
    "levels": levels,
    "by_id": by_id,
    "violations": [{"sample": w, "id": i, "message": m, "count": c}
                   for (w, i, m), c in found.most_common(50)],
}
json.dump(summary, open(summary_path, "w"), indent=1)
print(f"\n   summary: {summary_path}")
print(f"   full report (scratch): {path}")
if wrong:
    print("   WRONG=1: one emitted metric arrived under an undeclared name (energy.meter.humidity),")
    print("   so this run MUST fail — that is the proof the gate is watching.")
PY

say ""
if (( WEAVER_EXIT == 0 )); then
  if [[ "$WRONG" == "1" ]]; then
    die "FAIL: WRONG=1 was set and weaver still exited 0 — the check is not watching"
  fi
  say "PASS: the running stack agrees with the registry ($WEAVER_EXIT from weaver, fail-on=violation)"
  exit 0
fi
die "FAIL: weaver exited $WEAVER_EXIT (fail-on=violation). Summary: $SUMMARY, full report: $FULL"
