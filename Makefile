# A two-layer OpenTelemetry collector topology for a virtual e-charging network.
#
#   make up        edge collectors + impaired link + hub + 5 real sites + visual stack
#   make ps        what is running
#   make logs      follow the hub (this is where every site's telemetry lands)
#   make stats     live CPU/RAM of the whole stack (feeds docs/sizing.md, D10)
#   make queues    are the edge disk queues draining, and how deep are they
#   make fault     inject the voltage sag:  make fault SITE=rural FAULT=voltage-sag
#   make curtail   hold generation export down:  make curtail SITE=solar PCT=60
#   make step      microgrid only - switch a block of EV load on:  make step KW=300
#   make clock-skew  push a device's own clock out:  make clock-skew SITE=rural SECONDS=240
#   make link      degrade a site's link:   make link SITE=wind PROFILE=down
#   make clean     stop and delete everything, including buffered telemetry
#
#   SOLAR_CLOCK_HOUR=12 make up   pin the solar sim to a daytime hour (see docs/findings.md)
#
# The current milestone and every work item are GitHub issues:
#   https://github.com/agardnerIT/virtual-e-charging-network/milestones
# plan.md holds the decisions (D1-D38 and counting); it is the source of truth.
#
# Five sites today: rural (weak feeder, 2 bays), solar-mildura, wind-snowtown,
# metro-tottenham (12 bays, fibre) and microgrid-kangaroo-island (solar + wind + BESS, no
# feeder, its own grid former - so its signature is frequency, not voltage).

COMPOSE := docker compose -f compose/docker-compose.yml
TOXIPROXY ?= http://localhost:8474

# Host ports for the two UIs. Both defaults avoid ports this machine already holds
# (see docs/findings.md #32 and the note in compose/docker-compose.yml).
HMI_PORT ?= 8091
JAEGER_PORT ?= 16687

# Per-phase cardinality control for the visual store (M4, #11). Empty means "keep", which is
# the default and changes nothing:  make up VISUAL_PHASES=drop
VISUAL_PHASES ?=

# Force the solar simulator to a daytime hour for a demo that has to run at night. Empty by
# default, which means real local time and an honest irradiance curve.
SOLAR_CLOCK_HOUR ?=

.PHONY: up up-debug assert-traffic demo slo registry-check registry-stats sunspec-check down ps logs stats queues link fault fault-status profiles clean queue-detail \
	device-logs poller-logs visual prom hmi curtail step microgrid-status clock-skew clock-reset \
	session session-stop session-status spine exemplar ocpp-logs csms-logs

# STACK=debug is the OT half of the stack with no visual layer: no Prometheus, no Jaeger, no
# HMI (M4, #20). For CI and for debugging the OT side without a browser, and it ends in an
# assertion rather than a log tail.
STACK ?= full

# The services the OT story needs, listed rather than inferred: a new site must be added here
# or it is silently missing from the debug stack.
OT_SERVICES = toxiproxy device-rural poller-rural device-solar poller-solar device-wind \
	poller-wind device-metro poller-metro device-microgrid poller-microgrid \
	edge-rural edge-solar edge-wind edge-metro edge-microgrid csms ocpp-cp hub ops

up:
ifeq ($(STACK),debug)
	$(MAKE) --no-print-directory up-debug
else
	SOLAR_CLOCK_HOUR=$(SOLAR_CLOCK_HOUR) HMI_PORT=$(HMI_PORT) JAEGER_PORT=$(JAEGER_PORT) \
	  VISUAL_PHASES=$(if $(VISUAL_PHASES),$(VISUAL_PHASES),keep) $(COMPOSE) up -d
	@echo
	@echo "waiting for the first telemetry to cross the impaired link..."
	@sleep 6
	@# The depot's link is 12 ms of fibre, not a clear one: apply its baseline the same way
	@# `make link` would, because a toxiproxy config file cannot carry toxics (#41).
	@$(MAKE) --no-print-directory link SITE=metro PROFILE=wet
	@$(MAKE) --no-print-directory queues
	@echo
	@echo "open the map:  http://127.0.0.1:$(HMI_PORT)"
	@echo "             (make up STACK=debug for collectors only, no local stack)"
endif

# The collectors-only stack. The hub's `debug` exporter is the only sink, which is what a CI
# runner and an OT debugging session both want.
up-debug:
	SOLAR_CLOCK_HOUR=$(SOLAR_CLOCK_HOUR) $(COMPOSE) up -d $(OT_SERVICES)
	@$(MAKE) --no-print-directory link SITE=metro PROFILE=wet
	@$(MAKE) --no-print-directory assert-traffic

# A bounded, machine-checkable assertion: metric points AND at least one span must have crossed
# the impaired links and arrived at the hub. Non-zero exit if not — that is the point of the
# target. (The loop pauses via the shell's sleep, which is fine: the budget is fixed.)
ASSERT_TIMEOUT_S ?= 45
assert-traffic:
	@printf 'asserting telemetry reaches the hub within %ss...\n' "$(ASSERT_TIMEOUT_S)"
	@$(COMPOSE) exec -T ops sh -c '\
	  deadline=$$(( $$(date +%s) + $(ASSERT_TIMEOUT_S) )); \
	  while [ $$(date +%s) -lt $$deadline ]; do \
	    m=$$(wget -qO- http://hub:8889/metrics 2>/dev/null | awk "/^otelcol_receiver_accepted_metric_points/ {s+=\$$NF} END {print s+0}"); \
	    s=$$(wget -qO- http://hub:8889/metrics 2>/dev/null | awk "/^otelcol_receiver_accepted_spans/ {s+=\$$NF} END {print s+0}"); \
	    if [ "$$m" -gt 0 ] && [ "$$s" -gt 0 ]; then \
	      printf "ok: hub accepted %s metric points and %s spans\n" "$$m" "$$s"; exit 0; \
	    fi; \
	    sleep 2; \
	  done; \
	  printf "FAIL: nothing at the hub after %ss (points=%s spans=%s)\n" "$(ASSERT_TIMEOUT_S)" "$$m" "$$s" >&2; \
	  exit 1'

down:
	$(COMPOSE) down

ps:
	$(COMPOSE) ps

logs:
	$(COMPOSE) logs -f hub

stats:
	docker stats --no-stream --format \
	  "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}" \
	  $$($(COMPOSE) ps -q)

# Depth of each edge's disk-backed sending queue, read from the collector's own
# telemetry over the compose network. Non-zero during an outage, falling back to zero
# after recovery: the store-and-forward demonstration in one command.
queues:
	@printf '%-14s %9s %9s\n' "edge" "queued" "capacity"
	@for s in rural solar wind metro microgrid; do \
	  raw=$$(docker compose -f compose/docker-compose.yml exec -T ops \
	        wget -qO- http://edge-$$s:8888/metrics 2>/dev/null); \
	  sz=$$(echo "$$raw" | grep '^otelcol_exporter_queue_size' | awk '{s+=$$2} END{print s+0}'); \
	  cap=$$(echo "$$raw" | grep '^otelcol_exporter_queue_capacity' | awk '{s+=$$2} END{print s+0}'); \
	  printf '%-14s %9s %9s\n' "edge-$$s" "$$sz" "$$cap"; \
	done

# make link SITE=rural PROFILE=degraded
SITE ?= rural
PROFILE ?= degraded

# Which site's charge point the session targets talk to. The rural site keeps M2's service
# name; every other site is `ocpp-cp-<site>`. One CP per site is why a bay id alone is not
# enough to find a bay - see `session-status`, which sweeps them all.
CP_SVC = $(if $(filter rural,$(SITE)),ocpp-cp,ocpp-cp-$(SITE))

link:
	@./impairment/link.sh $(SITE) $(PROFILE)

# Inject the one fault this slice supports. Deliberately execs into the device container
# instead of publishing a control port, so the OT zone stays unreachable from the host.
#   make fault SITE=rural FAULT=voltage-sag
#   make fault SITE=rural FAULT=voltage-sag ARGS="--depth 0.6 --duration 30"
#   make fault SITE=rural FAULT=clear
FAULT ?= voltage-sag
ARGS ?=

fault:
	@$(COMPOSE) exec -T device-$(SITE) python site/faultctl.py $(FAULT) $(ARGS)

fault-status:
	@$(COMPOSE) exec -T device-$(SITE) python site/faultctl.py status

# Curtail a generation site's export, the way a network constraint would.
#   make curtail SITE=solar PCT=60
#   make curtail SITE=wind PCT=0          # full curtailment
#   make curtail SITE=solar PCT=100       # clear
PCT ?= 60
curtail:
	@$(COMPOSE) exec -T device-$(SITE) python site/faultctl.py curtail --pct $(PCT)

# ---------------------------------------------------------------------------------
# M3-4: clock skew. A device clock that is minutes out does not restart - it drifts, or
# somebody sets it by hand - so this is a runtime control on the device, not an env var.
#   make clock-skew SITE=rural SECONDS=240     # device claims it is 4 minutes in the future
#   make clock-skew SITE=rural SECONDS=-300    # ... or 5 minutes behind (every point goes
#                                              # quality=stale while the link is perfectly fine)
#   make clock-reset SITE=rural                # back to the site's configured skew
# The demonstration lives in tools/clock-inversion.sh; the write-up is
# docs/findings.md #51-53 and docs/ot-primer.md.
# ---------------------------------------------------------------------------------
# `SECONDS` is what the comment above documents; SKEW is the fallback for anyone who would
# rather not shadow a bash special variable. Both work.
SKEW ?= 240

clock-skew:
	@$(COMPOSE) exec -T device-$(SITE) python site/faultctl.py clock-skew \
	  --seconds $(or $(SECONDS),$(SKEW))

clock-reset:
	@$(COMPOSE) exec -T device-$(SITE) python site/faultctl.py clock-reset

# ---------------------------------------------------------------------------------
# M3-2: the isolated microgrid. Its event is a load step, not a fault: a block of EV chargers
# closing its contactors is an operational event, and the island's grid-forming battery has to
# absorb it. D29 keeps exactly one *fault*, and this is not it.
#   make step KW=300 DURATION=90    # +300 kW for 90 s, then clear
#   make step-clear
#   make microgrid-status           # busbar frequency, BESS power, SoC, hold reason, sources
# ---------------------------------------------------------------------------------
KW ?= 300
DURATION ?=

step:
	@$(COMPOSE) exec -T device-microgrid python site/faultctl.py loadstep --kw $(KW) \
	  $(if $(DURATION),--duration $(DURATION),)

step-clear:
	@$(COMPOSE) exec -T device-microgrid python site/faultctl.py loadstep-clear

microgrid-status:
	@$(COMPOSE) exec -T device-microgrid python site/faultctl.py status

# ---------------------------------------------------------------------------------
# M2: a charging session is a trace. Control of the OCPP charge point is exec-only for
# the same reason `make fault` is: the rural OT zone publishes no ports (plan.md §5.4,
# docs/findings.md #32). The CP reads supply voltage from device-rural over Modbus, so one
# `make fault` produces the whole spine: sag -> derate -> failed session.
# ---------------------------------------------------------------------------------
EVSE ?= EVSE01
# Default exemplar query: the EVSE power data point the sag samples carry an exemplar on.
Q ?= energy_evse_power_real_watts

# Start a session on demand (EVSE01 already runs one by default, so this is mainly for EVSE02,
# or for a demo that wants a fresh transaction).
#   make session                    # EVSE01 at the rural site
#   make session SITE=metro EVSE=EVSE07
session:
	@$(COMPOSE) exec -T $(CP_SVC) python ocpp/cpctl.py start --evse $(EVSE)

session-stop:
	@$(COMPOSE) exec -T $(CP_SVC) python ocpp/cpctl.py stop --evse $(EVSE)

# Every bay of every charge point, one row each - the rural site's two and the depot's twelve.
# `EVSE=EVSE07` narrows it to one bay and skips the site that does not have it (cpctl exits 3,
# which is a normal answer when a caller sweeps sites, not a failure).
session-status:
	@for svc in ocpp-cp ocpp-cp-metro; do \
	  echo "-- $$svc --"; \
	  $(COMPOSE) exec -T $$svc python ocpp/cpctl.py status \
	    $(if $(EVSE),--evse $(EVSE)) || true; \
	done

# The narrative spine in one command: start a session, sag the supply, watch the EVSE
# derate, then sag it past the contactor-drop threshold and watch the transaction fail for
# PowerLoss. Two depths of the SAME fault - no second injection point, no authored
# causality: the charge point is reacting to the meter it can see.
spine:
	@$(COMPOSE) exec -T $(CP_SVC) python ocpp/cpctl.py start --evse $(EVSE)
	@sleep 3
	@echo "-- 0.88 pu: inside the band, charger derates but keeps charging --"
	@$(MAKE) --no-print-directory fault SITE=rural FAULT=voltage-sag \
	  ARGS="--depth 0.88 --duration 25"
	@for i in 1 2 3 4; do \
	  $(COMPOSE) exec -T $(CP_SVC) python ocpp/cpctl.py status; sleep 2; \
	done
	@echo "-- 0.78 pu: below the contactor threshold, session ends for PowerLoss --"
	@$(MAKE) --no-print-directory fault SITE=rural FAULT=voltage-sag \
	  ARGS="--depth 0.78 --duration 15"
	@$(COMPOSE) exec -T $(CP_SVC) python ocpp/cpctl.py watch --evse $(EVSE) --seconds 60

# Did the exemplar survive the collector chain and Prometheus' OTLP receiver? Ask the
# store. A non-empty result is the M2 payoff: a metric data point that jumps to the
# session trace. An empty one is a finding to write down, not a thing to fake.
exemplar:
	@start=$$(python3 -c 'import time; print(int(time.time()) - 1800)'); \
	enc=$$(Q='$(Q)' python3 -c 'import os, urllib.parse; print(urllib.parse.quote(os.environ["Q"]))'); \
	end=$$(python3 -c 'import time; print(int(time.time()) + 1)'); \
	$(COMPOSE) exec -T ops wget -qO- \
	  "http://prometheus:9090/api/v1/query_exemplars?query=$$enc&start=$$start&end=$$end" \
	  | python3 -m json.tool

ocpp-logs:
	$(COMPOSE) logs -f --no-log-prefix $(CP_SVC)

csms-logs:
	$(COMPOSE) logs -f --no-log-prefix $(if $(filter rural,$(SITE)),csms,csms-$(SITE))

device-logs:
	$(COMPOSE) logs -f device-$(SITE)

poller-logs:
	$(COMPOSE) logs -f poller-$(SITE)

# M5: the whole story in one command, ending in a verdict. This is a test, not a transcript:
# it exits non-zero and names the assertion that failed.  DEMO_FAST=1 for CI.
demo:
	@./tools/demo.sh

# M5, #13: the four SLOs, from the store, with targets. WINDOW=6h for the ugly truth.
slo:
	@./tools/slo.sh

# M4, #9: the published semantic conventions. `registry-check` is the gate: weaver validates the
# file, and the coverage script asserts that the registry and the emitters agree in BOTH
# directions — a metric the stack emits and the registry omits fails, and so does a metric the
# registry describes that nothing emits.
WEAVER := docker run --rm -v $(PWD)/schema:/registry otel/weaver:latest

registry-check:
	@echo "== weaver registry check"
	@$(WEAVER) registry check -r /registry
	@echo
	@echo "== registry vs emitters"
	@./tools/registry-coverage.py

registry-stats:
	@$(WEAVER) registry stats -r /registry

# The byte layout every fidelity claim rests on: encode -> decode for every map, both word orders,
# the not-implemented sentinel, and the model chain (#23).
sunspec-check:
	@./tools/sunspec-roundtrip.py

profiles:
	@curl -s $(TOXIPROXY)/proxies | python3 -m json.tool

clean:
	$(COMPOSE) down -v --remove-orphans

# ---------------------------------------------------------------------------------
# The local visual stack (D3). Nothing here reads a simulator: Prometheus is fed over OTLP
# by the hub, and the HMI reads Prometheus. `make up` ends in a picture, not in log output.
# ---------------------------------------------------------------------------------

# Open the map.
hmi:
	@command -v open >/dev/null && open http://127.0.0.1:$(HMI_PORT) \
	  || echo "open http://127.0.0.1:$(HMI_PORT)"

# PromQL from the host, through the ops box - Prometheus publishes no host port (see the
# note in compose/docker-compose.yml). URL-encode on the HOST: the ops box is alpine and has
# no python3, so doing it in there makes this target depend on a package nobody installed.
#   make prom Q='count by (__name__) ({__name__=~"energy.*"})'
prom:
	@enc=$$(Q='$(Q)' python3 -c 'import os,urllib.parse; print(urllib.parse.quote(os.environ["Q"]))'); \
	$(COMPOSE) exec -T ops wget -qO- "http://prometheus:9090/api/v1/query?query=$$enc" | python3 -m json.tool

visual:
	@$(COMPOSE) logs --tail=20 --no-log-prefix prometheus jaeger hmi

# convenience: untruncated queue metric for one edge
queue-detail:
	@docker compose -f compose/docker-compose.yml exec -T ops \
	  wget -qO- http://edge-rural:8888/metrics | grep -E '^otelcol_exporter_(queue|sent|send_failed)' | awk '{n=$$1; sub(/\{.*/,"",n); print n, $$2}'
