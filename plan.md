# Virtual E-Charging Network — Plan

> **Last updated:** 2026-11-11
> **Status:** M0–M4 complete: six sites (rural, solar, wind, metro depot, highway hub, isolated microgrid), one
> hub, one impaired link per site, sessions as traces, store-and-forward through a measured outage,
> and a clock-skew demonstration that makes an effect precede its cause.
> **Work is tracked in [GitHub issues and milestones](https://github.com/agardnerIT/virtual-e-charging-network/milestones).**
> This file holds decisions, measurements and fidelity disclosures only; a doc names future work as an
> issue link (`#12`) and never as a TODO.
> **Living document rule:** this file is the source of truth for decisions. Superseded decisions are
> struck through with the replacement noted, never deleted.

---

## 1. What this is

A full end-to-end, **fully virtual** electric vehicle charging network — from the electrical network
(generation, distribution, behind-the-meter) through to charge points and home solar + wallboxes —
with **every layer instrumented via OpenTelemetry**, and the OpenTelemetry Collector used as the
integration point between operational technology (OT) and IT.

The reference implementation to prove out the claim: *OpenTelemetry is useful beyond applications.*

**Success criterion** (the sentence every scope decision is tested against): within ten minutes of
`make up`, a reader with no electrical background opens a map of Australia, watches a voltage sag
propagate into a failed charging session, and understands that OT data carries quality, time, units and
intermittency — and that a stock OpenTelemetry Collector carried all of it end to end.

**Hard constraint: OTel-native.** No ActiveGate, no OneAgent, no vendor agent of any kind, ever. The
collectors *are* the agents; every backend is an OTLP exporter and nothing more (D15).

**Generation is clean energy only:** one utility-scale solar farm and one wind farm (D17).

Three audiences, in priority order:

1. **Someone with an IT/observability background** — can clone it, run it, and learn how OT telemetry
   differs from app telemetry (this repo's owner is audience #1).
2. **Someone with an electrical/OT background** — recognises the values, units, alarms, event lists and
   failure modes as realistic, not "a JSON file pretending to be a charger".
3. **Someone with an OTel/observability platform background** — can point their own backend at it, because
   the output is plain OTLP and nothing else.

## 2. Scope

**In scope**

- Four simulated sites spanning realistic Australian grid conditions (see §5.2).
- Electrical: MV/LV transformer, feeder, protection, power quality, three-phase metering.
- Generation: one utility-scale **solar farm** and one **wind farm**, each a first-class, independently
  instrumented site (clean energy only — no thermal, diesel or nuclear generation anywhere in the
  network). Site-level solar PV and battery/BESS remain.
- Charging: AC posts, DC fast chargers, load management, OCPP charge point ↔ CSMS.
- Behind the meter: home solar, battery, wallbox, EV, tariffs, negative midday prices.
- IT: the CSMS itself instrumented with normal app telemetry (traces/logs/metrics).
- The observability pipeline itself: collector self-telemetry, backpressure, OpAMP remote management.
- Network reality: latency, packet loss, link outages, store-and-forward, clock skew, stale data.

**Out of scope (at least initially)**

- Real hardware of any kind. No physical chargers, inverters, meters or PLCs.
- Full IEC 61850 (MMS/GOOSE/Sampled Values) stack — out of scope, and **no gateway was built**.
  ~~Substation automation is represented by a gateway that emits OTLP with realistic IEC 61850-style
  measurements and quality.~~ *Struck 2026-11-11: nothing in the repo speaks or emulates IEC 61850, and
  saying so implied a component that does not exist (`docs/standards.md` §7). What the repo actually
  shows is the part that matters for telemetry — SunSpec/Modbus with an invented quality flag and two
  timestamps — and adding a fake 61850 gateway would be a claim without a reader.*
- Real market settlement, real payment processing, real vehicle CAN/ISO 15118 protocol work.
  ~~ISO 15118 Plug & Charge appears as an identity/certificate *metadata* story, not a protocol
  implementation.~~ *Struck 2026-11-11: the charge point carries an `idTag` per bay (OCPP 1.6's own
  authorisation), which is the identity story; there is no certificate, no contract certificate, no
  Plug & Charge. Say "idTag", not "15118".*
- Kubernetes. Docker Compose only (see D13).
- Industrial protocol breadth on the critical path. **One** southbound device protocol (Modbus/SunSpec)
  plus OCPP teaches every OT telemetry lesson: units, scaling, sign, names, absent timestamps, absent
  quality, polling load, deadbands, session-as-trace. DNP3, IEC 60870-5-104, IEC 61850, DLMS/COSEM and
  OPC UA are documented as *mappings* (and may return later as opt-in profiles), not built by default.
  See D4.

---

## 3. Decisions log

| # | Decision | Rationale |
|---|---|---|
| **D1** | Primary deliverable is a **repo others can run**, optimised for learning, not a talk script. | Owner's answer to Q1. Talks can be derived from it later; a script cannot be derived from a script. |
| **D2** | **Vendor neutral by construction.** The collector's exporter set is configuration, not architecture. Default profile ships with the `debug` exporter only. | Owner uses Dynatrace; others will use Grafana, Honeycomb, Elastic, SigNoz. Backend choice must never be a prerequisite to running the repo. |
| **D3** | A **local lightweight visualisation** is built, but it is not the observability backend. It is a *proof of life*. | Two distinct needs: (a) "did my telemetry arrive" and (b) "show me my data". Mixing them couples the repo to one vendor. |
| **D4** | ~~Electrical realism is **not** cut down.~~ **Realism means "recognisable to an engineer", not exhaustive.** Values, units, curves and failure modes must survive a domain reader's scrutiny; the long tail of industrial protocols does not have to be implemented to earn that. Domain gaps are filled by `docs/ot-primer.md` and checked against public standards. | Amended (was: "don't cut corners"). The original wording implied a multi-month simulator project and pushed the OTel story to the end. Depth buys credibility; breadth is opt-in (§2, D16). |
| **D5** | Prefer **existing OSS simulators and components**; write custom code only where the domain is thin (energy semantic conventions, OT quality/validity handling, network impairment). | Owner's answer to Q4. |
| **D6** | Anything we build that fills an OTel gap is written **upstream-shaped**: Weaver registry, collector component, semconv proposal — not a private fork. | Owner's answer to Q4: "we can build it and contribute back". |
| **D7** | **Multi-site across Australia, heterogeneous by design** — four site archetypes, not four copies of one site. | Owner's answer to Q5. Patchy connectivity is a *feature* of the model, not a nuisance to work around. |
| **D8** | ~~**OpAMP is in scope**, but it arrives with multi-site (M3+), and M0–M2 must not depend on it.~~ **OpAMP is deferred off the critical path** (post-1.0 stretch). Nothing in the default path may depend on it. | Amended: remote config is genuinely how you would manage N edge collectors, but it is a management-plane story, invisible to the "OTel carries OT telemetry" narrative. Revisit once D16 and the map are real. |
| **D15** | **OTel-native only. No ActiveGate, no OneAgent, no vendor agent of any kind.** The OpenTelemetry Collectors *are* the agents. Every backend is reached through an OTLP exporter (gRPC or HTTP) and nothing else. | Owner's constraint. D2 is the architectural expression of this; D15 makes it non-negotiable. It also improves the story: "the collector is the agent in OT" becomes something the repo demonstrates rather than asserts. |
| **D16** | **Two collector layers, and only two: an edge collector per site, and one hub collector.** Edge = protocol adapter, unit/scale/sign/tag normalisation, quality + dual timestamps, deadband, disk-buffered store-and-forward, single egress. Hub = ordering, enrichment, tail sampling, fanout to N backends, self-telemetry. | Standard OTel agent→gateway fan-in. One layer leaks every site's protocol quirks into a single config and destroys store-and-forward; three layers is regional aggregation that four-to-seven sites do not need. Add a third only when site count makes the hub the bottleneck. |
| **D17** | **Generation is clean energy: one solar farm and one wind farm**, each a first-class site with its own edge collector. No thermal, diesel or nuclear generation. The isolated site becomes a solar + wind + BESS microgrid. | Owner's answer. Two generation assets give the map its "power station" anchors and give the pipeline two contrasting generation regimes: solar's clipping midday curve and wind's high per-turbine variance with farm-level smoothing. |
| **D18** | **Visuals (map, mimic, trends, alarms) are a required deliverable, but they come after the collector layer.** Telemetry first, pictures second. Every view is fed from the pipeline — OTLP/collector output — never from a vendor agent and never by reading the simulators directly. | Owner's sequencing. Keeps the collector topology as the first thing validated, and keeps every visual a genuine end-to-end proof rather than a direct read of the simulator's internal state. |
| **D19** | **The Modbus poller is written in Python (`pymodbus`), not Go.** It is a device-facing adapter that emits OTLP; the collector remains the gateway for everything northbound. Extracting it into a real Go `modbus` receiver is explicitly deferred to the rule of two (D6). | Owner's answer. OTel has no Modbus receiver in core or contrib, so something must speak the protocol — but the reusable, upstream-shaped artefact should be extracted *after* M1–M2 show which transformations every integrator actually writes by hand, not guessed at up front. Python keeps the first slice cheap. |
| **D9** | **Docker Compose is the only runtime.** Devcontainer support is a goal, targeting the same compose file. | Owner's answer to Q7. |
| **D10** | Hard budget: **runs in ≤3 GB of RAM on an Apple M2 / 8 GB machine**, arm64 images only. Aggregate profiles are opt-in with a warning. | Owner's answer to Q7. Rules out Grafana+Loki+Tempo+Mimir as a default, and rules out Java/MySQL CSMS as a default. |
| **D11** | Sites are simulated as separate containers with **network impairment applied between site and hub** (latency/loss/bandwidth/outage), not as one LAN. | D7. Without impairment the "OT is not a datacentre" story is a claim, not a demo. |
| **D12** | **Quality and provenance are first-class**, not metadata afterthoughts: every OT data point carries validity (good/stale/bad/comms-lost) and both source and receive timestamps. | This is the single biggest semantic difference between OT and app telemetry, and the biggest gap we can contribute back (see §11). |
| **D13** | No Kubernetes. | Consistent with `autonomous-observability`; compose is the lowest-friction clone-and-run. |
| **D14** | Metrics are the default transport for continuously-varying electrical values; **traces are reserved for sessions and sequences of events**; logs for alarms and protocol frames. | OT has three distinct shapes (point values, sequences, events) that map almost 1:1 onto OTel's three signals. See `docs/ot-primer.md` §6. |
| **D20** | **M1a split of responsibility: the poller reads and scales, the edge collector gives meaning.** The poller is a generic, map-driven Modbus adapter — it applies the device's own scale factors and word order (only device-facing code knows them) and emits *device-shaped* metrics (`modbus.register.vpha`, unit `1`, raw integer and scale factor as attributes). The edge collector owns names, units, sign, the A/B/C → L1/L2/L3 mapping, quality and dual timestamps. | The M1a task specification overlapped here; resolved so the semantic work lands where the plan says the teaching is (the edge collector, D16), while device-specific scaling stays with the device-facing code. |
| **D21** | **OT quality is a datapoint attribute, `otel.data_point.quality` (good / stale / bad / comms-lost), attached by the edge collector's `transform` processor.** | OTel has no native notion of a value that is present but not trustworthy. Working default for M1a; strongest contribution candidate, §11.1. |
| **D22** | **Deadband is implemented in the poller, not the collector.** | contrib has no stateful deadband processor — `filter`/`metricstransform` cannot compare a value with its predecessor. Source-side change-of-state reporting is also the OT-correct place. Contribution candidate, §11.6. |
| **D23** | **The collector's receive time rides as a datapoint attribute (`energy.receive.timestamp`), not as the metric timestamp.** | OTLP metrics have no per-datapoint receive time, and 0.130's `transform` silently ignores `set(time_unix_nano, …)` — verified against the pinned image, not assumed. The metric's own timestamp stays the edge observer's; the device's claim is `energy.device.timestamp`. A recorded deviation from the §4.3.3 working default. |
| **D24** | **The local visual stack is Prometheus (OTLP/HTTP receiver) + Jaeger v2 (OTLP/gRPC) + our own `hmi/`. Perses is optional; Grafana is not shipped.** All three were run and measured against real hub-fed data: 80 MiB for this stack, 162 MiB with Perses, 276 MiB with Grafana OSS. | RAM did **not** decide it — every candidate fits D10 easily, which was itself the useful result. The deliverable is a map and a one-line mimic with values *in place* (§7, D18), and neither Perses nor Grafana draws that; Grafana would have cost 86 MiB more for a panel that still needed custom code. Perses stays available as `make up STACK=perses` because YAML dashboards are the better as-code story for a trends view, and nothing depends on it. Consequence: the whole 3 GB budget remains available for the real backend recipes. |
| **D25** | **OTLP attributes are adapted to Prometheus' data model at the Prometheus boundary only (`transform/visual` in the hub), never in the OTLP-native path.** Per-sample payload is shed on that path; five bounded site attributes are copied resource → data point on that path. | Two measured facts force it. (1) Prometheus flattens *datapoint* attributes into series identity, so a value that changes every poll mints a new series every poll: 4 532 `energy_*` series in 15 minutes from one meter, ~86 000 per point per day, and 80 MiB of Prometheus. (2) The receiver puts *resource* attributes on a synthetic `target_info` series and not on the data — so `energy.site.id` was never on a metric, and §4.2's “the map needs nothing else” was false for Prometheus. Shedding and re-stamping only at that boundary keeps the resource/datapoint split the spec intends while making the map work without an `on(job) group_left` join. |
| **D26** | **The poller scans at 1 Hz and reports every 5 s, and the site-local hop to the edge collector is OTLP/HTTP, not gRPC.** The edge → hub hop stays gRPC. | The report rate used to be the scan rate, which made the poller open a gRPC export once per second: 1 106 metric points a minute across a link whose story is that it is a 4G backhaul, and the exact opposite of what the deadband is for. Fixed → ~195 points/min, CPU 6.09% → 0.17%, and RSS stopped climbing (33 → 232 MiB had become the steady state; see docs/sizing.md). On a hop that never leaves the site network, gRPC's multiplexing and framing buy nothing for 200 points a minute and cost a native `grpcio` (~21 MiB, measured); removing it also removes the transport from the site image. The WAN hop keeps gRPC: reliability there comes from `retry_on_failure` + the disk-backed queue, which is transport-independent, and the impairment profiles and queue accounting are already validated against it. HTTP is arguably the more robust choice for a hostile link (middlebox traversal, no idle connection to reap) and is recorded here as an open choice rather than churned. |

| **D27** | **The wind farm uses SunSpec-shaped VENDOR models (64010 turbine ×6, 64011 farm connection point), not a SunSpec model number.** The turbine model repeats, one block per machine, and the block index rides as `modbus.register.instance` — the same mechanism model 160 (MPPT) uses. | SunSpec has no turbine model in the 100-series that anyone shipped, so assigning one would imply conformance that does not exist. The vendor models still follow every SunSpec register convention (integer + scale factor, acc32, sentinel, model chain), which is what `docs/standards.md` is for. Reusing the repeating-block mechanism costs nothing new and keeps six turbines as one metric with six data points. |
| **D28** | **Shared models (123 controls, vendor 64001) are disambiguated at the edge by `energy.device.kind`, not by separate names per device.** | The solar inverter and the wind farm both implement model 123 and the vendor model, and the first build had `transform/inverter` rename the wind farm's export limit to `energy.inverter.export_limit` — a wrong name, not a missing one. The model number belongs to the device, so the device kind is the right discriminator, and it keeps one meaning per metric name across the shared edge config (D16). |
| **D29** | **Generation can be forced to a daytime hour and curtailed on demand: `make up SOLAR_CLOCK_HOUR=12`, `make curtail SITE=solar PCT=60`.** | The success criterion is a ten-minute demo (plan.md §1), which must not depend on the wall clock or on the wind happening to blow. A solar farm at 2am is correctly asleep, which is honest but not a demo; pinning the sim hour is the smallest possible lever. Operator curtailment is a different fact from inverter clipping, so it has its own control rather than being inferred. |
| **D30** | **OCPP 1.6J, not 2.0.1.** The charge point and the CSMS test double speak 1.6J over a real WebSocket with the `ocpp1.6` subprotocol. | 1.6J is the deployed base in Australia, and its message-by-message shape maps most literally onto the span tree: `Authorize` → `StartTransaction` → `MeterValues…` → `StopTransaction` are five named protocol events, whereas 2.0.1 collapses start and stop into one `TransactionEvent`. §4 names a failed `StartTransaction`, which 1.6 has and 2.0.1 does not. |
| **D31** | **The charge point owns the session trace. The CSMS emits logs only and is correlated to the trace by `ocpp.transaction.id`.** `BootNotification` and the connector `StatusNotification`s get a separate short trace (`ocpp.boot`). | OCPP 1.6 has nowhere to put a W3C `traceparent`, so two ends cannot share a trace id, and two independent traces joined by nothing would be worse than one honest one. One owner keeps “a session is a single trace” true. A boot is a property of the connection, not of a transaction, so it is not part of the session tree. The logs still carry `trace_id`/`span_id` on the charge-point side because the logging call happens inside the span — verified on the exported OTLP log record, not assumed. |
| **D32** | **The charge point reads supply voltage from `device-rural`'s SunSpec meter over Modbus**, using the same `sunspec.py` map as the poller, and derives its derate from the measured phase voltage. | It makes the spine one injection: `make fault SITE=rural` changes a register, and the derate and the failed session are the charge point reacting to a meter it can actually see. A second fault-injection point inside the CP would be authored causality. The CP is therefore the only process that is both an OCPP client and a Modbus client, which is the interesting shape. |
| **D33** | **The exemplary link from a metric data point to its session trace is wired, but the chosen store cannot carry it: Prometheus' OTLP receiver does not ingest OTLP exemplars.** Measured on 3.1.0 (pinned) and 3.8.1 (latest at the time): the metric arrives, `/api/v1/query_exemplars` returns `[]`. The SDK is not the problem — the exemplar is present in the OTLP payload with its `trace_id` and `span_id`, verified by decoding the request body. | Recorded rather than faked. Consequences: (a) the charge point still stashes the session/sag span context on every observation, so the exemplars will appear the day the receiver supports them and nothing has to be re-plumbed; (b) the join available today is `ocpp.transaction.id`, which is on the session span, on every OCPP log line and on `energy.session.energy`. §11.2 becomes a stronger contribution candidate: this is a real gap between OTLP and the most common OTLP-native Prometheus path. |
| **D34** | **The CSMS is a protocol-real test double (Python, in-memory), not SteVe + MySQL.** SteVe remains an optional profile. | D10's 3 GB budget and §8 rule out a JVM and a database as the default: they are the two most expensive things in the stack, and the CSMS's storage engine is not what this repo is about. What matters for telemetry is that the wire protocol is real — upgrade, subprotocol, JSON frame arrays, transaction ids, `Blocked` id tags — which is what the double implements. |
| **D35** | **The charge point emits semantic `energy.evse.*` / `energy.session.*` names directly (it is an application, not a device poller) but sets `energy.quality`, so the edge collector remains the single author of `otel.data_point.quality`.** | Keeps D20/D21 coherent: the edge's datapoint block is not name-gated, so an application's quality goes through the same conversion as a meter's, and the HMI renders “the box that went comms-lost” the same way whichever side produced it. |
| **D36** | **EVSE metrics carry only EVSE-stable attributes; session-scoped identifiers ride on spans and logs.** The one exception is `energy.session.energy`, which carries `ocpp.transaction.id` because a per-transaction counter is the thing you want to query. | Prometheus flattens a data point attribute into series identity (D25), so putting the idTag, vehicle id and transaction id on every EVSE metric would mint a series per session for no extra query power. |

| **D37** | **The metro depot is one charge point with twelve connectors, and each site has its own CSMS test double.** Sites differ by environment and scale, never by code path: the depot runs the same `cp.py`, `site.py` and edge config with `CP_EVSE_COUNT=12`. | OCPP 1.6 is per-connector, so twelve outlets on one CP is legal and is what a power cabinet looks like; it also keeps the concurrency inside one process, where a crossed transaction id is visible rather than hidden by the network. A second CSMS instance instead of one shared one keeps the two OT zones disjoint — the property the repo exists to demonstrate — at the cost of a fidelity claim about central CSMSs, which `docs/standards.md` states. |

| **D46** | **One cardinality control, on the visual path only: drop per-phase data points when `VISUAL_PHASES=drop`. No aggregation in the collector, no estimator, no per-tenant policy.** | Measured: 84 of 213 active `energy_*` series (39 %) are per-phase, and dropping them takes the visual store to 130 series with the device's own aggregate left in place (`docs/sizing.md`). One control because every additional lever is a second place semantics can differ between two backends; the backend-of-record pipeline keeps the phases, which is the whole point of having two pipelines. |
| **D48** | **The market signal is a recorded day of five-minute prices, replayed in phase with the time of day — not a live NEM feed and not a static tariff.** `energy.market.price` carries `energy.market.price.feed_age` so a reader can see it is a recording. | Live price means an API key, a network dependency and a rate limit in a repo whose whole value is `make up` working offline; a static tariff means the price is a constant and a cost chart is a straight line. A recording keeps the *shape* that matters (a midday negative-price strip, an evening peak) with no dependency, and the age metric keeps it honest. `docs/standards.md` §7 states that this is not a market model. |
| **D49** | **The upstream contribution is the validity/quality convention, prepared as a ready-to-file issue rather than filed on someone's behalf; the deadband processor is its smaller sibling and stays second.** The evidence package is `docs/upstream/ot-quality-attribute.md`: the clock inversion, the stale-vs-comms-lost measurement, the 4 532-series cardinality number, and the fact that delivery, device and time failures currently share one word. | Rule of two (D6) is satisfied — both gaps have hurt twice, in two different components — and the ranking criterion is "gap size × our ability to demonstrate it", which quality wins on both counts. Filing it needs the maintainer's own account and name, so the honest state is "prepared, not filed", with the reasoning and the ask written down. If it lands upstream, this repo's attribute becomes a convention rather than a workaround (D21). |
| **D50** | **The highway hub's capacity cap is an OCPP `ChargePointMaxProfile` pushed by the CSMS, shared equally between the bays that are charging — and the reason it is capped appears as an attribute, `energy.evse.throttled_by = voltage \| load_management`.** | A site with more bays than power needs a load-management decision, and OCPP 1.6 already has the mechanism for it: `SetChargingProfile` with `connectorId: 0` is a limit on the whole charge point. Equal share is what a simple layer does and what an operator expects before anyone configures priority; per-connector profiles this CP cannot honour are refused with a reason rather than partially applied. The attribute is the part worth keeping: "the offer fell" is ambiguous between the grid sagging and our own policy, and those have different remedies (same argument as `energy.bess.hold_reason`). |
| **D44** | **Keep `energy.*` as the single public prefix, and structure the registry internally into groups that map onto the SIG boundaries a split would use.** | A split (`grid.*` / `solar.*` / `ev.*`) would align with how separate OTel SIGs think, at the cost of the property the repo is built on: one vocabulary for everything electrical at a site, and one selector (`{__name__=~"energy_.*"}`) that finds it. Measured cost of a split today: 322 occurrences of an `energy_*` name across 114 distinct families, 115 of them in `hmi/app.js` and 83 in `docs/telemetry.md` — a rename is a day of carefully matching strings, and the *value* of the split is a review boundary that a Weaver registry can express without renaming anything. So: one prefix, groups inside it (`energy.meter`, `energy.inverter`, `energy.turbine`, `energy.windfarm`, `energy.microgrid`, `energy.bess`, `energy.evse`, `energy.session`), and #9 publishes it that way. Revisit only if a SIG publishes a conflicting convention — then the split is a rename of groups, not of metrics. |
| **D45** | **No log store in the default stack — and the reason is scope, not footprint.** Logs go to the hub's `debug` exporter; `--profile loki` plus a documented three-line hub recipe exists for anyone who wants frames as a query. | Measured: Loki with real OCPP traffic from all five sites costs ~50 MiB (`docs/sizing.md`), so the usual justification would have been false. The real argument is that the charge point's OCPP frames are already trace-correlated, the CSMS's copies are the only thing a log store adds, and a second query language plus retention and backup story is a large teaching surface for one signal. Revisit if a user needs frame search rather than session reconstruction. |
| **D47** | **The project keeps the name "Virtual E-Charging Network" (VECN) and stops looking for one.** The repository, the compose project (`otel-ot`), the `energy.*` prefix and the milestone structure all stay. | Naming was on the open-questions list for three milestones, which is three milestones of a decision nobody needed to make before the thing worked. "Descriptive, not memorable" is a real objection and it is also the objection that does not block a single issue: the thesis is carried by the README's first paragraph and by the five observability patterns, not by the name. A rename now would touch every document, image name and issue link to buy a slogan. If the project ever needs a name for the front page, that is a rename issue with a concrete audience — not a decision to keep deferring. |
| **D42** | **Ordering is by receive time; device time is provenance and is never rewritten.** Both timestamps ride the signal (D12, D23), and a backend that sorts by the device's clock will reorder events across sources — demonstrated: a charge point's session stops 3 s after the sag that caused it, and the meter's device timestamp puts the cause 236 s *after* the effect (`tools/clock-inversion.sh`, `docs/ot-primer.md` §5.1). | A clock offset that is constant across *one* source cancels out; an inversion needs two clocks, which is exactly what a metric-from-a-device plus a span-from-an-application is. Correcting on ingest hides the disagreement and destroys the only evidence that a device's clock is wrong. Prometheus already stores metric samples at receive time, so the local stack obeys this by construction; the attribute is what a backend can get wrong. |
| **D43** | **A skewed device clock is `stale`, not `comms-lost`, and that word means "the device's own clock says this reading is old".** Measured: a meter 300 s behind had 28 of its series carrying `stale` while the edge queue was 0 and the charge point is still charging at 0.99 pu; 240 s ahead reports `good` everywhere and puts timestamps in the future. | The quality vocabulary has four words because there are four different failures (D21). Collapsing "wrong clock" into "comms lost" sends an operator to the wrong plant, and the ahead case is invisible precisely because nothing complains — which is why the answer is "never sort by device time" rather than "reject stale data". |
| **D38** | **The microgrid's grid former is a grid-forming BESS on a 1.5 MW / H=2 s island, and its primary signal is frequency, not voltage.** The model is droop control (`P = (f_nom − f)/(f_nom·R)·S_base`, R = 2 %) plus the swing equation, integrated at 20 Hz inside the device because the equation is stiff and a once-per-scan integration oscillates. | The same event at two sites must look different for a reason: a weak feeder explains a sag, and an island has no upstream network to explain anything. Frequency is what an operator would actually watch on an island, and the low inertia is the honest consequence of inverter-based generation. `docs/standards.md` §4 separates the modelled physics from the numbers invented for visibility. |
| **D39** | **A load step is an operational event, not a fault.** The microgrid's reproducible event is `make step KW=…` (a block of EV chargers closing contactors) plus an automatic 300 kW step every four minutes; the one *fault* stays the voltage sag (D29). | D29 exists because a fault framework is a project of its own. A load step is not a fault — it is what happens to a microgrid every day — so it needs no new injection mechanism, and it makes the site's behaviour visible without anyone typing a command. |
| **D40** | **A site's series budget is measured per site and stated before it is claimed.** The island is 27 series in 23 families; the source families carry two data points each (`energy_source_name`), not two metric names. | The repeating-block pattern (160/MPPT, 64010/turbine) applies again: one metric with a dimension is a query, N metric names is a schema leak. Measured with `count by (energy_site_name)`, in `docs/telemetry.md` §1. |
| **D41** | **Measuring a single site's value means taking the freshest sample, not the first series.** A poller restart mints a new `service.instance.id`, which is a new Prometheus series, so an instant query can return the value of a process that died ten minutes ago. Both the HMI (newest-wins) and `tools/*-measure.sh` (pick by sample timestamp) do this explicitly. | Found while recording the microgrid load step: frequency read 49.99 against a battery at 296 kW, which is not the droop relationship. The data was right and the query was wrong — the worst kind, because it looks plausible. |
---

## 4. The spine of the demo

One narrative, three layers, one telemetry pipeline:

1. A **voltage sag** propagates on a weak rural feeder (electrical plane).
2. The DC charger **derates** and the AC post **drops a session** (OT control plane, OCPP).
3. The CSMS sees failed `StartTransaction`, degraded power delivery, and an auth retry storm (IT plane).

The sag is a metric with an exemplar; the derate is a span in the session trace; the CSMS failure is a
log with a trace ID. **If a reader can follow that single event from a millisecond-class PQ event to an
application error, the project has succeeded.**

As built in M2, three of those four legs are real and one is documented as blocked: the sag is a metric,
the derate and the failed session are one trace, and the CSMS's records carry `ocpp.transaction.id` (and,
on the charge-point side, the trace id). The **exemplar** leg is not: Prometheus' OTLP receiver does not
ingest OTLP exemplars (D33). That is now a finding rather than an assumption, and the wiring is in place
for the day it is fixed.

Everything else in the repo exists to make that journey possible at realistic fidelity.

---

## 5. Architecture

### 5.1 Planes

```
  GENERATION / GRID            SITE / CHARGING              BEHIND THE METER          IT / DIGITAL
  -------------------          ---------------              ----------------          ------------
  MV/LV transformer            AC posts (7-22 kW)           Solar PV inverter         CSMS application
  Feeder + protection          DC fast charger (50-350 kW)  Battery + inverter        OCPP gateway
  Power quality (V/I/THD/PF)   Load management              Home meter (NMI)          Auth / payments
  Solar farm (utility-scale)   DC fast charger (50-350 kW)  Battery + inverter        OCPP gateway
  Wind farm (utility-scale)    Load management              Home meter (NMI)          Auth / payments
  Frequency / ROCOF            OCPP <-> CSMS                Wallbox (OCPP/Mode 3)     Database
  Spot price (NEM)             Site energy meter            EV (SoC, taper curve)     Observability stack
```

Cross-cutting, present at every plane: **link/backhaul health** (4G / Starlink / fibre), **time
accuracy**, **data quality**, **alarms**.

### 5.2 Site archetypes (D7)

| Site | Location model | Grid reality | Backhaul | What it uniquely demonstrates |
|---|---|---|---|---|
| **Metro depot + home** | Melbourne | Strong, 3-phase, PF ~0.98 | Fibre, 5 ms | Solar + battery + wallbox, negative midday spot price, charge-on-solar, home energy flows |
| **Highway charging hub** | Hume / Ballarat | 3-phase, DNSP connection cap (e.g. 200 kVA), export limit | 4G, 30–80 ms, occasional loss | Load management under a hard capacity cap, dynamic current allocation, DC charger module switching |
| **Weak rural end-of-line** | Broken Hill / Mildura | Long feeder, poor voltage regulation, sags, harmonics, high unbalance | 4G, 80–250 ms, 1–3% loss, outages | The §4 story. PQ events, sag classification, charger derate, stale-data handling |
| **Isolated microgrid** | Coober Pedy / Longreach | Off-NEM: solar + wind + BESS, own frequency droop, load shedding | Satellite, 500–700 ms, long multi-hour outages | Frequency as a first-class signal, BESS SoC, curtailment, **days-long store-and-forward** |
| **Solar farm** *(generation)* | Near Mildura / Broken Hill | Grid-connected, high irradiance, export-limited | 4G, 30–80 ms | Utility-scale PV: irradiance, panel temperature, DC/AC ratio + clipping, inverter/string availability, curtailment, spot-price-driven behaviour |
| **Wind farm** *(generation)* | Near Port Augusta / Snowtown | Grid-connected, strong wind resource | Fibre, 10–20 ms | Wind speed/direction, per-turbine variance, farm-level smoothing, nacelle availability, curtailment, frequency contribution |

Build order: the **weak rural end-of-line** (site 3 — the §4 story) and **one generation site** come first;
between them they prove the collector pattern for a tiny remote load *and* a large remote source. The
metro site and the microgrid follow. Load management (site 2) is last, because it is a control story
rather than a telemetry story.

### 5.3 Collector topology — two layers, and only two (D16)

**Yes: there is a collector at each remote site, and yes, they fan in.** This is the standard OTel
agent→gateway pattern, not a bespoke design.

- **Layer 1 — edge collector, one per site, inside the OT zone.** The only thing on site that talks
  outward. It normalises units/scale/sign/tag names, attaches quality and both timestamps, applies
  deadband and downsampling, buffers to disk, and emits **OTLP/gRPC northbound and nothing else**.
- **Layer 2 — hub collector, one, in the NOC/IT zone.** Receives OTLP from every edge, checks ordering,
  enriches, tail-samples, fans out to N backends, and emits its own telemetry back into the pipeline.

Why the edge collector must be local, and why there are exactly two layers, is argued in D16. In short:
devices cannot speak OTLP, links fail, and cardinality has to be shed at the source — but four-to-seven
sites do not justify a regional aggregation tier, and adding one before it is needed is the kind of
"complex" that is not "necessary".

```
  ┌─ SITE (edge, OT zone) ───────────────────────────────┐
  │  device sims / device poller (site-local network)     │
  │   modbus/sunspec · ocpp charge point · meters         │
  │  ┌────────────────────────────────────────────────┐   │
  │  │ EDGE COLLECTOR             (one per site)      │   │
  │  │  receivers : otlp ← local device poller,       │   │
  │  │              hostmetrics                       │   │
  │  │  processors: resource(site · geo) → transform  │   │
  │  │              (units, scale, sign, tags) →      │   │
  │  │              quality + dual timestamps →       │   │
  │  │              filter/delta (deadband)           │   │
  │  │  exporters : otlp/hub                          │   │
  │  │  durability: file_storage + sending_queue      │   │
  │  └────────────────────────────────────────────────┘   │
  └──────────────┬────────────────────────────────────────┘
                 │ OTLP/gRPC over impaired link
                 │ (toxiproxy: latency · loss · bandwidth · outage)
                 ▼
  ┌─ NOC (IT zone, hub) ──────────────────────────────────┐
  │  HUB COLLECTOR            (one; more only if needed)  │
  │   receivers : otlp                                     │
  │   processors: order/dedupe · enrich · tail_sampling    │
  │   connectors: routing / forward                        │
  │   exporters : debug · local stack · vendor (all OTLP)  │
  │   self-telemetry → same pipeline                       │
  └──────────────┬─────────────────────────────────────────┘
                 ▼
        any OTLP endpoint (D2 / D15)
```

Three deliberate demonstrations:

- **The collector as the OT/IT boundary.** The edge collector is the only thing in the OT zone that talks
  outward: protocol adapter, trust boundary, buffer and sampling-policy engine in one. In app-land the
  collector is plumbing; here it is load-bearing infrastructure.
- **The collector as a store-and-forward historian substitute.** When a satellite link is down for six
  hours, backpressure becomes a design problem (drop? downsample? buffer?), and the answer is visible in
  the collector's own metrics.
- **The collector as the agent.** With no OneAgent or ActiveGate anywhere (D15), the collectors are the
  only software we deploy to the estate. If the topology works, it works because OTel worked.

**Honest limitation.** There is no Modbus receiver in OTel core or contrib. A small **device poller**
beside the edge collector speaks the device protocol and forwards OTLP; the collector remains the gateway
for everything northbound. Extracting that poller into a real `modbus` receiver is contribution candidate
§11.4, and is deliberately not attempted before M1–M2 have shown which transformations every integrator
actually writes by hand.

**Build order for this layer.** Before any domain simulator exists, prove the layering with the
off-the-shelf OTLP generator `telemetrygen` standing in for each site:
generator → edge collector → impaired link → hub collector → debug. That is M0, and it is deliberately
domain-free.

### 5.4 Network impairment (D11)

Timing/fault injection lives outside the simulator: an impairment proxy in front of each site's OTLP
egress, plus an HTTP fault-injection API on the world model so a scenario is one command:

```
make fault SITE=broken-hill FAULT=voltage-sag
make link  SITE=coober-pedy  PROFILE=degraded    # 600ms, 5% loss, 256 kbit/s
make link  SITE=coober-pedy  PROFILE=down        # hard outage, watch the queue grow
```

Tool choice to confirm in M0: toxiproxy (easy, cross-platform, per-proxy controls) vs `tc netem`
(no extra container, but requires privileged netns and is awkward on Docker Desktop for macOS). Leaning
toxiproxy for portability.

### 5.5 Time and quality (D12)

- Every OT reading carries **two timestamps**: when the device says it happened, and when the collector
  observed it. Sites in this network have no reliable NTP, so these diverge — deliberately.
- Every reading carries a **quality/validity** attribute. Polling protocols have no native notion of
  "this value is 30 minutes old because the link dropped"; that has to be attached.
- Sequence-of-events ordering is checked at the hub: if a remote site's clock has drifted, an event can
  appear before its own cause. This is a real OT problem and a strong teaching moment.

---

## 6. Semantic model (`energy.*`)

OTel semantic conventions have almost nothing for electrical or energy domains. We define a small,
reviewable registry rather than ad-hoc attribute names (D6), authored with **Weaver** so the simulated
telemetry can be validated against it in CI.

Entity hierarchy — note which identifiers are *real world* and therefore familiar to domain readers:

```
energy.grid (NEM / SWIS / isolated)
  └── energy.site                     id, nmi, dnsp, lat/lon, connection cap (kVA)
        ├── energy.transformer        kva_rating, vector_group, tap_position, winding/oil temp
        ├── energy.feeder             bay, protection group, voltage level
        ├── energy.meter (NMI)        phases, ct ratio, energy registers
        ├── energy.inverter (PV)      sunspec model, mppt strings, export limit
        ├── energy.bess               soc, soh, c-rate, round-trip efficiency
        ├── energy.genset             fuel level, run hours, output
        └── energy.evse               ocpp identity, connector count, max current
              └── energy.connector    plug type (CCS2/Type2), status, cable temp
                    └── energy.session  transaction id, EVSE id, energy delivered
                          └── energy.vehicle  (opaque id; SoC, requested vs delivered)
```

Open items to resolve before the registry is frozen (tracked in §11): attribute vs resource placement,
naming (`energy.*` vs proposing into `solar.*`/`grid.*`), and whether quality becomes an OTel-wide
concept (`otel.data_point.quality`) rather than an energy-local one.

---

## 7. Backend, visualisation and vendor neutrality (D2, D3)

Three layers, deliberately decoupled:

1. **Pipeline** — collectors, always present, always the same. Exporters are the only thing that changes.
2. **Visuals** — a national map of Australia (site markers, generation vs load, live state), drill-down to
   a per-site one-line mimic, plus trends and an ISA-18.2-shaped alarm list. All fed from the pipeline,
   never from the simulators and never via a vendor agent (D18). The map is the primary storytelling
   surface for video and blog.
3. **Real backend** — not shipped as a default. Documented recipes: a local light stack, Dynatrace, and
   "point it at anything that accepts OTLP".

```
make up                       # sims + edge collectors + hub + local visual stack
make up STACK=debug           # collectors only, debug exporter (CI / pipeline debugging)
make up STACK=dynatrace       # adds an OTLP exporter to your environment
```

**Dynatrace is first-class, but agent-free.** Reached as `otlphttp` from the hub to Dynatrace's native
OTLP ingest endpoint, with an API token from the environment. No OneAgent, no ActiveGate (D15). The repo
also documents how OT-only concepts (quality/staleness, dual timestamps, per-phase values) land in
Dynatrace, because no generic backend models them for you.

**Resolved (was open question 3):** the default `make up` ends in a **visual**, not in log output. A
first-run that is log output cannot serve the goal of this repo.

---

## 8. Resource budget (D10)

Target: whole default stack ≤3 GB RSS and ≤4 GB disk on M2/8 GB, arm64 only.

- Collectors: ~40–80 MB each. Four edge + one hub is fine; this is the cheap part.
- The expensive parts are backends (Loki/Tempo/Mimir/Grafana) and any JVM/DB pair (SteVe + MySQL).
  Both are therefore optional profiles.
- CSMS approach: a **minimal but protocol-real OCPP 1.6J/2.0.1 CSMS** as a test double in the site
  simulator (Python/Node, ~tens of MB) for the default path, with a "real CSMS" optional profile for
  anyone who wants it. The OCPP wire protocol is what matters for telemetry; the CSMS's storage engine
  is not. Revisit if this proves to undercut realism (D4).
- Aggressive image hygiene: slim/base images, one shared Python base for all simulators, no rebuilds per
  site (one image, parameterised by env — sites differ by config, not by code).

---

## 9. Milestones

The ordering is **thin end-to-end first, then thick**. The §4 narrative must be reachable early; a repo
about inspiration that only becomes inspiring at M5 is mis-sequenced.

| M | Deliverable | Exit criteria |
|---|---|---|
| **M0** | **Collector topology, proved** ✅ | Done: `make up` brings up two edge collectors + impaired link + hub, using `telemetrygen` as a stand-in source. All three signals cross the link; edge stamps site identity + geo; disk queue fills during an outage and drains after. 146 MiB for the whole stack, recorded in `docs/sizing.md`. No domain code. **Outstanding:** local visual stack choice. **Resolved in M1b** — chosen on measured RAM, see D24. |
| **M1** | **Generation + one load, roughly end to end** | **M1a ✅ (2026-10-14):** the weak-rural site carries real OT data — SunSpec/Modbus meter → poller → edge (names, units, sign, quality, dual timestamps) → impaired link → hub; a voltage sag runs end to end; deadband at the source. **M1b part 1 ✅ (2026-10-14):** the map of Australia and the per-site one-line mimic, both fed only from the pipeline; the local visual stack chosen on measured RAM (D24); two cardinality/memory bugs found and fixed on the way (D25, D26). **M1b part 2 ✅ (2026-10-15):** the solar site runs a real SunSpec/Modbus inverter (models 103/160/302/303/123) and the wind farm is a third, independently instrumented site (six turbines, its own edge collector, OT network and link profile); `telemetrygen` is gone from the default path and all three devices share one image. |
| **M2** | **Sessions as traces** ✅ | **Done (2026-10-15):** an OCPP 1.6J charge point and a CSMS test double run in the rural OT zone; a session is one trace (`ocpp.session` → authorize / start / meter_values / `energy.voltage_sag` / stop) with `reason=PowerLoss` after a real sag measured over Modbus; OCPP frames are logs carrying the session's `trace_id`; the rural mimic's EVSE01/02 boxes are live. `make spine` runs sag → derate → failed session in one command. The metric-exemplar leg is wired but **not carried by the chosen store** — measured, and recorded as D33. |
| **M3** | **Hard cases** | Metro depot + isolated microgrid; store-and-forward verified through a simulated multi-hour outage; clock skew makes an event appear before its cause at the hub. Still no OpAMP (D8). **Metro depot ✅ (2026-11-11):** a fourth site with twelve bays on one OCPP charge point over a 12 ms fibre link, twelve concurrent sessions, one trace each and no crossed transaction ids; the site meter's ~13 kW beside twelve 22 kW bays is recorded as a gap, not faked (`docs/standards.md` §6, findings #42). The EVSE series count is measured before and after in `docs/sizing.md`. |
| **M4** | **Breadth + registry** | Highway hub load management under a capacity cap (#12); full `energy.*` registry in place and validated (#9, blocked by the naming decision in #10); per-phase cardinality control measured (#11). |
| **M5** | **SLOs + scripted demo** | Alarms/SLOs — session success rate, time-to-first-power, delivered vs requested kWh, PQ event exposure (#13); `make demo` runs the whole story reproducibly (#14). |
| **M6** | **Polish + contribute back** | Public naming (#15); upstream contribution, one at a time under the rule of two (#16); OpAMP as an optional stretch. The README-walks-a-newcomer part of this row is done (#6, `docs/telemetry.md`). |

Not parallel where it matters: M0 before M1 (do not debug a domain simulator and a collector topology at
the same time), and M1 before M2 (traces without correct metric semantics produce pretty nonsense).

---

## 10. Repo layout

The real tree is in [`CONTRIBUTING.md`](CONTRIBUTING.md) §"Layout", and it is kept there because a
layout that drifts from the filesystem is worse than none. What belongs here is the rule and the
pieces that do **not** exist yet:

- One image for every device and every poller (`sim/`), parameterised by environment only (D10).
- One edge collector config for every site, one hub config (`collector/`).
- `schema/` (a Weaver registry) is **#9**; there is no `energy.*` registry published yet.
- Per-backend recipes (`collector/otlp-exporters/`) are **not** started; the swap point today is the
  two `VISUAL_*_ENDPOINT` variables and `docs/telemetry.md` §7.
- An alarm list in the HMI is **#13**; the mimic and the map are real.

---

## 11. Contribution candidates (D6)

**Tracked in #16.** One at a time, and only once it has hurt twice here (D6). The list stays
because it is the ranking argument, not the tracker.

Ranked by (gap size × our ability to demonstrate it), not by ease:

1. **OT data quality semantics.** Validity/staleness/comms-lost as a first-class property of a metric or
   data point, with an exemplar-quality path. Every OT protocol has this (DNP3 quality flags, OPC UA
   StatusCode); OTel does not. Highest-value gap in the whole project.
2. **Source-vs-receive time semantics.** A convention for "the device says it happened then, I observed it
   now" so that skewed remote clocks are representable rather than silently wrong.
3. **Energy/electrical semantic conventions.** The `energy.*` registry, published via Weaver, with units
   and per-phase structure that matches IEC practice (L1/L2/L3, pu, PF sign convention).
4. **An OT-facing collector receiver or connector** for a polling protocol (Modbus/SunSpec is the most
   reusable; OCPP is arguably better as a connector from logs/events). Only after M1–M2 shows exactly
   which transformations every integrator ends up writing by hand.
5. **Documentation**: "OT telemetry patterns" — deadband, store-and-forward, polling vs event, quality,
   time. The collector's existing capabilities solve most of these, but nobody has written the OT-shaped
   guide.
6. **A `deadband` / change-of-state processor.** Stateful, drops a data point unless the value moved
   more than a threshold since the last one. Every OT historian and every RTU has this; OTel contrib
   does not. Demonstrated by hand in `sim/poller/` at M1a.

Rule: nothing gets written upstream until it has been painful in this repo twice (rule of two).

---

## 12. Risks and rabbit holes

| Risk | Mitigation |
|---|---|
| Domain depth explodes (IEC 61850, DLMS/COSEM, ISO 15118, PMU/`C37.118`) | Explicit out-of-scope list (§2); `docs/standards.md` states fidelity honestly per protocol. |
| Backend sprawl eats the RAM budget | D10 is a hard gate; measure in M0 before choosing (D2 keeps backends swappable). |
| Simulator code becomes the project (more LOC than pipeline) | D5: reuse first; custom code is limited to the world model, the CSMS test double and the HMI. |
| "Realistic" quietly becomes "invented" | Every simulated value has a spec or a physical formula behind it, cited in `docs/standards.md`. |
| Notification/alarm flood makes the demo unwatchable | ISA-18.2-style alarm discipline is a design requirement for the HMI, not polish. |
| arm64 image gaps (some OT/CSMS images are x86-only or JVM+DB heavy) | M0 image audit; prefer Node/Python/Go; fall back to our own test double (D5). |
| Metric cardinality from 1s three-phase polling across N sites | Treated as a feature: the repo shows deadband, aggregation and cardinality control at the edge, with the numbers to prove it. |

---

## 13. Open questions

1. ~~Deliverable, vendor neutrality, electrical depth, custom code, scale, OpAMP, runtime~~ — answered, see §3.
2. ~~**Local stack**: Perses + Prometheus + Jaeger, or Grafana OSS? Decide in M0 on measured RAM (D10).
   Hard requirements: fed over OTLP from the hub only, and needs no vendor agent (D15, D18).~~
   **Resolved in M1b:** all three candidates were run and measured (80 / 162 / 276 MiB); RAM did not
decide it and the shape of the mimic did. Prometheus + Jaeger + our own `hmi/`, Perses optional.
   See D24 and `docs/sizing.md`.
3. ~~**`make up` default**: sims + edge collectors + hub + debug exporter only — agree? It makes the repo
   runnable on 8 GB with zero backend decisions, but the first-run experience is log output.~~
   **Resolved:** default ends in a visual (map + mimic), not log output; `STACK=debug` remains for CI.
   See §7.
4. **Electricity market realism** — live NEM spot price vs a static tariff table: **#22**.
5. **Registry naming** — keep `energy.*` or split `grid.*` / `solar.*` / `ev.*`: **#10** (blocks #9).
6. **Public naming of the whole project** — "virtual e-charging network" is descriptive, not
   memorable: **#15**.
   Not urgent; matters at M6 when people have to find it.
