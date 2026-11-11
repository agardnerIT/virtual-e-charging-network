# Sizing — measured, not estimated

Budget (D10): the default stack must fit comfortably in ≤3 GB on an Apple M2 / 8 GB machine,
arm64 only. Every number below was measured by running the stack. Re-run `make stats` after any
change that affects the footprint and update this file.

**Host:** Apple M2, 8 GB, arm64 (Docker Desktop, `arm64` images only).
**Stack:** M4 — 6 edge collectors (rural, solar, wind, metro, microgrid, highway), 1 hub, the impaired
link, the rural SunSpec/Modbus meter + poller, the solar SunSpec/Modbus inverter + poller, the
wind farm's SunSpec-shaped 6-turbine device + poller, the depot's meter + poller, and the
island's microgrid controller + poller (all five devices and all five pollers are the *same
image*), two OCPP 1.6J charge points (2 bays rural, 12 bays depot) with a CSMS test double per
site, plus the local visual stack (Prometheus, Jaeger, our own HMI). `telemetrygen` is gone from
the default path.

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

### M3-2 delta: the fifth site, the island (2026-11-11, `docker stats`)

A microgrid is one device, one poller and one edge collector — the same three containers as any
other site, because a site is an environment, not a code path (D10). Measured with the whole
five-site stack running (`make stats`), 24 containers in total:

| Component | Purpose | `docker stats` |
|---|---|---|
| `edge-microgrid` | edge collector, island (OT zone) | 45.4 MiB |
| `device-microgrid` | busbar + BESS + 2 sources + load, 110 registers | 21.8 MiB |
| `poller-microgrid` | same poller, `DEVICE_KIND=microgrid` (fresh process) | 34.1 MiB |
| **Total, whole stack** (24 containers) | | **1 048 MiB (1.02 GiB)** |

The whole five-site stack is **1 048 MiB** — *lower* than the four-site figure of 1 094 MiB, which
is not a contradiction and is worth understanding before quoting either: the pollers are the largest
single source of variance in this stack (see "Poller memory" below), and the four-site measurement
caught three long-running pollers at 68–125 MiB while this one caught a fresh one at 34 MiB. The
honest reading is that the *site cost* is ~100 MiB and the *stack total* is 1.0–1.1 GiB depending on
how long the pollers have been running. Still comfortably inside D10's 3 GB, with ~2 GB spare.

The island's series cost is 27 series in 23 families (D40), measured with
`make prom Q='count by (energy_site_name) ({__name__=~"energy_.*"})'`:

```
 27   Kangaroo Island Microgrid
 90   Snowtown Wind Farm
 40   Mildura Solar Farm
112   Tottenham Metro Depot
 41   Broken Hill EV Charging Site
```

The four source metrics carry *two data points each* (`energy_source_name` = solar, wind) rather
than being two metric names, which is the same repeating-block decision as model 160 (MPPT) and the
turbine model — and it is why 23 families cost 27 series rather than 31.

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

## Per-phase cardinality, measured — and the one control (M4, #11)

Every per-phase point is its own series in any attribute-flattening store. Measured on the running
five-site stack, counting only series that *actually reported* in the last 45 s (a plain `count()`
includes series that stopped sending minutes ago — findings #49):

```
make prom Q='count(last_over_time({__name__=~"energy_.*"}[45s]))'                      -> 213
make prom Q='count(last_over_time({__name__=~"energy_.*",energy_phase!=""}[45s]))'     ->  84
```

So **84 of 213 active series — 39 % — exist only to carry a phase label**: 28 per-phase metrics x
L1/L2/L3. With the control on (`make up VISUAL_PHASES=drop`), the same two queries give **130** and
**0**: 83 series, 39 % of the estate, removed from the visual store with nothing invented.

| | active `energy_*` series | of which phase-labelled |
|---|---|---|
| `VISUAL_PHASES=keep` (default) | 213 | 84 (39 %) |
| `VISUAL_PHASES=drop` | 130 | 0 |

**How it works, and what it deliberately does not do.** `filter/visual_phases` on the hub drops
data points whose attribute set carries `energy.phase`, and only on `metrics/visual`. The aggregate
is still there because the device publishes it: a SunSpec meter reports `V` as well as
`VphA/B/C`. **No averaging happens in the collector** — an average computed there would be a number
this repo cannot defend, and the per-phase data is still on the backend-of-record pipeline for any
backend that wants it. The control is about what a *dashboard* needs, not about what the data means.

What it does not do: no cardinality estimation, no per-tenant policy, no aggregation. If you need a
mean of three phases, the device already publishes one; if you need the phases, you have the other
pipeline. Reproduce with `make up VISUAL_PHASES=drop` and the two queries above.

---

## The marginal cost of a site, measured by adding two (M4, #18)

The headline number in this file is a *whole stack*, which is the wrong unit for anyone planning a
deployment: the question is what a **seventh site** costs. Multiplied estimates are not measurements,
so `compose/scale.yml` adds two whole sites — device, poller and edge collector each, same image,
same edge config — and `make stats` before and after gives the marginal cost directly.

| | containers | `docker stats` |
|---|---|---|
| five real sites + the visual stack + the market feed | 25 | 1 132.6 MiB |
| the same, plus two synthetic sites | 31 | 1 326.8 MiB |
| **marginal cost of one site** | **3** | **97.1 MiB** |

The two new sites on their own:

| Component | fresh process |
|---|---|
| device (SunSpec meter, 118 registers) | 21.0 MiB |
| poller (1 Hz scan, 5 s export) | 31.8 MiB |
| edge collector (disk queue, three pipelines) | 45.3 MiB |
| **one site** | **~98 MiB** |

**What that means for seven sites:** 1 133 MiB for today's five plus 2 × 97 = **~1 327 MiB**, which is
exactly what the measurement says, and it leaves ~1.7 GB of D10's 3 GB for a real backend and a real
log store. A site is ~100 MiB; the hub, the stores and the visual stack are ~560 MiB and do not scale
with sites.

**What the two synthetic sites are not**, stated so the number is not overclaimed (the overlay's own
header says this too): they send straight to the hub rather than through a toxiproxy proxy (the link
is one shared 8 MiB container, so the marginal cost is nil either way), and they have no charge point
and no CSMS. A site with a 12-bay OCPP pair adds the depot's numbers: 34 + 35 MiB, which is the same
order — the Python interpreter, not the work.

**And a warning that this file already half-contained:** `poller-metro` measured **103.6 MiB** in the
same `make stats` that caught a fresh poller at 31.8 MiB. Same code, same image, different age. That
drift is #17's subject; until it is explained, treat "a poller is 32 MiB" as "a *new* poller is
32 MiB".

---

## Loki, measured — the log store this stack does not ship (D45)

The default stack has no log store. That is a decision, and the usual justification for one —
footprint — does not survive measurement, so it is not the justification.

Measured on the running five-site stack (`docker stats`, single binary, filesystem storage,
6 h retention, config in `dashboards/loki/loki.yaml`):

| What | `docker stats` |
|---|---|
| Loki, started but idle | 30.3 MiB |
| Loki after 2 000 synthetic OCPP entries (a single stream) | 38.9 MiB |
| Loki receiving the hub's real OCPP log traffic from the sites | **52.5 MiB** |

So a log store costs about **50 MiB** — 5 % of the stack, well inside D10's ~2 GB headroom.
"Too expensive" would have been a false reason, and this section exists because it would have
been the easy one to write.

The recipe was also exercised end to end, not just documented: `--profile loki` for the
container, an `otlphttp/loki` exporter plus a `logs/record` pipeline on the hub, and then

```
{service_name="vecn-ocpp-cp"} | json | ocpp_transaction_id="42"
```

returned the charge point's own frames for one transaction. (Both service names — `vecn-ocpp-cp`
and `vecn-ocpp-csms` — appeared in Loki's label index, which is how the path was confirmed.)

The decision (D45) is therefore about **scope, not cost**: this repo's argument is that the data
belongs in metrics and traces, the charge point's OCPP frames are already trace-correlated, and
the only thing a log store adds is full-text frame search and the CSMS's side of the conversation
(which carries no `trace_id`). That is worth 50 MiB to someone and not worth a second query
language, retention policy and backup story in a teaching stack by default. Both halves are
stated in `docs/telemetry.md` §5 and the option is one profile away.

---

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

Each of these is an issue, not a note. Nothing here is a promise; they are the places where a
number in this file is missing or weaker than the prose around it.

- Disk growth of the edge `file_storage` queues over a multi-hour outage: **#3** (measured; the
  table lands here when that run finishes).
- The poller's **60-minute soak**, to put the RSS slope beyond argument: **#17**. See "Poller
  memory" for why short windows do not answer it.
- A **seven-site projection**, re-measured rather than multiplied: **#18**. At today's numbers
  7 pollers ≈ 223 MiB and 8 collectors ≈ 400 MiB, so ~0.8 GB — inside D10, but the collector
  figures should be re-measured before either is quoted as fact.
- **Where logs go.** Neither visual-stack candidate stores them without adding Loki, which is
  the one component that could still threaten D10. That is a decision, not a measurement: **#19**.
