# Virtual E-Charging Network

A fully virtual electric-vehicle charging network, instrumented end to end with OpenTelemetry: from
utility-scale generation, through the distribution network, to charge points and home wallboxes.

The point is not the electricity. The point is that **OpenTelemetry is useful far beyond application
monitoring** — and that the OpenTelemetry Collector is the natural boundary between operational
technology (OT) and IT.

Status: **M3 in progress — metro depot done** — the collector topology runs (M0), four sites carry
real OT data on one device image (a SunSpec meter, a SunSpec inverter with two MPPT trackers, a
six-turbine wind farm, and a twelve-bay depot), a **charging session is a trace** (M2), and there is
a visual layer fed from the pipeline: a map of Australia with every site's live state, drilling down
to a one-line mimic per site. Decisions and roadmap live in [`plan.md`](plan.md); domain background in [`docs/ot-primer.md`](docs/ot-primer.md);
which standard each device and protocol claims, and how faithfully, in [`docs/standards.md`](docs/standards.md);
measured numbers in [`docs/sizing.md`](docs/sizing.md); hard-won gotchas in [`docs/findings.md`](docs/findings.md); work items are tracked as [GitHub issues](https://github.com/agardnerIT/virtual-e-charging-network/issues?q=is%3Aissue), with the current milestone in [`NEXT.md`](NEXT.md).

![Telemetry architecture](docs/architecture.png)

Source of truth for the figure is [`docs/architecture.drawio`](docs/architecture.drawio); the PNG and SVG
are regenerated from it. Solid borders are running today, dashed are planned.

## Run it

```
make up        # the whole stack, ending in a picture: http://127.0.0.1:8091
make logs      # follow the hub: this is where every site's telemetry lands
make stats     # CPU/RAM of the whole stack (~1.07 GiB, four sites, twelve live bays)
make queues    # are the edge disk queues draining (four edges)
make hmi       # open the map
make session-status            # every bay of every charge point: supply, state, kW, A, tx, kWh
make session-status SITE=metro EVSE=EVSE07    # one bay at the depot
make spine     # voltage sag -> charger derate -> failed session, in one command
make prom Q='count by (__name__) ({__name__=~"energy.*"})'   # PromQL, through the ops box
make clean     # stop and delete everything, including buffered telemetry
```

`make up` ends in a visual, not in log output — the map of Australia, every site's live state, and a
one-line mimic per site. `make up STACK=debug` (collectors only) is on the roadmap.

## The rural site is a real device path (M1a)

```
   [SunSpec/Modbus meter]  <-poll->  [poller]  --OTLP/HTTP-->  [edge collector]  --WAN-->  [hub]
     device-rural                    poller-rural               edge-rural              hub
```

Nothing here is `telemetrygen`. A Modbus TCP device holds a SunSpec-shaped register file — integers
with separate scale-factor registers, a 32-bit energy accumulator across two registers, a device
clock — and the poller reads it and forwards OTLP. The edge collector then does the part that makes
OT telemetry different, and it is the reason an edge collector exists at all:

- **Names and units.** `modbus.register.vpha`, unit `1` becomes `energy.meter.voltage`, unit `V`.
- **Scale.** `modbus.register.raw=2314` with `scale_factor=-1` becomes `231.4 V`. The raw value stays
  on the data point, so the transformation is auditable.
- **Phase naming.** The device says `A/B/C`; IEC practice says `L1/L2/L3`. Translated at the edge.
- **Quality.** Every data point carries `otel.data_point.quality` — `good`, `stale`, `bad` or
  `comms-lost`. Modbus has no such concept, and neither does OTel; this is the repo's strongest
  contribution candidate. Stop the device and watch retained values go `comms-lost`.
- **Two timestamps.** `energy.device.timestamp` is when the device says the sample happened;
  `energy.receive.timestamp` is when the collector observed it. They differ by design — remote sites
  in this network have no reliable NTP.
- **Deadband.** A value that moves less than a threshold is not worth a data point. The two constant
  configuration registers on this device emit exactly one data point each, ever.
- **The report rate is not the scan rate.** The device is scanned at 1 Hz; values are reported
every 5 s. Tying those together was a real bug — it put 1 106 metric points a minute across a link
  whose story is that it is a 4G backhaul, and that is the opposite of what a deadband is for.

### Make a voltage sag

```bash
make fault SITE=rural FAULT=voltage-sag ARGS="--depth 0.78 --duration 20"
make logs      # energy.meter.voltage drops from ~230 V to ~180 V (0.78 pu)
make fault SITE=rural FAULT=clear
```

This is step one of the project's spine: **voltage sag → charger derate → failed charging session**
(plan.md §4). M2 completes it with OCPP sessions as traces.

### Watch time cross a bad link

```bash
make link SITE=rural PROFILE=down      # hard outage on the rural site
make queues                            # the edge queue starts filling; the solar site stays at 0
make link SITE=rural PROFILE=wet       # restore
make queues                            # the queue drains back to zero
```

Profiles: `wet` (clear), `degraded` (250 ms + 2% loss), `satellite` (650 ms + 5% loss + 32 kB/s),
`down` (outage).

## The two generation sites (M1b)

Both generation sites are real Modbus devices on the **same image and same poller** as the rural
meter — they differ only by environment (D10). They exist as a pair because solar and wind are
different *regimes*, and the contrast is measurable rather than decorative:

- **Solar / Mildura — a SunSpec model-103 inverter** with two **MPPT trackers** (model 160), an
  **irradiance and panel-temperature station** (models 302/303) and an **export limit** (model 123).
  It is deliberately oversized against its inverter, **DC/AC = 1.26**, so on a clear day it walks
  **clips** — the flat top of the AC curve is a design choice, not a bug. Cell temperature derates
  it, so a hot clear day produces less than a cool one.
- **Wind / Snowtown — six turbines** expressed as repeating SunSpec-shaped vendor blocks plus a farm
  connection point. Power goes as the cube of wind speed between cut-in and rated, so a single
  machine swings hard while the farm total stays comparatively steady — the opposite shape to solar,
  and it generates at 2am when the solar farm is correctly asleep.

```bash
make prom Q='energy_inverter_power_real_watts{energy_site_id="solar-mildura"}'   # clipped flat top
make prom Q='energy_turbine_power_real_watts{energy_site_id="wind-snowtown"}'    # six disagree
make curtail SITE=solar PCT=60     # operator curtailment, distinct from inverter clipping
make up SOLAR_CLOCK_HOUR=12        # force a daytime solar curve for a demo that runs at night
```

## A charging session is a trace (M2)

```
   [SunSpec/Modbus meter]  <-poll--  [charge point]  --OCPP 1.6J/WS-->  [CSMS test double]
        device-rural                    ocpp-cp                        csms
                                            |
                                            +--OTLP/HTTP-->  [edge-rural] --> hub
```

The charge point is the only process here that is both an OCPP client and a Modbus client, and that
is the point. It reads the site meter's phase voltages over Modbus, so when
`make fault SITE=rural FAULT=voltage-sag` moves a register it **measures** the sag and reacts to it:
offered current falls along a linear derate between 0.94 pu and 0.80 pu, and below 0.80 pu for three
seconds the transaction ends for `PowerLoss`. Nothing in the telemetry is authored causality.

One session is one trace, owned by the charge point:

```
ocpp.session                 root — ocpp.stop.reason=PowerLoss, energy.session.energy_kwh
├── ocpp.authorize
├── ocpp.start_transaction   ocpp.transaction.id allocated here
├── ocpp.meter_values        every 10 s, carrying energy, power, offered current
├── energy.voltage_sag       starts below 0.94 pu; ends on recovery or when the session fails
└── ocpp.stop_transaction    reason=Local or PowerLoss
```

`BootNotification` and the connector `StatusNotification`s are a *separate* short trace: a boot is a
property of the charge point's connection, not of a transaction. OCPP frames are logs — the charge
point's copies carry the session's `trace_id`, and the CSMS's copies are joined by
`ocpp.transaction.id` because OCPP 1.6 has nowhere to put a `traceparent`. That limitation is written
up in `docs/standards.md` rather than papered over.

```bash
make session-status     # supply 229/230/231 V (1.00 pu) | EVSE01 Charging 21.6 kW offered 32.0 A
make spine              # 0.88 pu: 21.6 -> 10.3 kW; 0.78 pu: 0 A, then the session fails
make exemplar          # asks Prometheus for exemplars. It answers [] — see below.
```

**The exemplar leg is a finding, not a feature.** A metric exemplar that jumps from the sag's data
point to its session trace is wired end to end: the charge point stashes the span context on every
observation, and the OTel SDK puts the `trace_id` and `span_id` in the OTLP payload (both verified by
decoding the request body). Prometheus' OTLP receiver stores the metric and **drops the exemplar** —
measured on 3.1.0 and 3.8.1, with the collectors bypassed. The join that works today is
`ocpp.transaction.id`, which is on the span, on every OCPP log line and on `energy.session.energy`.
See plan.md D33.

## The metro depot: twelve bays, twelve concurrent sessions (M3-1)

The fourth site is where the demo stops being a demo. Same images, same poller, same edge collector
config, same charge point code — the difference is scale (12 connectors instead of 2) and
environment (a 12 ms fibre backhaul instead of a 4G link):

```
   [SunSpec/Modbus meter]  <-poll--  [charge point, 12 connectors]  --OCPP 1.6J/WS-->  [CSMS double]
        device-metro              ocpp-cp-metro                                   csms-metro
                                            |
                                            +--OTLP/HTTP-->  [edge-metro] --12 ms WAN-->  hub
```

Every bay runs a session, started 15 s apart, so twelve transactions are live within three minutes
of `make up` and their ends are staggered too. What the depot is *for* is the two things that only
concurrency and cardinality can break:

- **One session is still one trace.** Checked while twelve were running: every trace carries exactly
  one `energy.evse.id`, one `ocpp.transaction.id`, one `energy.session.id` and one connector, every
  `MeterValues` child carries its own transaction, and the CSMS's connector→transaction map agrees
  with Jaeger's 12-for-12. No 600 s session's spans appear in a sibling's trace.
- **Series scale with bays, not with sessions.** 72 EVSE series for 12 bays (6 per bay: power,
  offered current, state, three phase voltages) plus one `energy.session.energy` series per live
  transaction. Measured before (2 bays) and after (12) in [`docs/sizing.md`](docs/sizing.md).

```bash
make session-status SITE=metro          # twelve rows: state, kW, offered A, tx, session kWh
make session-status SITE=metro EVSE=EVSE07
make prom Q='count by (__name__) ({__name__=~"energy_evse.*",energy_site_id="metro-tottenham"})'
make link SITE=metro PROFILE=down       # the depot's own link: 12 ms +- 4 ms when wet
```

The mimic is the same drawing as the rural site's with a different number of bays: the sites
reported in the pipeline decide how many cells there are, and the bays are a table on an LV board
rather than twelve boxes on a busbar. What the depot does **not** claim is written down in
[`docs/standards.md`](docs/standards.md): one CP with twelve connectors (not twelve CPs), no load
sharing between connectors, a CSMS instance per site to keep the OT zones disjoint, and a site meter
whose load is its own model rather than the sum of the bays.

## What the pipeline shows

- **Two layers, and only two** (D16). Edge collectors fan in to one hub — the standard OTel
  agent→gateway pattern.
- **OTel-native** (D15). No ActiveGate, no OneAgent. The collectors *are* the agents.
- **The collector is the OT/IT boundary.** Nothing inside the OT zone is reachable from outside; the
  edge collector is the only thing that talks outward.
- **Store-and-forward is real.** The edge queue is on disk. Kill the link and it fills; restore it and
  it drains.
- **Every signal is already map-ready.** The edge collector stamps `energy.site.id`,
  `energy.site.kind`, `geo.latitude` and `geo.longitude`, so the map in M1b needs no knowledge of the
  sites.

## The map and the mimic

The views are ours (`hmi/`), not a vendored dashboard, and they are fed the same way everything else
is: over OTLP. The hub fans out to Prometheus and Jaeger; the page queries Prometheus through its own
origin and never talks to the store directly.

- **The map** places every site from the attributes the edge collector stamps — `energy.site.id`,
  `energy.site.kind`, `geo.latitude`, `geo.longitude` — so it needs no knowledge of the sites.
- **The mimic** is a one-line diagram, and it is a different one per regime: a load site is grid →
  CB1 → transformer → busbar → meter with live per-phase voltage, current and power in place, with
  every bay on an LV board beneath it (2 cells at the rural site, 12 at the depot — same drawing,
  count read from the pipeline); a solar
  farm is arrays → MPPT trackers → inverter → step-up → grid with irradiance, DC and AC values; a
  wind farm is six turbines on a collector bus → step-up → grid. There is no per-phase real power on
  the inverter (model 103 only carries total W), so the diagram shows the total rather than
  multiplying V by I and calling it a measurement. Equipment with no instrumented point is drawn
  dashed and labelled, never filled with a plausible number. At the rural site the two EVSE boxes
  are live (M2): charge point state, kW, offered current and session energy, rendered with the same
  quality treatment as every other value, so an EVSE that goes `comms-lost` looks it.
- **Quality is shown at the value**, not in a legend, and stale is never drawn as live. Note what is
  deliberately *not* treated as staleness: a value that simply has not changed. The poller deadbands
  — about a third of readings never leave the site — so freshness and trust are different questions.
  Trust comes from `otel.data_point.quality`, which is the whole point of the repo.

Toggle behaviour, `make fault`, and the link profiles all show up here live; the mimic is the surface
the voltage sag is meant to be watched on.

## Layout

```
plan.md                  decisions log + roadmap (source of truth)
NEXT.md                  the current milestone; work items live in GitHub issues
docs/ot-primer.md        electrical/OT primer for people who came from IT
docs/standards.md        which spec each device claims, and how faithfully
docs/sizing.md           measured RAM/disk, the arm64/image audit, and the visual-stack bake-off
sim/sunspec.py           the SunSpec-shaped register maps, shared by every device and the poller
sim/site/                the Modbus TCP devices (meter, PV inverter, wind farm) + control endpoints
sim/poller/              the Modbus -> OTLP poller (one image, any register map)
sim/ocpp/                OCPP 1.6J charge point + CSMS test double (frame codec shared by both)
collector/edge/          one config, parameterised by environment, for every site
collector/hub/           hub pipeline: receive, shed for the local store, fan out
hmi/                     the map and the one-line mimic (static files, no build step)
dashboards/                Prometheus/Jaeger config, and optional Perses provisioning
impairment/              toxiproxy config + link profiles
compose/                 docker-compose.yml
Makefile
```

## Where this is going

Thin end-to-end first, then thick (plan.md §9). M1 made the voltage sag and both generation regimes
real. **M2 is complete**: a charging session is one trace, the charge point measures the sag over
Modbus and fails the transaction for `PowerLoss`, OCPP frames are trace-correlated logs, and the
rural mimic's EVSEs are live. The metric-exemplar leg is emitted and carried but not stored by
Prometheus — a measured finding (D33), not a claim. **M3** adds the hard cases, and the first is
in: a metro depot with twelve bays and twelve concurrent sessions, measured for cardinality. Still
to come: an isolated microgrid, a multi-hour store-and-forward outage measured rather than
projected, and a clock-skew demo where an effect appears before its cause.
