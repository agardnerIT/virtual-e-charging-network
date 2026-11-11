# Virtual E-Charging Network

A fully virtual electric-vehicle charging network — utility-scale generation, a distribution feeder,
charge points with cars on them — instrumented end to end with **OpenTelemetry only**. There is no
vendor agent anywhere in it: the collectors *are* the agents.

The thesis is that **OpenTelemetry is useful far beyond application monitoring**. Device telemetry has
quality codes, unaligned clocks and links that disappear for hours. The Collector is a natural,
testable boundary between operational technology (OT) and IT. This repo runs those cases rather than
asserting them.

![Telemetry architecture](docs/architecture.png)

Source of truth for the figure is [`docs/architecture.drawio`](docs/architecture.drawio); solid is running
today, dashed is planned.

## The idea

Devices speak Modbus or OCPP, not OTLP. A small poller per site reads the device on its own terms and
forwards OTLP to an **edge collector**; the edge collector adds meaning (names, units, quality,
timestamps), buffers to disk, and is the site's only egress. Edge collectors cross an impaired WAN to
one **hub**, which fans out to Prometheus, Jaeger and a map.

```
[meter | inverter | turbines | charge points] -> poller -> edge collector -> WAN -> hub -> stores
```

Two collector layers, and only two (plan.md D16): edge per site, one hub. Anything else leaks every
site's quirks into one config and destroys store-and-forward.

## What the stack emits, before you run it

Five sites, one metric vocabulary, and one place to change the backend. The short version:

| Signal | What you get | Where it lands in this repo | Detail |
|---|---|---|---|
| **Metrics** | 310 `energy_*` series measured today (the count moves with live sessions): meter, EVSE/session, inverter/PV, turbine/wind-farm. Every point carries `otel.data_point.quality` (`good` / `stale` / `bad` / `comms-lost`) and splits by `energy_site_name`, `energy_site_id`, `energy_site_kind` (`charging_site` / `solar_farm` / `wind_farm`), plus `energy_phase`, `energy_direction`, `energy_evse_id`, `energy_mppt`, `energy_turbine` | Prometheus 3, fed by OTLP from the hub. No host port: `make prom Q='...'` | [§3](docs/telemetry.md#3-metrics), [§3.6 charts](docs/telemetry.md#36-charts-to-build-in-the-first-ten-minutes) |
| **Traces** | One trace per charging session: `ocpp.session` → `authorize` / `start_transaction` / `meter_values` / `energy.voltage_sag` / `stop_transaction`; `ocpp.boot` is a second, separate trace | Jaeger v2, fed by OTLP: <http://127.0.0.1:16687> | [§4](docs/telemetry.md#4-traces) |
| **Logs** | Every OCPP frame with `ocpp.action`, `ocpp.message.direction`, `ocpp.charge_point.id`, `ocpp.transaction.id`, `ocpp.frame`; the charge point's copies carry the session's `trace_id` | The hub's `debug` exporter — `make logs`, `make ocpp-logs`, `make csms-logs`. **No log store by design** | [§5](docs/telemetry.md#5-logs) |
| **The collectors' own telemetry** | `otelcol_*`: queue depth/capacity, refused and failed exports — the store-and-forward signal | Edge `:8888`, hub `:8889`, `make queues` | [§6](docs/telemetry.md#6-the-collectors-own-telemetry) |

**Configs:** one per layer, and you only ever edit the hub to change a backend.
[`collector/edge/site.yaml`](collector/edge/site.yaml) holds device semantics (names, units, quality,
phase mapping, receive time, disk queue) and is identical for all five sites;
[`collector/hub/hub.yaml`](collector/hub/hub.yaml) holds the fan-out. Backend endpoints appear in one
place — the two `VISUAL_*_ENDPOINT` variables in [`compose/docker-compose.yml`](compose/docker-compose.yml).
Point those at your own OTLP endpoint, or add an exporter to the hub's `metrics` pipeline for the
untouched backend-of-record copy. [§7](docs/telemetry.md#7-where-the-configs-are-and-how-to-point-this-at-your-backend)
has both, and the cardinality rule that decides which pipeline you want.

Want to break it on purpose? `make link SITE=wind PROFILE=down`, `satellite`, `degraded` —
[§8](docs/telemetry.md#8-toxiproxy-the-problem-patterns-and-what-they-do-to-the-telemetry) says what
each one does to the telemetry, measured, including the two traps (a disabled proxy does not kill an
established stream; a drained queue is written back under its original timestamps).

New to the domain entirely? [`docs/telemetry.md`](docs/telemetry.md) opens with the five sites in
plain words, and [`docs/ot-primer.md`](docs/ot-primer.md) explains the electrical side.

## If you came from observability, look at these five things

| Pattern | How it appears here | Why your normal stack cannot express it |
|---|---|---|
| **A business event as a trace** | One charging session is one trace: `ocpp.session` is the root, `authorize` / `start` / `meter_values` / `stop` are children | APM models a request that ends in milliseconds. A ten-minute transaction whose stop reason is a physical measurement has no request to hang off. |
| **Quality as a data point attribute** | `otel.data_point.quality` = `good` / `stale` / `bad` / `comms-lost`, authored by the edge collector (D21) | Neither Modbus nor OTLP has a status field for a value that is present but not trustworthy; Prometheus cannot say "stale, but not yet gone". |
| **Two timestamps per sample** | `energy.device.timestamp` (the device's clock) and `energy.receive.timestamp` (when the collector saw it) | OT sites run with no usable NTP, so the two disagree by seconds to minutes. OTLP metrics carry one timestamp; the receive time rides as a data point attribute (D12, D23). |
| **A WAN outage that loses nothing** | The edge queue is on disk. It fills during an outage and drains on restore — measured, not projected | An SDK retry queue drops what it cannot hold in memory. Multi-hour retention is a collector feature, not an app one. |
| **The Collector as the OT/IT boundary** | Nothing in the OT zone publishes a host port; one egress process per site; control is `docker compose exec` | Agent-per-host topologies assume the agent can reach out and be reached. Here the gap is the point, and the thing inside it has to speak the device protocol. |

Every pattern above is exercised by a command below; each has a decision in [`plan.md`](plan.md).

## See it in 60 seconds

```bash
make up                          # the whole stack; ends at http://127.0.0.1:8091
make queues                      # five edges, all zero: the links are clean
make spine                       # sag -> derate -> failed session, in one command
make session-status SITE=metro   # twelve bays: supply voltage, state, kW, offered A, tx
```

`make spine` is the whole story in one go: it starts a session, drops supply voltage to 0.88 pu
(per-unit — 0.88 × 230 V), watches the charger **derate** (reduce its offered current) to 10.3 kW, then
drops to 0.78 pu until the transaction ends for `PowerLoss`. Nothing is scripted on the trace side: the
charge point reads the meter over Modbus and reacts to what it can see. It may be night where you are —
`make up SOLAR_CLOCK_HOUR=12` pins the solar farm to daytime.

## What is running

Four sites, one image, one poller, one edge config; they differ by environment and scale (plan.md D10,
D37).

| Site | Regime | `energy.site.kind` | Devices | Link | Bays |
|---|---|---|---|---|---|
| `rural` | load, weak feeder | `charging_site` | SunSpec meter (the solar/meter register-map standard) + a 2-bay charge point | 4G, impaired | 2 |
| `solar-mildura` | generation | `solar_farm` | SunSpec 103 inverter, 2 MPPT (maximum-power-point) trackers, irradiance/temperature | impaired | — |
| `wind-snowtown` | generation | `wind_farm` | 6 turbines + a farm connection point | impaired | — |
| `metro-tottenham` | load, depot | `charging_site` | SunSpec meter + a 12-bay charge point | 12 ms fibre | 12 |
| `microgrid-kangaroo-island` | island | `microgrid` | Solar + one turbine + a grid-forming battery; no feeder, so frequency is set on site | impaired | — |

The kind splits *regime*, not size: the rural site and the depot are both `charging_site`. What each
site is, in plain words, and which metric families exist at which site: [`docs/telemetry.md`](docs/telemetry.md#1-the-sites-spelled-out).

Five independent impaired links, each with profiles `wet`, `degraded`, `satellite` and `down`. The visual
layer is Prometheus + Jaeger + our own map and one-line mimic, fed only from the pipeline. Measured
footprint of all 21 containers: ~1.07 GiB ([`docs/sizing.md`](docs/sizing.md)).

## One session is one trace

The charge point is the only process here that is both an OCPP client and a Modbus client, and that is
the point: it reads the site meter's phase voltages, so a sag is something it **measures**. Offered
current falls linearly between 0.94 pu and 0.80 pu; below 0.80 pu for three seconds the transaction
ends for `PowerLoss`. One session is one trace, owned by the charge point:

```
ocpp.session                 root — ocpp.stop.reason=PowerLoss, energy.session.energy_kwh
├── ocpp.authorize
├── ocpp.start_transaction   ocpp.transaction.id allocated here
├── ocpp.meter_values        every 10 s, carrying energy, power, offered current
├── energy.voltage_sag       starts below 0.94 pu; ends on recovery or when the session fails
└── ocpp.stop_transaction    reason=Local or PowerLoss
```

`BootNotification` and the connector `StatusNotification`s are a *separate* short trace: a boot is a
property of the connection, not of a transaction. OCPP frames are logs — the charge point's copies
carry the session's `trace_id`, and the CSMS (charge-point management system, the central server)
cannot, so its copies are joined by `ocpp.transaction.id`. OCPP 1.6 has nowhere to put a
`traceparent`; that limitation is written up in [`docs/standards.md`](docs/standards.md) §5 rather than
papered over.

The twelve-bay depot keeps this true under concurrency: twelve live sessions, twelve traces, checked
against the CSMS's own connector map ([`docs/sites/metro.md`](docs/sites/metro.md)).

## Is it technically honest?

**Real:** Modbus TCP function 03 against SunSpec-shaped register maps (models 203, 103, 160, 302/303,
123 and vendor extensions — subsets, labelled as subsets), and OCPP 1.6J messages over a real WebSocket
with the `ocpp1.6` subprotocol. **Modelled:** the physics — irradiance, cell temperature, a cube-law
wind curve, the charge point's linear derate. **Not done:** IEC 61850, DNP3, OPC UA, real protection,
real settlement, a power-flow model, OCPP conformance of any kind. Per-device fidelity and the full
"deliberately not claimed" list are in [`docs/standards.md`](docs/standards.md), with the source of
every number it quotes.

**Measured footprint:** 1 094 MiB (1.07 GiB) for 21 containers against a 3 GB budget, and six series
per bay — 87 `energy_evse.*` series across the two charging sites, 74 of them from the depot's twelve bays —
by `docker stats` and PromQL ([`docs/sizing.md`](docs/sizing.md)).

**One measured limitation that simplification must not lose:** the metric-exemplar leg from the sag's
data point to its session trace is wired end to end, and Prometheus' OTLP receiver drops the exemplar.
The metric arrives; `/api/v1/query_exemplars` returns `[]`, on 3.1.0 and 3.8.1 with the collectors
bypassed. The SDK and the OTLP body carry it correctly. `make exemplar` shows the empty answer; the
join that works today is `ocpp.transaction.id`. Recorded as a finding, not faked (plan.md D33).

## More to run

| Command | What it does |
|---|---|
| `make logs` | follow the hub — this is where every site's telemetry lands |
| `make stats` | CPU/RAM of the whole stack (feeds `docs/sizing.md`) |
| `make prom Q='count by (__name__) ({__name__=~"energy.*"})'` | PromQL through the `ops` box; Prometheus has no host port |
| `make fault SITE=rural FAULT=voltage-sag ARGS="--depth 0.6 --duration 30"` | inject the one fault this repo models; `FAULT=clear` clears it |
| `make curtail SITE=solar PCT=60` | operator curtailment, distinct from inverter clipping |
| `make link SITE=wind PROFILE=down` | hard outage on one link; then watch `make queues` |
| `make session SITE=metro EVSE=EVSE07` | start a transaction on one bay |
| `make exemplar` | ask Prometheus for exemplars; it answers `[]` |
| `make hmi` | open the map |
| `make clean` | stop and delete everything, including buffered telemetry |

The rest, including `make spine`, `make ps` and `make queue-detail`, is in the [`Makefile`](Makefile).

## Docs map

| | |
|---|---|
| [`docs/telemetry.md`](docs/telemetry.md) | **start here if you just cloned it**: the five sites, every metric and its split keys, traces, logs, and how to point it at your backend |
| [`plan.md`](plan.md) | decisions D1–D37, the source of truth, and the roadmap; ids above point here |
| [`NEXT.md`](NEXT.md) | the current milestone; work items are tracked as GitHub issues |
| [`docs/sites/rural.md`](docs/sites/rural.md) | the load site: scaling, phase naming, deadband, the sag, link profiles |
| [`docs/sites/generation.md`](docs/sites/generation.md) | solar clipping, the wind cube law, curtailment |
| [`docs/sites/metro.md`](docs/sites/metro.md) | twelve bays: concurrent sessions and cardinality |
| [`docs/sites/microgrid.md`](docs/sites/microgrid.md) | the island: why frequency moves when voltage does not |
| [`docs/hmi.md`](docs/hmi.md) | the map and the one-line mimic, and how quality is drawn |
| [`docs/standards.md`](docs/standards.md) | which spec each device and protocol claims, and how faithfully |
| [`docs/sizing.md`](docs/sizing.md) | measured RAM, disk, cardinality, the arm64 image audit |
| [`docs/findings.md`](docs/findings.md) | the things that cost an hour, by area |
| [`docs/ot-primer.md`](docs/ot-primer.md) | electrical/OT primer for people who came from IT |
| [`docs/m2-build-log.md`](docs/m2-build-log.md) | how M2 was actually built, including the bugs |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | hard constraints, conventions, the file tree, the checklist before "done" |

## Status and next

M0, M1a, M1b, M2 and the metro depot (M3-1) are complete and verified: `make up` ends in a picture at
<http://127.0.0.1:8091>, `make queues` shows four clean edges, and `make spine` ends in a `PowerLoss`
transaction. **M3** adds the hard cases — an isolated solar + wind + battery microgrid (done: five sites), a
multi-hour store-and-forward outage measured, and clock skew that makes an effect precede its cause. Track it in the
[M3 milestone](https://github.com/agardnerIT/virtual-e-charging-network/milestone/1).
