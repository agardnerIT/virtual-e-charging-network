# NEXT — pick this up here

Handover for the next agent. This file is self-contained: you should not need the previous
conversation. It assumes you have read `plan.md` (the source of truth for decisions) and skimmed
`docs/ot-primer.md` and `docs/standards.md`.

**Your task: M2 — sessions as traces.** Details in §4. Read §1 and §7 first.

> **⚠ M2 is already in progress — read [`docs/m2-build-log.md`](docs/m2-build-log.md) before writing
> anything.** A previous session did the recon and locked eight decisions (OCPP 1.6J; the charge
> point owns the session trace; the CP reads supply voltage from `device-rural` over Modbus; a
> separate boot trace; …), wrote four files under `sim/ocpp/`, and probed the pinned OTel SDK for
> exemplar support. The build log records all of that plus the exact remaining checklist, so you can
> continue instead of re-deriving it. Note: **this directory is not a git repository** — nothing is
> committed and there is no undo beyond what is on disk.

---

## 1. Where the project is

`M0`, `M1a`, `M1b part 1` and `M1b part 2` are complete and verified. What exists and runs:

- A two-layer OpenTelemetry Collector topology: **one edge collector per site, one hub collector.**
- A simulated impaired WAN between them (toxiproxy), with **three independent links**.
- **Three real sites, all on the same device image and the same poller** (D10), differing only by
  environment:
  - **Rural / Broken Hill** (`rural-brokenhill`) — a weak end-of-line charging site on a SunSpec
    model-203 Modbus meter. 4G link, 7 s slow clock, voltage sag, deadband, comms-lost.
  - **Solar / Mildura** (`solar-mildura`) — a utility-scale **SunSpec model-103 inverter** with two
    MPPT trackers (model 160), an irradiance/temperature station (302/303) and an export limit
    (123). 4G link, 4 s fast clock, DC/AC 1.26 so it clips at midday.
  - **Wind / Snowtown** (`wind-snowtown`) — six **SunSpec-shaped vendor-model turbines** on one
    Modbus device (repeating blocks), plus a farm connection point. Fibre link, 2 s slow clock.
- **The visual layer** (D18): the hub fans out over OTLP to **Prometheus** (metrics) and
  **Jaeger v2** (traces), and our own **`hmi/`** draws a map of Australia and a one-line mimic per
  site, fed only from that pipeline. Perses is available as an optional profile.
- The whole stack is ~749 MiB (`make stats`), against the 3 GB budget (D10). `telemetrygen` is gone.

### Verify the current state in ten minutes

```bash
make up                 # ends in a picture, not log output
make hmi                # http://127.0.0.1:8091 - map + drill-down to three mimics
make stats              # ~750 MiB, well under the 3 GB budget (D10)
make queues             # three edges, all 0

# the sag, end to end, and visible on the rural mimic
make fault SITE=rural FAULT=voltage-sag ARGS="--depth 0.78 --duration 20"

# the two generation regimes, side by side:
make prom Q='energy_inverter_power_real_watts{energy_site_id="solar-mildura"}'   # clipped flat top
make prom Q='energy_turbine_power_real_watts{energy_site_id="wind-snowtown"}'    # six disagree

# operator curtailment (different from inverter clipping; see D29)
make curtail SITE=solar PCT=60
make curtail SITE=solar PCT=100

# store-and-forward, unchanged from M0
./impairment/link.sh wind down && make queues     # wind > 0, others 0
./impairment/link.sh wind wet  && make queues     # all back to 0

# quality is real: stop the device and the retained values go comms-lost
docker compose -f compose/docker-compose.yml stop device-rural
```

**It may be night where you are.** The solar inertial curve is real local time, so at 2am the
solar farm is correctly asleep. For a daytime demo: `make up SOLAR_CLOCK_HOUR=12`. The wind farm
generates regardless.

### Verify the visual layer without a browser

Playwright is at `~/collabspot/node_modules`, but its bundled Chromium build may not match the
installed version. Point it at the cached one:

```javascript
// NODE_PATH=~/collabspot/node_modules node /tmp/diag.cjs
const { chromium } = require('playwright');
const page = await browser.newPage();
await page.goto('http://127.0.0.1:8091', { waitUntil: 'domcontentloaded' }); // NOT networkidle
await page.evaluate(() => openSite('wind-snowtown'));
console.log(await page.evaluate(() => [...state.points.values()]
  .filter(p => p.labels.energy_site_id === 'wind-snowtown').length));
```

**`waitUntil: 'networkidle'` never fires on this page** — it polls Prometheus every 2 s, so the
network is never idle and the call hangs until Playwright's timeout. Use `domcontentloaded`. And
**do not screenshot with Chrome's `--virtual-time-budget`** (7.17).

### Open — flag to the owner, don't guess

- Whether the collector's **own telemetry** should traverse the impaired link (today it is read
  locally by the `ops` container — still the deliberate M0 simplification).
- The **edge → hub transport**: gRPC today. HTTP is arguably more robust on a hostile field
  network, but reliability comes from the disk-backed queue either way (D26).
- **Where logs should go.** Metrics and traces have a local store; logs go to `debug` only. Loki is
  the one remaining component that could threaten D10 — measure before adding it.
- **The `.drawio` is now two milestones out of date** — it predates the visual stack *and* the two
  generation sites. `docs/architecture.drawio` is the source of truth; regenerate the PNG/SVG from
  it rather than editing images.

---

## 2. Hard constraints — do not violate

| Constraint | Source | Meaning |
|---|---|---|
| **OTel-native, no exceptions** | plan.md D15 | No ActiveGate, no OneAgent, no vendor agent. The collectors *are* the agents. Every backend is reached over OTLP only. |
| **Two collector layers, and only two** | D16 | Edge per site, one hub. Do not add a tier. |
| **arm64 only** | D10 | Must run on Apple Silicon. Verify any new image actually runs arm64. |
| **≤3 GB RAM total** | D10 | Currently ~749 MiB for three sites. Measure, then update `docs/sizing.md`. |
| **Docker Compose only** | D13 | No Kubernetes. |
| **Clean energy only** | D17 | No diesel, no thermal, no nuclear generation. |
| **One southbound protocol** | D4 | Modbus (SunSpec-shaped). Do **not** add DNP3, IEC 104, 61850, DLMS or OPC UA. |

---

## 3. Design decisions settled so far

D1–D29 in `plan.md`. The ones a new agent will trip over if not read:

1. **One image, three roles** (`sim/Dockerfile`): all three devices and all three pollers share it,
   and `sim/sunspec.py` is shared by device and poller so encoder and decoder cannot drift (D10).
2. **The poller reads and scales; the edge collector gives meaning** (D20). Device-shaped names and
   unit `1` leave the poller; names, units, sign, A/B/C → L1/L2/L3, quality and dual timestamps are
   the edge collector's job. Do not move semantics into the poller.
3. **Quality is `otel.data_point.quality`** (D21) — a workaround, and the repo's strongest
   contribution candidate.
4. **Deadband lives in the poller** (D22); heartbeat 60 s.
5. **Receive time is an attribute** (D23), not the metric timestamp.
6. **Prometheus + Jaeger + our own `hmi/`** (D24).
7. **OTLP↔Prometheus attribute adaptation happens only at the Prometheus boundary** (D25).
8. **Scan 1 Hz, report 5 s, site-local hop over OTLP/HTTP** (D26). WAN hop stays gRPC.
9. **One fault only: voltage sag.** Resist building a fault framework. Curtailment is a *control*,
   not a fault (D29).
10. **Wind uses vendor models, not a SunSpec model number** (D27).
11. **Shared models (123/vendor) are disambiguated by `energy.device.kind` at the edge** (D28).
12. **`energy.*` attributes stay hand-rolled** until M4 (Weaver registry).

---

## 4. The task — M2: sessions as traces

The narrative spine (plan.md §4) is *sag → charger derate → failed session*. M1 made the first two
legs real. M2 makes the third leg real and closes the three-signal story (D14):

- An **OCPP charge point** simulator at the rural site (OCPP 1.6J or 2.0.1 — pick one and say why).
- A **minimal but protocol-real CSMS** test double (Python/Node, tens of MB — D10 rules out
  SteVe + MySQL as the default; that becomes an optional profile).
- **A session is a trace**: `BootNotification → Authorize → StartTransaction → MeterValues… →
  StopTransaction → (a sag interrupts it)`. The sequence of events is the span tree; the energy is
  the metric. OCPP frames are logs.
- **A metric exemplar links to its session trace.** This is the payoff: the voltage sag is a metric
  data point, the failed session is a trace, and the exemplar is what lets a reader jump from one
  to the other. Check whether Prometheus' OTLP receiver preserves OTLP exemplars — if it does not,
  that is a finding worth writing down, not a thing to fake.
- The rural **EVSE01/EVSE02** boxes in `hmi/app.js` are currently dashed and labelled "arrives with
  OCPP sessions (M2)". Fill them in from the pipeline.

Keep the sag → derate → drop sequence injectable as one command if you can; `make fault` and
`make curtail` are the existing shape.

---

## 5. Definition of done for M2

- [ ] An OCPP charge point simulator and a CSMS test double run as containers in the rural OT zone.
- [ ] A charging session is a single trace with correctly-nested spans and a span for the sag.
- [ ] OCPP protocol frames appear as logs correlated to the trace.
- [ ] A metric exemplar (energy or power at the sag) links to the session trace, or a documented
      finding that the chosen store cannot carry it.
- [ ] The rural mimic shows the EVSEs with live session state, not "not instrumented".
- [ ] M1 regressions still pass: sag, deadband, queue fill/drain, quality → `comms-lost`.
- [ ] `make stats` → `docs/sizing.md`, still comfortably inside 3 GB.

---

## 6. Out of scope for M2

The metro and microgrid sites (M3) · additional fault types · load management under a capacity cap
(M4) · Weaver and the formal `energy.*` registry (M4) · OpAMP · DNP3/IEC 104/61850/DLMS/OPC UA ·
a log store · real ISO 15118 / Plug & Charge.

---

## 7. Hard-won findings — read before you lose an hour

**Carried forward from M1:**

1. **`otel/opentelemetry-collector-contrib:0.116.0` is broken on arm64** (empty image, `no such
   file or directory`). `0.130.0` is pinned and works.
2. **The collector images are distroless — there is no shell.** Read a collector's own telemetry
   via the `ops` container instead (that is what `make queues` and `make prom` do).
3. **`file_storage` needs `create_directory: true`,** and the edge services run `user: "0:0"`.
4. **This machine has an unrelated `ssh` port-forward holding `4317`, `8889`, `9090`, `16686` and
   `8090`.** A Docker port mapping that cannot bind fails *quietly*. Prometheus publishes no host
   port; the UIs are on overridable free ports (HMI `8091`, Jaeger `16687`).
5. **Always bound a host-side network client.** `curl --max-time`, `urlopen(..., timeout=)`.
6. **toxiproxy restore is not `POST toxics -d '[]'`.** DELETE each toxic and re-enable the proxy —
   `impairment/link.sh` does this.
7. **`otelcol_exporter_queue_size` counts batches, not items**, and 0.130 normalises metric names
   with double underscores. Match on prefix.
8. **`set(time_unix_nano, Now())` is a silent no-op in 0.130.0.** Hence D23. Do not re-add it.
9. **`groupbyattrs` merges metrics that share name + resource + scope.** Datapoint attributes
   survive; that is what turns per-phase points into one metric with N data points.
10. **A plain YAML scalar breaks on `: ` inside an OTTL string.** Quote it or reword.
11. **pymodbus 3.16 replaced the classic datastore API.** Use `SimData`/`SimDevice`; the
    `SimDevice(action=…)` hook runs on every read and is the seam for a live simulator. The client
    parameter is `device_id`, not `slave`/`unit`.
12. **Verify an image tag exists before you trust a pin:** `docker manifest inspect <image>`.
13. **Modbus carries no time.** The device exposes a vendor `UnixTime` register.
14. **`collector/edge/site.yaml` is shared by every site.** Anything site-specific must degrade
    cleanly when the env var or the device is absent.
15. **`target_info` is how you enumerate sites in the visual store** (resource attrs land there,
    not on the data — D25).
16. **Prometheus mangles metric names with their units:** `energy.meter.voltage` + `V` →
    `energy_meter_voltage_volts`; `m/s` → `meters_per_second`; `%` → `percent`; `1` → `ratio`.
    `POINTS` in `hmi/app.js` is the one place that mapping lives.
17. **A poller restart mints a new `service.instance.id`**, hence a new series set, with the old one
    lingering for the 5-minute staleness window. Never compare `prometheus_tsdb_head_series` across
    a restart.
18. **To measure a container's real memory, read `/proc/1/status` (`VmRSS`) and
    `/sys/fs/cgroup/memory.stat`**, not just `docker stats` — they disagree by ~20%.
19. **Mounting scratch config files from `/tmp` fails on this host** (`/tmp` is a symlink). Put
    them under the repo.
20. **`jaeger:2.19.0` ships no config file.** Mount one.
21. **Do not mount a whole directory over `/etc/perses`.** Mount the config file and the
    provisioning folder only.
22. **Do not screenshot the HMI with `--virtual-time-budget`** — it virtualises `Date.now()`.
    Use Playwright with a real clock, and `domcontentloaded`, not `networkidle`.

**New in M1b part 2:**

23. **Modbus function 03 reads at most 125 registers (0x7D).** The meter image (118) fit in one
    read; the inverter (142) and wind farm (187) do not. A too-large read is an *invalid request*
    and the device answers with an exception, so the poller must chunk (it now reads ≤120 at a
    time). The failure mode was silent: the poller only logged after 30 *successful* polls, so a
    total failure produced one line at startup and nothing else. It now logs the first failure and
    every 30th.
24. **Repeating a metric name in `create_observable_gauge` drops the later callbacks.** The Python
    SDK returns the same instrument for a repeated name and silently ignores the second callback
    set, so per-point registration loses tracker/turbine 2..n. Group points by metric name first,
    then pass every callback to one instrument. Symptom: one MPPT reported, the other vanished.
25. **`value_int` is 0 for a double-valued gauge.** OTTL's `value_int` only means anything for int
    datapoints; these gauges are doubles, so `value_int == 0` matched everything and every status
    rendered as "off". Use **`value_double`**. (`resource.attributes[...]` *is* available in the
    metric context, which is what D28 uses.)
26. **A free random walk will park on its clamp.** The wind model's first cut let the base wind
    wander down to the 2 m/s clamp and the whole farm idled at 0 kW after a few minutes. Use a
    mean-reverting (Ornstein–Uhlenbeck) walk toward a site mean.
27. **Sensor noise must be gated by the physical driver.** Gaussian noise on the irradiance reading
    leaked through the DC model and the solar farm generated a few kW at 2am. If the sun is down,
    irradiance is exactly zero; do not add noise to it.
28. **`make prom` ran `python3` inside the `ops` box, which is alpine and has none.** It only ever
    worked because someone had installed it into a running container. URL-encode on the host.
29. **An unused build dependency is still a broken pin.** `x-generator` is kept but profiled; if you
    ever use `telemetrygen` again, remember its image has no shell and every metric is named `gen`.

---

## 8. Conventions

- `plan.md` is the source of truth for decisions and is a living document: superseded decisions are
  struck through with the replacement noted, never deleted. Add decisions as `D30`, `D31`, …
- Keep the simulator code as small as it can be while staying recognisable to an electrical
  engineer. Depth buys credibility; breadth is opt-in (D4).
- `docs/standards.md` states fidelity per device. Update it when you add a device or a point.
- Update `docs/sizing.md` with measured numbers after any change that affects the footprint, and
  say which measurement it is (`docker stats` vs `VmRSS`).
- Prefer `make` targets over raw docker commands for anything a reader will want to repeat.
