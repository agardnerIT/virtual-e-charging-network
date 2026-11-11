#!/usr/bin/env bash
# Degrade or restore a site's WAN link. See plan.md §5.4.
#
#   ./impairment/link.sh rural wet         # clear: no impairment
#   ./impairment/link.sh rural degraded    # 250 ms + 2% loss
#   ./impairment/link.sh rural satellite   # 650 ms + 5% loss + 32 kB/s
#   ./impairment/link.sh rural down        # hard outage
#   ./impairment/link.sh metro wet         # the depot's own good link: 12 ms +- 4 ms
#
# Every invocation starts from a clean link: existing toxics are removed and the proxy is
# re-enabled before the new profile is applied. Without that, profiles stack up and a
# "restore" silently leaves the link broken.
set -euo pipefail

API="${TOXIPROXY:-http://localhost:8474}"
SITE="${1:?usage: link.sh <rural|solar|wind|metro> <wet|degraded|satellite|down>}"
PROFILE="${2:?usage: link.sh <rural|solar|wind|metro> <wet|degraded|satellite|down>}"

case "$SITE" in
  rural) PROXY=rural-wan ;;
  solar) PROXY=solar-wan ;;
  wind)  PROXY=wind-wan ;;
  metro) PROXY=metro-wan ;;
  *) echo "unknown site: $SITE (want rural|solar|wind|metro)" >&2; exit 1 ;;
esac

toxic() { # toxic <name> <type> <json-attributes>
  curl -s -X POST "$API/proxies/$PROXY/toxics" \
    -d "{\"name\":\"$1\",\"type\":\"$2\",\"attributes\":$3}" >/dev/null
}

# The config file declares the proxies and nothing else: toxiproxy 2.11 accepts a `toxics` key in
# its config and silently ignores it (docs/findings.md #41). So a site's good-link baseline is
# applied here, over the API - and `make up` calls this for the metro site.
for _ in $(seq 1 20); do
  curl -s --max-time 2 "$API/proxies" >/dev/null && break
  sleep 0.5
done

# --- reset to a clean link ------------------------------------------------------
for t in $(curl -s "$API/proxies/$PROXY/toxics" \
             | python3 -c 'import json,sys; print(" ".join(x["name"] for x in json.load(sys.stdin)))'); do
  curl -s -X DELETE "$API/proxies/$PROXY/toxics/$t" >/dev/null
done
curl -s -X POST "$API/proxies/$PROXY" -d '{"enabled":true}' >/dev/null

# --- apply the requested profile ------------------------------------------------
#
# `wet` is the site's own good link, not "no toxics". The metro depot's backhaul is 12 ms of
# fibre with jitter, and it is in toxiproxy.json so the very first `make up` already has it;
# clearing it here as well keeps `make link SITE=metro PROFILE=down && ... PROFILE=wet` a
# round trip to the same baseline instead of a quiet change of environment.
case "$PROFILE" in
  wet)
    if [ "$SITE" = metro ]; then
      toxic fibre-latency latency '{"latency":12,"jitter":4}'
      echo "$PROXY: clear (12 ms +- 4 ms, no loss)"
    else
      echo "$PROXY: clear (no impairment)"
    fi
    ;;
  degraded)
    echo "$PROXY: 250 ms + 2% loss"
    toxic latency latency '{"latency":250}'
    toxic loss loss '{"probability":0.02}'
    ;;
  satellite)
    echo "$PROXY: 650 ms + 5% loss + 32 kB/s"
    toxic latency latency '{"latency":650}'
    toxic loss loss '{"probability":0.05}'
    toxic bw bandwidth '{"rate":32}'
    ;;
  down)
    echo "$PROXY: outage"
    curl -s -X POST "$API/proxies/$PROXY" -d '{"enabled":false}' >/dev/null
    ;;
  *)
    echo "unknown profile: $PROFILE (want wet|degraded|satellite|down)" >&2
    exit 1
    ;;
esac
