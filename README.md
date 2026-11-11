# Virtual E-Charging Network

A fully virtual electric-vehicle charging network, instrumented end to end with OpenTelemetry: from
utility-scale generation, through the distribution network, to charge points and home wallboxes.

The point is not the electricity. The point is that **OpenTelemetry is useful far beyond application
monitoring** — and that the OpenTelemetry Collector is the natural boundary between operational
technology (OT) and IT.

Status: **M1b complete** — the collector topology runs (M0), three sites carry real OT data on one
device image (a SunSpec meter, a SunSpec inverter with two MPPT trackers, and a six-turbine wind
farm), and there is a visual layer fed from the pipeline: a map of Australia with every site's live
state, drilling down to a one-line mimic per site. Decisions and roadmap live in
[`plan.md`](plan.md); domain background in [`docs/ot-primer.md`](docs/ot-primer.md); which standard
each device claims, and how faithfully, in [`docs/standards.md`](docs/standards.md); measured numbers
in [`docs/sizing.md`](docs/sizing.md); the current work item is [`NEXT.md`](NEXT.md).

![Telemetry architecture](docs/architecture.png)

Source of truth for the figure is [`docs/architecture.drawio`](docs/architecture.drawio); the PNG and SVG
are regenerated from it. Solid borders are running today, dashed are planned.

## Run it

```
make up        # the whole stack, ending in a picture: http://127.0.0.1:8091
make logs      # follow the hub: this is where every site's telemetry lands
make stats     # CPU/RAM of the whole stack (~750 MiB, three sites)
make queues    # are the edge disk queues draining
make hmi       # open the map
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
  CB1 → transformer → busbar → meter with live per-phase voltage, current and power in place; a solar
  farm is arrays → MPPT trackers → inverter → step-up → grid with irradiance, DC and AC values; a
  wind farm is six turbines on a collector bus → step-up → grid. There is no per-phase real power on
  the inverter (model 103 only carries total W), so the diagram shows the total rather than
  multiplying V by I and calling it a measurement. Equipment with no instrumented point is drawn
  dashed and labelled, never filled with a plausible number.
- **Quality is shown at the value**, not in a legend, and stale is never drawn as live. Note what is
  deliberately *not* treated as staleness: a value that simply has not changed. The poller deadbands
  — about a third of readings never leave the site — so freshness and trust are different questions.
  Trust comes from `otel.data_point.quality`, which is the whole point of the repo.

Toggle behaviour, `make fault`, and the link profiles all show up here live; the mimic is the surface
the voltage sag is meant to be watched on.

## Layout

```
plan.md                  decisions log + roadmap (source of truth)
NEXT.md                  the current work item
docs/ot-primer.md        electrical/OT primer for people who came from IT
docs/standards.md        which spec each device claims, and how faithfully
docs/sizing.md           measured RAM/disk, the arm64/image audit, and the visual-stack bake-off
sim/sunspec.py           the SunSpec-shaped register maps, shared by every device and the poller
sim/site/                the Modbus TCP devices (meter, PV inverter, wind farm) + control endpoints
sim/poller/              the Modbus -> OTLP poller (one image, any register map)
collector/edge/          one config, parameterised by environment, for every site
collector/hub/           hub pipeline: receive, shed for the local store, fan out
hmi/                     the map and the one-line mimic (static files, no build step)
dashboards/                Prometheus/Jaeger config, and optional Perses provisioning
impairment/              toxiproxy config + link profiles
compose/                 docker-compose.yml
Makefile
```

## Where this is going

Thin end-to-end first, then thick (plan.md §9). M1 is complete: a voltage sag on a weak rural feeder
is real, and so are both generation regimes. **M2** makes the last leg of the narrative real — OCPP
sessions as traces, with a metric exemplar joining the sag to the failed session — at which point a
reader can follow one PQ event from a millisecond-class disturbance to an application error.
