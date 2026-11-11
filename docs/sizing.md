# Sizing — measured, not estimated

Budget (D10): the default stack must fit comfortably in ≤3 GB on an Apple M2 / 8 GB machine,
arm64 only. Every number below was measured by running the stack. Re-run `make stats` after any
change that affects the footprint and update this file.

**Host:** Apple M2, 8 GB, arm64 (Docker Desktop, `arm64` images only).
**Stack:** M3 — 4 edge collectors (rural, solar, wind, metro), 1 hub, the impaired link,
the rural SunSpec/Modbus meter + poller, the solar SunSpec/Modbus inverter + poller, the
wind farm's SunSpec-shaped 6-turbine device + poller, the depot's meter + poller (all four
devices and all four pollers are the *same image*), two OCPP 1.6J charge points (2 bays rural,
12 bays depot) with a CSMS test double per site, plus the local visual stack (Prometheus,
Jaeger, our own HMI). `telemetrygen` is gone from the default path.

## Two numbers, and why both are here

`docker stats` (what `make stats` prints, and the number D10 gates on) and a process's real
resident set (`/proc/1/status: VmRSS`) are **not the same measurement**. `VmRSS` includes resident
pages of shared libraries; `docker stats` does not count them.

They moved in opposite directions in the same change, which is why this distinction is written
down rather than assumed: moving the poller from OTLP/gRPC to OTLP/HTTP took `VmRSS` from 54 MiB
to 43 MiB while `docker stats` barely moved (35.2 → 31.9 MiB), because most of what was removed
was mapping of `libgrpc`/`libssl`.

The M1a version of this file quoted the poller at 33 MiB. That figure was not wrong — it was a
*fresh-boot* number presented as a steady state. The poller's steady state at the time was
208 MiB. See "Poller memory" below.

## Twelve bays: the cardinality a depot was built to expose (M3-1)

`plan.md` §12 and D25 warn that a per-session attribute on a metric mints a series per session.
The depot is the site that makes that warning real: two EVSE series sets became twelve, and every
bay is billing-grade cardinality that Prometheus flattens into series identity.

Measured the same way both times — PromQL against the OTLP-fed store, with the stack settled, every
edge queue at 0 and every bay charging (`make prom Q='count({__name__=~"energy_evse.*"})'`). "Before"
is the three-site M2 stack with the rural site's two bays; "after" is the four-site M3 stack with
the depot's twelve also live, measured on a store that had just come up from `make clean && make up`.
**Measurement: PromQL counts of series in Prometheus**, not `docker stats` and not `VmRSS` — this is
a store-cardinality number, not a memory number.

| | before (3 sites, 2 bays) | after (4 sites, 12 bays) |
|---|---|---|
| `energy_evse.*` series, all sites | 13 | 97 |
| — of which the depot | — | 84 |
| `energy_session.energy` series | 2 | 13 |
| all `energy_*` series carrying a site id | 173 | 297 |
| `prometheus_tsdb_head_series` | 897 | 895 |

The last row is in the table because it is the number everyone reaches for, and it is the one that
**cannot be read as a delta**: it counts every series in the store including Prometheus' own,
`target_info`, and every replica left behind by a container that has since restarted. Both readings
were taken about ten minutes into a running stack, but the "before" store had been up for half an
hour and the "after" one was fresh, so the two cancel out into noise. The honest deltas are the
first three rows; the EVSE series count does not depend on how old the store is.

The arithmetic, because it is the useful part: **six series per bay** (power, offered current,
state, and one voltage per phase) and **one series per live transaction** for
`energy.session.energy`. Twelve bays therefore cost 72 + 12 = 84 series, and that is what a bay
costs, no matter how many sessions run through it — the idTag, the vehicle id, the session id and
the transaction id ride on spans and logs, not on the gauges (D36). Only `energy.session.energy`
carries `ocpp.transaction.id`, by design and by exception, and it is why 12 bays produce 12 new
series every 10-minute session cycle (they age out 5 minutes after the transaction stops, so the
steady state is ~24 for the depot, not 12 × forever).

Two details worth knowing before you read the numbers back:

- `energy_evse_state_ratio` reads 27 for 14 bays, because `energy.evse.state.text` is a bounded
  attribute (Available / Preparing / Charging / Finishing): a bay that has just changed state holds
  both series for the staleness window, so 12 bays × 2 states + the rural site's 3 = 27. Bounded is
  the point — an unbounded text attribute would have behaved like the `energy.receive.timestamp`
  disaster above. Read this number on a stack that has just settled and it will be lower.

## Runtime footprint

Measured on `make up`, 2026-10-15, with all three generation/load sites real and no
`telemetrygen` anywhere:

| Component | Purpose | `docker stats` |
|---|---|---|
| `device-rural` | SunSpec/Modbus TCP meter simulator | 21.2 MiB |
| `poller-rural` | Modbus → OTLP/HTTP poller (Python) | 36.2 MiB |
| `device-solar` | SunSpec/Modbus inverter (models 103/160/302/303/123) | 21.4 MiB |
| `poller-solar` | same poller, `DEVICE_KIND=inverter` | 33.4 MiB |
| `device-wind` | SunSpec-shaped 6-turbine farm device (187 registers) | 21.3 MiB |
| `poller-wind` | same poller, `DEVICE_KIND=windfarm` | 31.6 MiB |
| `edge-rural` | edge collector, rural site (OT zone) | 44.3 MiB |
| `edge-solar` | edge collector, solar farm (OT zone) | 48.9 MiB |
| `edge-wind` | edge collector, wind farm (OT zone) | 45.5 MiB |
| `hub` | hub collector (NOC), fanning out to two exporters | 59.1 MiB |
| `toxiproxy` | the impaired WAN, three per-site proxies | 8.0 MiB |
| `ops` | helper for `make queues` / `make prom` | <1 MiB |
| `prometheus` | metrics store, OTLP/HTTP receiver | 41.2 MiB |
| `jaeger` | trace store (v2 — itself an OTel collector distribution) | 12.5 MiB |
| `hmi` | nginx serving the map + mimic | 3.9 MiB |
| **Total** | | **≈749 MiB (0.73 GiB)** |

### M3 delta: the fourth site (2026-11-11, `docker stats`)

A fourth site is a collector, a meter and its poller, a charge point and a CSMS double. Measured on
the full four-site stack with 12 concurrent sessions, `make stats`:

| Component | Purpose | `docker stats` |
|---|---|---|
| `edge-metro` | edge collector, depot (OT zone) | 57.0 MiB |
| `device-metro` | depot sink meter | 21.6 MiB |
| `poller-metro` | same poller, `DEVICE_KIND=meter` | 38.3 MiB |
| `ocpp-cp-metro` | one charge point, 12 connectors, 12 live sessions | 34.4 MiB |
| `csms-metro` | CSMS test double for the depot | 35.0 MiB |
| **Total, whole stack** (21 containers) | | **1 094 MiB (1.07 GiB)** |

Three things in that table are worth saying out loud. The depot's **charge point costs the same as
the rural one** (34 vs 37 MiB) while driving six times the sessions: twelve asyncio session loops
and 72 gauge data points every 5 s are nothing next to the Python interpreter. The **second CSMS**
is the price of keeping the two OT zones disjoint (a shared CSMS would have to be on both). And
`edge-rural` at 217 MiB is the *disk-backed queue volume's* page cache after hours of use, not a
leak — it is unchanged from M2 and it is the one number in this table that is not steady state.

So the whole four-site stack is **1 094 MiB**: 230 MiB more than the three-site number above, of
which ~57 MiB is the fourth site's collector and ~6 MiB per OCPP pair. Still comfortably inside
D10's 3 GB, with 1.9 GB left for the real backend recipes.

### M2 delta, measured 2026-10-15 with `docker stats`

The OCPP pair adds two containers and, more importantly, two new *kinds* of telemetry: a trace
per session (15 spans for a 600 s session), a log line per OCPP frame (~12 per minute idle,
more during a session), and five EVSE/session gauges. Measured on the full stack with a live
session and a completed failed session:

| Component | Purpose | `docker stats` |
|---|---|---|
| `csms` | OCPP 1.6J central system test double (WebSocket + in-memory transactions) | 27.5 MiB |
| `ocpp-cp` | OCPP charge point; WebSocket client **and** Modbus client (supply voltage) | 36.6 MiB |
| **M2 total** | | **≈832 MiB (0.81 GiB)** |

That is +83 MiB for the pair, inside D10 with ~2.17 GB to spare. `edge-rural` grew from 44 MiB
to ~217 MiB in the same run — not the OCPP pair: it is the disk-backed queue draining a backlog
built up while the hub was down during the build, and it settles back once drained. Measure it
again on a quiet stack before quoting it.

The charge point is the larger of the two because it holds both an OTLP/HTTP exporter stack and
a `pymodbus` client; the CSMS holds a websockets server and the transaction table.

### The exemplar probe, and what it cost

Testing whether Prometheus' OTLP receiver stores exemplars (D33) needed a second Prometheus, run
standalone on the `noc` network and then removed. It is **not** part of the stack and adds
nothing to the numbers above. The finding: metric stored, exemplar not returned — on 3.1.0 and
on 3.8.1. See D33 for the three-step evidence.

Budget headroom: ~2.25 GB. Adding the third site (one device, one poller, one edge collector)
cost ~420 MiB on top of M1b part 1's 327 MiB, of which the three new containers are ~150 MiB
and the rest is the extra telemetry in the already-running stores and the hub. Still an order
of magnitude inside D10.

The three devices are within 0.2 MiB of each other because they are the same Python image
holding a register array; the three pollers are within 5 MiB because they are the same code
with a different register map. That is D10's "one image, sites differ by configuration"
showing up as a number.

## The visual stack: chosen on measured RAM, not preference (D24)

`make up` ends in a picture, not in log output (plan.md §7), so a local visual stack is part of
the default. Three candidates were run against real hub-fed data and measured. Both backends are
fed **over OTLP by the hub and by nothing else** (D15, D18); the HMI reads the store.

| Candidate | Visual layer | Whole stack |
|---|---|---|
| Perses + Prometheus + Jaeger | 162 MiB | ≈477 MiB |
| Grafana OSS + Prometheus + Jaeger | 276 MiB | ≈597 MiB |
| **Prometheus + Jaeger + our own `hmi/`** | **80 MiB** | **≈327 MiB** |

**RAM did not decide it** — every candidate fits D10 with room to spare, which was itself the
useful result. It was decided on the work: the map and the one-line mimic are the deliverable
(plan.md §7, D18), and neither Perses nor Grafana draws an SVG one-line diagram with live values
*in place*. Grafana would have cost 43 MiB more for a panel that still needed custom code.
Perses is kept as an optional profile (`make up STACK=perses`) because its dashboards-as-YAML is
genuinely the better "as code" story for a trends view, and nothing depends on it.

Consequence for the budget: the whole 3 GB is still available for the real backend recipes.

## Cardinality: the measured version of plan.md §12

Three numbers, all from one rural meter on a 1 Hz scan:

| | before | after |
|---|---|---|
| `energy_*` series in Prometheus, after 15 minutes | 4 532 | 28 |
| Prometheus RSS | ~80 MiB | 46.7 MiB |
| Prometheus head series (all) | 5 306 | 765 |

The cause was OTLP data point attributes that change on every poll — `energy.receive.timestamp`,
`energy.device.timestamp`, `modbus.register.raw`. Prometheus flattens attributes into series
identity, so each poll minted new series: ~86 000 per point per day, from one meter. They are
shed on the visual path only (`transform/visual` in `collector/hub/hub.yaml`); the
backend-of-record pipeline still carries them.

Two findings from the OTLP → Prometheus hop that any integrator will hit:

- **Resource attributes do not land on series.** The receiver puts them on a synthetic
  `target_info` series and puts *datapoint* attributes on the series themselves. So
  `count by (__name__) ({energy_site_id=~".+"})` returned `target_info` and nothing else — the
  edge collector's `energy.site.id` was never on a metric, and the M1a task specification's "the map needs
  nothing else" was false for Prometheus. The five bounded site attributes are now copied onto
  the data points at that boundary, so the map needs no `on(job) group_left` join.
- **Metric names are mangled with their units.** `energy.meter.voltage` + UCUM unit `V` becomes
  `energy_meter_voltage_volts`; `kW.h` becomes `kW_h`. The HMI's `POINTS` table is the single
  place that mapping lives.

Head series is not perfectly flat: every poller restart mints a new `service.instance.id`, so a
new `instance` label and a fresh series set, with the old one lingering for the 5-minute
staleness window. It settles back. Not a leak, but it is why a single reading of
`prometheus_tsdb_head_series` should not be compared against a reading taken just after a restart.

## Wire volumes

| Path | Measured |
|---|---|
| poller → edge collector | ~195 metric points/min (was 1 106) |
| edge → hub | batch per 2 s per site (`send_batch_size: 2048`, `timeout: 2s`) |
| edge queue capacity | 5 000 batches per site; depth is in *batches*, not items (docs/findings.md #7) |
| 30 s rural outage | ~15 batches accumulated, drained to 0 on restore |

The poller's report rate is 195 points/min for 28 real points because the scan is 1 Hz, reporting
is every 5 s, and the deadband suppresses roughly a third of readings (34 838 suppressed of
110 850 read, measured). The deadband is why the number is not 336/min.

## Poller memory: 33 → 232 → 32 MiB

Symptom: `poller-rural` measured 33 MiB in the M1a write-up, then 88, 113, 158, 208 and finally
232 MiB (`VmRSS`) across a single session, with `VmHWM == VmRSS` — it never gave memory back —
and 6.09% CPU to poll 28 registers once a second.

Method: separate heap from cache (`/sys/fs/cgroup/memory.stat` showed `anon` 204 MiB, `file`
0.0 MiB, so this was real heap, not page cache), then bisect the runtime by importing layers,
then count what actually crossed the wire.

| Layer | Resident |
|---|---|
| bare Python 3.12 | 10 MiB |
| + `grpc` | 31 MiB |
| + OTel SDK | 34 MiB |
| + OTel gRPC exporter, pymodbus | 44 MiB |

So 44 MiB was the floor and ~190 MiB had accumulated at runtime. Two causes, both bugs:

1. **The report interval was the scan interval.** `_build_meter` passed `POLL_INTERVAL_MS` as the
   OTLP export interval, so the poller opened a gRPC export once per second: 1 106 points/min
   across the wire where ~28 are real. It also inverted the deadband's purpose — a steady
   register is not worth a message, but a batch was sent every second regardless. Now
   `OTEL_METRIC_EXPORT_INTERVAL_MS` = 5 000 and independent of the scan.
2. **A per-sample attribute.** `modbus.register.raw` changes on every poll. It is exactly
   recoverable from the value and the retained scale factor (`value = raw × 10^scale_factor`), so
   the audit trail survives without carrying a per-sample copy of every reading.

Result: **35.2 → 31.9 MiB** (`docker stats`) / **54 → 43 MiB** (`VmRSS`), CPU **6.09% → 0.17%**,
wire **1 106 → ~195 points/min**, and the unchecked climb that took the process to 232 MiB has not
recurred.

**What is not established.** The old build took ~2.5 hours to reach 232 MiB; the fixed build has only
been observed for tens of minutes at a stretch. Sampled on the HTTP build: **43 MiB at 0.6 min, 46 at
5, 48 at 6, 50 at 22** — a *decelerating* curve (≈0.6 MiB/min early, ≈0.1 MiB/min late), which is
what allocator warm-up looks like and not what the old build did. But 22 minutes is not 2.5 hours, and
short windows are dominated by CPython arena steps — two consecutive windows on the same process
measured 2.0 and 0.5 MiB/min, so a one-minute window is evidence of nothing. What *was* checked is
that it is not a connection leak: file descriptors held at 8, threads at 3, and 4 TCP rows, all
constant, sampled four times over two minutes.

**Soak result (2026-10-15).** The 60-minute check was run, and a 9-hour reading happens to exist
as well. The pre-rebuild process, sampled at 9 h uptime, sat at **67 MiB `VmRSS`** with
`VmHWM == VmRSS` — bounded, an order of magnitude below the old 232 MiB climb, but not flat. The
reworked poller held **44–49 MiB** across a 34-minute window (`VmHWM` tracking `VmRSS`, file
descriptors flat at 8). Read together: allocator warm-up then a plateau, not a leak. Raw samples
are in `docs/soak-poller-*.log` (one per minute). The method, so it does not need rediscovering:

```bash
docker exec otel-ot-poller-rural-1 sh -c 'grep VmRSS /proc/1/status'   # sample every few minutes
docker exec otel-ot-poller-rural-1 sh -c 'ls /proc/1/fd | wc -l'        # flat = no leak, just heap
```

A hypothesis that was tested and discarded, recorded so nobody repeats it: unbounded accumulation
of attribute-keyed aggregation series inside the SDK. It was disproved by the wire rate — an
accumulating SDK would have been exporting hundreds of thousands of points per interval after
hours of uptime, and it was exporting ~18.

### Transport (D26)

The site-local hop (poller → edge collector) moved from OTLP/gRPC to **OTLP/HTTP**. gRPC's
advantages (HTTP/2 multiplexing, streaming, binary framing, throughput) buy nothing for ~200
points a minute between two containers on the same bridge network, and the reliability story
lives in the edge collector's disk-backed queue, which is transport-independent. It also removes
`grpcio` — a native library — from the site image entirely.

The saving is **8 MiB on `VmRSS` (54 → 43), not the ~20 MiB predicted**: the HTTP exporter pulls
in `requests`/urllib3/certifi, which were not accounted for. Floor comparison, same probe shape:

| | gRPC | HTTP |
|---|---|---|
| + OTel exporter + SDK + pymodbus | 44 MiB | 36 MiB |

It also drops the thread count from **9 to 3** — grpcio brings a thread pool with it — which is a
better reason to prefer it on a site box than the 8 MiB is.

The edge → hub hop stays on gRPC. It crosses the impaired link, where HTTP is arguably the more
robust choice (middlebox traversal, no idle connection to reap), but the reliability there comes
from `retry_on_failure` + `file_storage`, not the transport, and the impairment profiles and queue
accounting are already validated against gRPC. Recorded as a deliberate open choice rather than
churned; the repo could later *demonstrate* the difference over one toxiproxy profile.

## Disk

Edge `file_storage` queue capacity is 5 000 batches per site. A 30 s rural outage accumulates
~15 batches. Prometheus retains 6 h (`--storage.tsdb.retention.time=6h`) — a proof-of-life
retention (D3), not a historian. Long-outage byte growth and multi-hour retention still need the
M3 measurement.

## Image and capability findings (audited by running, not by assuming)

| Image / component | Verdict |
|---|---|
| `otel/opentelemetry-collector-contrib:0.130.0` | **pinned.** Works on arm64. |
| `otel/opentelemetry-collector-contrib:1.2.0` | **does not exist** (`docker manifest inspect` → `no such manifest`). A stale pin that would have broken a cold `make up`. Restored to 0.130.0. |
| `otel/opentelemetry-collector-contrib:0.116.0` | **do not use on arm64.** The published arm64 image is empty: `exec /otelcol-contrib: no such file or directory`. |
| `python:3.12-slim` + `pymodbus==3.16.1` + OTel SDK `1.45.1` + OTLP/HTTP exporter | **works on arm64.** Pure Python; no `grpcio`, no native gRPC core. |
| `prom/prometheus:v3.1.0` | **works on arm64.** Native OTLP/HTTP receiver via `--web.enable-otlp-receiver`. |
| `cr.jaegertracing.io/jaegertracing/jaeger:2.19.0` | **works on arm64**, and is itself an OTel collector distribution. **Ships no config at all** — distroless-ish, binary at `/cmd/jaeger/jaeger-linux`, no `/etc/jaeger`. One must be mounted. |
| `persesdev/perses:v0.50.0` | works on arm64. **Do not mount over `/etc/perses`** — it keeps its CUE panel/query schemas there and dies with `open /etc/perses/cue/schemas/panels: no such file or directory`. Mount the config file and the provisioning folder only. |
| `grafana/grafana-oss:11.4.0` | works on arm64; 123 MiB resident for a dashboard layer, and no OTLP ingest of its own. |
| `nginx:1.27-alpine` | works on arm64; serves the HMI in 3 MiB. |
| `ghcr.io/.../telemetrygen:latest` | works on arm64; **no shell**. Its metrics are all named `gen`. |
| `ghcr.io/shopify/toxiproxy:2.11.0` | works on arm64. |
| `alpine:3.20` | works on arm64 (used for `ops`). |

Host-port collisions on this machine, recorded because they cost hours: an unrelated **ssh
port-forward** holds `4317`, `8889`, `9090`, `16686` and `8090`. A Docker port mapping that cannot
bind fails quietly — `docker compose port prometheus` printed nothing while the container still
reported a mapping — and connections to `127.0.0.1:9090` then hang forever against the tunnel.
So: Prometheus publishes no host port (`make prom Q='…'` reads it through the `ops` box, like
`make queues`), and the UIs run on overridable free ports (HMI `8091`, Jaeger `16687`).
Also recorded in docs/findings.md #32 for port 8888.

## Not yet measured

- Disk growth of the edge `file_storage` queues over a multi-hour outage (M3).
- **A 60-minute soak on a poller**, to put the RSS slope beyond argument. See "Poller memory" for
the method and for why short windows do not answer it.
- A seven-site projection. At today's numbers: 7 pollers ≈ 223 MiB, 8 collectors ≈ 400 MiB,
  hub + link + visual stack ≈ 140 MiB, so roughly 0.8 GB — inside D10, but the collector figures
  should be re-measured on the real hardware class before that is quoted as fact.
- Where logs should go. Neither visual-stack candidate stores them without adding Loki, which is
  the one component that could still threaten D10 (`transform/visual` and the OTLP fanout carry
  metrics and traces today; logs go to `debug` only).
