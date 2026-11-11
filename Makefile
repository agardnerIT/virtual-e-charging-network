# A two-layer OpenTelemetry collector topology for a virtual e-charging network.
#
#   make up        edge collectors + impaired link + hub + 3 real sites + visual stack
#   make ps        what is running
#   make logs      follow the hub (this is where every site's telemetry lands)
#   make stats     live CPU/RAM of the whole stack (feeds docs/sizing.md, D10)
#   make queues    are the edge disk queues draining, and how deep are they
#   make fault     inject the voltage sag:  make fault SITE=rural FAULT=voltage-sag
#   make curtail   hold generation export down:  make curtail SITE=solar PCT=60
#   make link      degrade a site's link:   make link SITE=wind PROFILE=down
#   make clean     stop and delete everything, including buffered telemetry
#
#   SOLAR_CLOCK_HOUR=12 make up   pin the solar sim to a daytime hour (see NEXT.md)
#
# See NEXT.md for the current work item and plan.md for the decisions.

COMPOSE := docker compose -f compose/docker-compose.yml
TOXIPROXY ?= http://localhost:8474

# Host ports for the two UIs. Both defaults avoid ports this machine already holds
# (see NEXT.md 7.4 and the note in compose/docker-compose.yml).
HMI_PORT ?= 8091
JAEGER_PORT ?= 16687

# Force the solar simulator to a daytime hour for a demo that has to run at night. Empty by
# default, which means real local time and an honest irradiance curve.
SOLAR_CLOCK_HOUR ?=

.PHONY: up down ps logs stats queues link fault fault-status profiles clean queue-detail \
	device-logs poller-logs visual prom hmi curtail

up:
	SOLAR_CLOCK_HOUR=$(SOLAR_CLOCK_HOUR) HMI_PORT=$(HMI_PORT) JAEGER_PORT=$(JAEGER_PORT) $(COMPOSE) up -d
	@echo
	@echo "waiting for the first telemetry to cross the impaired link..."
	@sleep 6
	@$(MAKE) --no-print-directory queues
	@echo
	@echo "open the map:  http://127.0.0.1:$(HMI_PORT)"
	@echo "             (make up STACK=debug for collectors only, no local stack)"

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
	@for s in rural solar wind; do \
	  raw=$$(docker compose -f compose/docker-compose.yml exec -T ops \
	        wget -qO- http://edge-$$s:8888/metrics 2>/dev/null); \
	  sz=$$(echo "$$raw" | grep '^otelcol_exporter_queue_size' | awk '{s+=$$2} END{print s+0}'); \
	  cap=$$(echo "$$raw" | grep '^otelcol_exporter_queue_capacity' | awk '{s+=$$2} END{print s+0}'); \
	  printf '%-14s %9s %9s\n' "edge-$$s" "$$sz" "$$cap"; \
	done

# make link SITE=rural PROFILE=degraded
SITE ?= rural
PROFILE ?= degraded

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

device-logs:
	$(COMPOSE) logs -f device-$(SITE)

poller-logs:
	$(COMPOSE) logs -f poller-$(SITE)

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
