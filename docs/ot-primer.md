# OT & Electrical Primer (for people who came from IT)

> Companion to `plan.md`. Written for someone fluent in services, traces, latency and SLOs who has
> never had to think about a transformer, a Modbus register or a sequence of events.
> Where a number is standard-dependent I say so rather than quoting it from memory. Anything marked
> **[verify]** must be checked against the cited standard before it becomes a simulated value.

---

## 1. The one table that unlocks the whole domain

Everything below is detail. This is the shape of it:

| OT word | Expands to | Closest IT word | Notes |
|---|---|---|---|
| SCADA | Supervisory Control And Data Acquisition | fleet-wide monitoring/control platform | The "control plane" for physical plant |
| RTU / gateway | Remote Terminal Unit | edge agent / sidecar proxy | Sits next to equipment, speaks device protocols, forwards northbound |
| PLC | Programmable Logic Controller | controller / embedded runtime | Runs the actual control logic, in a deterministic loop |
| HMI | Human-Machine Interface | operator frontend | Not a rich web app — usually a mimic diagram + alarm list + trends |
| historian | *(not an acronym)* | time-series database | Purpose-built for long retention of numeric points |
| tag / point | *(not an acronym)* | metric name / timeseries | Named address in a device (`SITE1.TX1.TOP_OIL_TEMP`) |
| scan / poll interval | *(not an acronym)* | scrape interval | OT polls devices; devices rarely push |
| deadband | *(not an acronym)* | delta filter / change detection | Report only when the value moves more than X |
| **Sequence of Events** | SOE = Sequence Of Events | **distributed trace** | Timestamped ordered log of state changes at millisecond resolution |
| alarm | *(not an acronym)* | alert / event | Has a *priority* and an *acknowledge* state, not just a severity |
| quality / validity | *(not an acronym)* | data health / staleness flag | Every OT value can be Good / Stale / Bad / Comms-lost |
| mimic / one-line diagram | *(not an acronym)* | service map | Schematic of the plant, live values in place |
| interlock / protection | *(not an acronym)* | circuit breaker, bulkhead | Hard-wired refusal to do something dangerous |
| protocol gateway | *(not an acronym)* | protocol-translating proxy | Modbus ↔ DNP3 ↔ OPC UA ↔ (our case) OTLP |
| commissioning | *(not an acronym)* | deployment + configuration management | To "commission" a device is to install, configure and prove it |

Acronyms that appear elsewhere in this document are expanded in §10.

**The single most useful mapping for this project:**
`point values → metrics`, `sequence of events → traces`, `alarms → logs/events`.
OT has had those three shapes forever. OTel happens to have exactly three signals. Almost none of the
"OT needs its own telemetry stack" argument survives contact with that mapping — which is the point of
the repo.

---

## 2. Electricity in fifteen minutes

### 2.1 The four quantities, and the one people confuse

| Quantity | Symbol | Unit | IT-friendly framing |
|---|---|---|---|
| Voltage | V | volts | "pressure" — the *availability* of energy |
| Current | I | amps | "flow rate" — how much is moving right now |
| Power (real) | P | watts | instantaneous work: what you're billed for at the *rate* |
| Energy | E | watt-hours | accumulated work: what you're billed *for* |
| Reactive power | Q | var | power sloshing back and forth doing no work (motors, transformers) |
| Apparent power | S | VA | P and Q combined as a vector |
| Power factor | PF | (0–1) | P / S. Below ~0.9 means you're paying for capacity you don't use |

`P = V × I × PF` for single phase; `P = √3 × V_line_to_line × I_line × PF` for three-phase. If you
remember one thing: **kW is a rate, kWh is an amount.** A 150 kW charger running for 20 minutes delivers
50 kWh — and the customer cares about kWh while the network operator cares about kW.

`VA` vs `W` matters because cables, transformers and switchgear are sized in kVA (thermal limit), while
work delivered is in kW. A load at PF 0.8 needs 25% more current than the same kW at PF 1.0. This is why
charger and inverter datasheets always quote both.

### 2.2 Three-phase, and why it's everywhere

Distribution is three-phase: three conductors called **L1, L2, L3** (Australian practice also says
A/B/C or red/white/blue), each 230 V relative to neutral and 400 V relative to each other, 120° out of
phase. In Australia LV nominal is **230 V / 400 V at 50 Hz**, with a permissible voltage range of about
**+10% / −6% (≈216–253 V)** under AS 60038.

Consequences that generate most real-world telemetry:

- **Phase imbalance** — if one phase carries much more than the others, you get neutral current, extra
  heating, and voltage rise on the loaded phase. A badly-managed site full of single-phase chargers is a
  textbook cause. Expect per-phase current as a core metric everywhere in this repo.
- **Neutral and earthing faults** are the dangerous, hard-to-see failures. This is why EV charging has
  dedicated residual-current protection: 30 mA for shock protection, plus **6 mA DC** detection
  (IEC 62955 / "RDC-DD") because DC leakage can blind a standard Type A RCD.
- Voltage is measured *per phase* and *phase-to-phase*. Which one is being reported is a classic source
  of integrating the wrong number, so the semantic model has to state it explicitly.

### 2.3 Per-unit (pu) — the notation electrical engineers think in

Instead of "298 V", engineers often say "1.30 pu" (per-unit), meaning *value ÷ nominal*. Nominal is
1.0 pu. A **voltage sag** to 0.7 pu for 100 ms is the standard way to describe a dip. We adopt pu
alongside engineering units, because every PQ standard and protection setting is expressed that way and
translating to volts for the reader loses meaning.

Power quality event classes (IEEE 1159 is the usual reference) **[verify]**:

| Event | Definition | Typical cause |
|---|---|---|
| Sag / dip | 0.1–0.9 pu, 0.5 cycle to 1 minute | Faults elsewhere on the feeder, large motor/charger starting |
| Swell | 1.1–1.8 pu | Load rejection, phase-to-earth fault on an unfaulted phase |
| Interruption | < 0.1 pu | Protection operated, breaker opened |
| Transient / surge | very short, high magnitude | Switching, lightning |
| Harmonics / THD | distortion of the sine wave | Non-linear loads: rectifiers, which is *exactly* what a DC charger is |
| Flicker | rapid repetitive voltage change | Arc furnaces, welding — rarely chargers |

A DC fast charger is a big non-linear load that switches in 25–50 kW rectifier modules, so it is both a
*source* and a *victim* of power quality problems. That is why §4 of `plan.md` uses a sag as the
narrative driver: it's the most common and most relatable PQ event there is.

### 2.4 Energy counters are a trap

Energy is usually reported as a monotonically increasing register: "42,917 kWh delivered since
commissioning". Practical hazards, all of which we simulate because a real engineer will check:

- **Width and rollover**: 32-bit counters wrap. A kWh counter at 32 bits wraps after ~4.29 GWh; a Wh
  counter after ~4.29 MWh. Whether the wrap is handled by the device or the collector is a real
  integration decision.
- **Multi-register encoding**: 32/64-bit values span two or four 16-bit Modbus registers, and word order
  is vendor-dependent (big-word/little-word swaps are a classic 3 a.m. bug).
- **Scale factors**: raw registers are integers. SunSpec stores a *scale factor* register next to the
  value; other vendors hard-code multipliers in a manual. Unit conversion is genuinely part of the job.
- **Sign convention**: is positive power import or export? Charging or discharging? Direction is always
  a convention, and getting it wrong silently inverts a dashboard.

### 2.5 Frequency is the network's heartbeat

The grid's frequency (50 Hz in Australia) is a live measure of supply/demand balance. Deviation means
generation and load are out of step. Australia's NEM frequency operating standards define normal and
contingency bands (roughly 49.85–50.15 Hz normal, 49.5–50.5 Hz contingency) and staged automatic load
shedding beyond that **[verify]**. In an **isolated microgrid** (site archetype 4) frequency is not a
background constant: it droops when a genset is loaded and is the tightest constraint on everything.

### 2.6 Who owns what in Australia

- **AEMO** operates the **NEM** (National Electricity Market: QLD, NSW, ACT, VIC, SA, TAS). WA runs the
  SWIS, NT has its own small systems.
- **DNSPs** (distribution network service providers — AusNet, Energex, SA Power Networks, …) own the
  poles, wires and the connection point, and set **connection capacity** and **export limits**.
- The market dispatches every **5 minutes** and prices can go **negative** in the middle of the day when
  rooftop solar floods the grid. Negative price ≈ "we'll pay you to consume" — which makes cheap EV
  charging a genuinely rational behaviour, not a gimmick. Great demo material for site archetype 1.
- Every metering point has an **NMI** (National Metering Identifier) — a real, stable identifier used in
  settlement. We use it as our site/meter entity ID rather than inventing one.

---

## 3. Where EV charging fits

### 3.1 AC vs DC, and the four IEC 61851 modes

| Mode | What it is | Power | Our simulation |
|---|---|---|---|
| Mode 1 | Domestic socket, no communication | low | not used (unsafe for EV charging) |
| Mode 2 | Portable cable with in-cable control box | ~2–3.6 kW | home fallback |
| Mode 3 | Dedicated AC EVSE, pilot signalling, on-board charger does AC→DC | 7–22 kW | wallbox + AC posts |
| Mode 4 | DC charging, off-board rectifier, digital comms (ISO 15118) | 50–350 kW | DC fast charger |

An **AC "charger" is not a charger.** It's a smart switch with a pilot signal and a meter; the actual
charging electronics are in the vehicle. A **DC charger** really is a charger: AC in, rectified DC out,
straight to the battery, which is why it's expensive and why its internals are worth monitoring.

### 3.2 What's inside the hardware we'll simulate

**AC post / wallbox**: contactor, RCD/RCBO (+ 6 mA DC detection), pilot-signal generator (CP line),
proximity pin (PP) for cable current rating, lock actuator, energy meter, status LEDs, sometimes an RFID
reader. Telemetry: contactor state, per-phase current/voltage, delivered kWh, CP/PP state, temperature,
lock state, session state.

**DC charger**: input switchgear, 3–8 rectifier **power modules** (typically 25–50 kW each) switched in
and out by a controller to hit the requested current, DC bus, isolation monitoring, output contactors,
cable with cooling for >150 kW, and a controller running OCPP + ISO 15118. Telemetry: module-by-module
output, module temperature, DC voltage/current, insulation resistance, cable temperature, contactor
state, derate reasons. Module switching is why DC charger efficiency and output are steppy, not smooth —
an engineer will notice if we draw a straight line.

**The charge curve**: an EV battery charges at roughly constant current to ~80% SoC, then constant
voltage with a tapering current to 100%. It also derates when cold or hot (thermal management), and
above ~80% SoC the car, not the charger, is the bottleneck. So "delivered kW" legitimately falling below
"requested kW" is normal *and* diagnostic: it's how you distinguish a healthy car-side taper from a
charger-side fault.

### 3.3 OCPP, and why sessions become traces

**OCPP** (Open Charge Point Protocol) is the JSON-over-WebSocket protocol between a charge point and a
**CSMS** (central system). It is the closest thing OT has to a REST/RPC API, and it is fundamentally
**transactional**:

```
BootNotification → StatusNotification → Heartbeat (loop)
Authorize → StartTransaction → MeterValues (periodic) → StopTransaction
              + RemoteStart/Stop, SetChargingProfile, ChangeAvailability, FirmwareStatusNotification
```

OCPP 1.6J is the deployed base; 2.0.1 adds a typed device/variable model, richer transaction events,
security events and ISO 15118 certificate handling. Because a session has a beginning, an end, ordered
intermediate events and failure modes, **a charging session is a trace**. That mapping (§1) is the
hinge of this project.

**Smart Charging** is where OCPP meets the electrical plane: the CSMS sends *charging profiles*
(`ChargePointMaxProfile`, `TxDefaultProfile`, `TxProfile`) with a stack level and a schedule of power or
current limits, and the charge point must obey the most restrictive applicable one. That is exactly how a
site stays under its DNSP connection limit, and it's how we tie a grid constraint to a per-session
outcome. **OCPI** is the separate roaming protocol *between* operators — CPO vs eMSP — and matters only
for the metadata story (who the driver is, which network they're roaming on).

### 3.4 Behind the meter

**Solar PV**: panels → DC strings → **MPPT** inputs on an inverter → AC. Realistic telemetry is DC
voltage/current/power per string, AC V/I/P/f per phase, energy today/total, inverter temperature, status
and fault codes, plus **irradiance** (W/m²) and panel temperature if a weather station exists. Inverters
are usually **oversized relative to the panels** (DC/AC ratio ~1.2), so the inverter *clips* at its rated
output on a good day — a flat top on the production curve is correct, not a bug.

**SunSpec** is the interoperability standard that matters here: a convention of standardised Modbus
register maps for solar equipment (common model, inverter models ~101/103, MPPT ~160, meter ~201, newer
700-series) **[verify model numbers]**. If you're going to build one realistic Modbus device, a SunSpec
inverter is the highest-value choice, because the register semantics are public and an electrical
engineer will recognise them.

**Export limits**: an inverter may not export more than the DNSP allows (a common single-phase figure is
5 kW under AS/NZS 4777.2 and network rules) **[verify]**. Modern inverters accept dynamic limits and
**curtail** when told. The control paths for this in Australia are **AS/NZS 4755** (demand-response modes
signalled by the network) and **CSIP-AUS**, the Australian profile of **IEEE 2030.5**, used for
DER (distributed energy resource) control including curtailment **[verify details]**. This is the
cleanest example in the whole domain of a *network constraint* driving *device behaviour* driving
*telemetry* — i.e. the sort of thing that belongs on one dashboard.

**Battery (BESS)**: SoC, SoH, charge/discharge rate as a C-rate (1C = full charge in one hour), round-trip
efficiency (~90%), temperature, cycle count, inverter limits, and grid-export behaviour. In an isolated
microgrid, the BESS plus a diesel genset is what stands in for the grid.

**Tariffs and optimisation**: time-of-use windows, feed-in tariffs, negative spot prices, and "charge on
solar" logic (divert PV surplus into the car instead of exporting it at a low or negative price). This is
where behind-the-meter telemetry stops being a hobby dashboard and becomes commerce.

---

## 4. Protocols, mapped to things IT people already know

| Protocol | IT analogue | Transport | Has timestamps? | Has quality? | Events? | What the collector must add |
|---|---|---|---|---|---|---|
| **Modbus RTU/TCP** (Modbus is a protocol name, not an acronym; RTU = Remote Terminal Unit, TCP = Transmission Control Protocol) | an untyped register file, like reading `/proc` by offset | Serial RS-485 (master/slave) or TCP | ✗ | ✗ | ✗ (poll only) | everything: timestamps, units, scaling, sign, quality, names |
| **SunSpec** (a standards alliance and its specifications, not an acronym) | a schema laid over that register file | Modbus | ✗ | ✗ | ✗ | same, but names/semantics are standardised |
| **DNP3** (Distributed Network Protocol 3, IEEE 1815) | SNMP with guarantees: events, classes, integrity polls | Serial or TCP | ✓ (48-bit ms) | ✓ (flags: comms-lost, restart, forced, over-range) | ✓ (class 1/2/3, unsolicited, event buffer) | mapping DNP3 quality → OTel; store-and-forward for the event buffer |
| **IEC 60870-5-104** (IEC = International Electrotechnical Commission; -104 is the TCP/IP profile) | DNP3's European sibling | TCP | ✓ | ✓ (IV invalid, NT *not topical*, SB substituted, BL blocked) | ✓ | same as DNP3; `NT` maps directly to our staleness attribute |
| **IEC 61850 MMS** (Manufacturing Message Specification) | a typed object model + RPC (closest OT gets to a real API) | TCP | ✓ | ✓ | ✓ (reporting, buffered reports) | mostly translation; needs precise time |
| **IEC 61850 GOOSE / SV** (Generic Object Oriented Substation Event / Sampled Values) | L2 multicast pub/sub for hard real-time; sampled-value streaming | Ethernet L2 | ✓ | — | ✓ | real-time only; out of scope, but it's why time sync exists |
| **OPC UA** (Open Platform Communications Unified Architecture) | gRPC + a schema registry, per value | TCP / PubSub | ✓ | ✓ (StatusCode per value) | ✓ (subscriptions) | an OPC UA receiver exists in contrib — check it in M0 |
| **DLMS/COSEM** (Device Language Message Specification / Companion Specification for Energy Metering), IEC 62056 | a metering object model using OBIS codes as paths | Serial/TCP | sometimes | ✓ | sometimes | keep OBIS identifiers intact; they're the meter engineer's names |
| **SNMP** (Simple Network Management Protocol) | SNMP (unusually, the same thing) | UDP | ✗ | ✗ | ✓ traps | available in contrib already |
| **OCPP 1.6J / 2.0.1** (Open Charge Point Protocol; the J is JSON over WebSocket) | WebSocket RPC with a session lifecycle | WebSocket/TLS | ✓ | ✗ | ✓ (it *is* events) | session→trace assembly, idempotency, ordering |
| **OCPI** (Open Charge Point Interface) | B2B REST API between companies | HTTPS | ✓ | — | — | metadata enrichment only |

Acronyms used elsewhere in this document are expanded in §10.

Two things worth noticing as an observability person:

1. **Modbus is the worst case and the most common.** No timestamps, no quality, no events, no units, no
   names. Every integrator in the world writes the same fixup layer by hand. That's a gap worth filling
   once (see `plan.md` §11).
2. **The disagreement about quality** is the deepest one. DNP3 and IEC 104 have quality flags; OPC UA has
   StatusCode; Modbus has nothing. OTel has no concept of a data point that is *present but not
   trustworthy*, which is the normal condition of a remote site with a flaky link. This is our
   strongest contribution candidate (D12).

---

## 5. Why OT telemetry is genuinely harder (not just different vocabulary)

1. **Time is unreliable.** Substation clocks are disciplined by PTP/IEEE 1588 or IRIG-B where it matters;
   everything else drifts on SNTP or nothing. A remote site with a two-minute clock error makes an
   ordered event list a lie. Hence the dual-timestamp rule (D12) and sequence checking at the hub.
2. **Connectivity is intermittent.** OT assumes links fail: event buffers, retries, store-and-forward,
   and "is the value stale?" are first-class. This is the difference between `plan.md` §5.3 and every
   "collector as a passthrough" demo.
3. **Polling is a tax.** A collector polling 500 registers every second generates load on RS-485 buses
   and 1980s CPUs. Deadbands, scan classes and change-of-state reporting exist for that reason — and
   they are also the answer to metric cardinality. Same problem, two names.
4. **Units and scaling are per-device.** There is no universal schema. The fixup layer is unavoidable.
5. **Criticality inverts blame.** In IT a missing metric is an observability bug. In OT, an operator
   action based on a stale reading can destroy equipment. Data quality isn't polish; it's safety.

---

### 5.1 The clock problem, with a worked example

Every other point in the list above can be argued about. This one cannot: **a sequence-of-events list
sorted by device time is wrong**, and the only question is how wrong.

Two clocks produce every timestamp in this repo:

- the **device clock** — the meter's own `UnixTime` register. No NTP at a remote site, so it drifts,
  or somebody sets it by hand. It is the only clock that was present when the electrons moved.
- the **receive clock** — when the collector saw the value. This one is on the same host as everything
  else, is monotonic in practice, and is what a store should sort by.

The rural site's meter is deliberately 7 s behind and the island's is 11 s behind, which is enough to
make the difference visible. It is not enough to break anything, so there is a runtime control:

```bash
make clock-skew SITE=rural SECONDS=240     # the meter now claims it is 4 minutes in the future
./tools/clock-inversion.sh                 # the whole demonstration, in one command
make clock-reset SITE=rural
```

`tools/clock-inversion.sh` pushes the clock, runs the one fault this repo has (a voltage sag that ends
a session for `PowerLoss`), and prints the *same* two events in both orderings:

```
ORDERED BY RECEIVE TIME
  14:22:41.485  sag begins (the cause)
  14:22:44.492  session ends for PowerLoss (the effect)   +3.0 s later

ORDERED BY DEVICE TIME
  14:22:44.492  session ends for PowerLoss (the effect)
  14:26:41.000  sag begins (the cause)                     +236.5 s
```

The cause arrives **four minutes after its effect**. Nothing was rewritten to make that happen: the
sag's timestamp comes from the meter that measured it, the stop's comes from the charge point that
decided it, and the two clocks disagree. That is not a bug in the pipeline — it is two facts from two
clocks, and it is what any OT estate looks like.

**Where each clock rides, in this stack.** The device clock is provenance, so it rides where
provenance belongs: on the charge point's `energy.voltage_sag` and `ocpp.stop_transaction` spans
(`energy.supply.device.timestamp`), and on the poller's log records. It is deliberately **not** a
metric attribute any more: the OTel SDK keys aggregation state by attribute set and never evicts, so
a timestamp per data point per poll leaked half a megabyte a minute per poller (D51, findings #57).
The bounded metric between the two clocks is `modbus_device_clock_error_seconds`, and the receive
time is the store's sample time.

**What a backend should do, and this repo does:**

1. **Order by receive time.** It is the only clock that is comparable across sources. In this stack
   that is free: Prometheus stores a metric sample at the time the hub received it, and a span's start
   time is its producer's clock read at emit time.
2. **Keep device time as provenance, never as the sort key.** `energy.device.timestamp` rides the data
   point and `energy.supply.device.timestamp` rides the trace (D12, D23). Reading it back tells you
   what the device believed; sorting by it reorders the world.
3. **Never rewrite it.** A corrected timestamp is a lost fact, and the correction is not reproducible
   after the device is fixed or replaced. If you need a corrected view, compute it *as a query*, with
   the raw value still underneath.
4. **Do not treat a skewed clock as `comms-lost`.** The quality vocabulary has four words and they mean
   different things (D21): `comms-lost` is "I cannot read the device"; `stale` is "the device's own
   clock says this reading is old"; `bad` is "the device says this point has no value"; `good` is
   everything else. A meter 5 minutes **behind** therefore makes every point report `stale` while the
   link is perfect and the values are seconds old — measured: 28 series carrying `stale` while
   `make queues` stayed at 0, and the charge point went on charging at 0.99 pu. A meter 4 minutes
   **ahead** reports `good` on everything and puts the timestamps in the future, which is the dangerous
   direction because nothing complains.

If you are building an alarm on top of this, the rule from finding #46 generalises: **"no recent
samples" is a delivery question, `quality` is a device question, and neither of them is a time
question.** They are three different failures that the same word — "stale" — gets used for.

---

## 6. What an electrical engineer expects to see on screen

If the HMI doesn't look like this, domain readers will discount everything else:

- **A mimic / one-line diagram** — the plant as a schematic (grid → transformer → busbar → feeder →
  EVSE → connector), with live values *in place* and colour for state. This is the thing they'd recognise
  as an HMI, and it's what our proof-of-life graphic should be.
- **An alarm list** with priority, timestamp, acknowledge state and a first-out indicator — not a
  severity-coloured log stream. Alarm discipline (ISA-18.2) exists to stop alarm floods; a demo that
  produces 400 alarms on one sag is not convincing.
- **A sequence-of-events (SOE) list** at millisecond resolution — chronological ground truth for
  post-mortem ("breaker opened 12 ms after the sag started"). This is our trace.
- **Trends** with engineering units on the axis, correct scaling, and gaps shown as gaps rather than
  interpolated straight lines. Stale data drawn as live data is the cardinal sin.
- **Engineering tag names**, ideally following a naming convention (IEC 81346 or ISA-5.1 style sailing
  prefixes like `TX1`, `CB2`, `EVSE04`) — not `thing_0042`.

That list is the acceptance criteria for the HMI milestone and for the semantic model.

---

## 7. Vocabulary to be able to use correctly

Transformer kVA rating, vector group, OLTC/tap position, winding and oil temperature, DGA (dissolved gas
analysis) · feeder, bay, busbar, protection group, overcurrent (50/51), earth fault (50N/51N), distance
(21), recloser · MCB, MCCB, RCD/RCBO, RDC-DD · sag/swell/interruption, THD, flicker, unbalance,
per-unit · SoC, SoH, C-rate, round-trip efficiency · SoC taper, CV/CC, MPPT, DC/AC ratio, clipping,
curtailment, export limit, zero-export · NMI, DNSP, NEM, FCAS, spot price, tariff window · CPO, eMSP,
EVSE, connector, transaction, smart charging profile, stack level · commissioning, setpoint, interlock,
permissive, first-out alarm.

---

## 8. Standards and sources to lean on

Grouped by what we need them for. Treat every one of these as "confirm before implementing" —
they are the correctness bar for D4, and `docs/standards.md` will record, per protocol, exactly how
faithful our simulation claims to be.

- **Grid and power quality**: AS 60038 (voltage), IEEE 1159 (PQ events), IEC 61000-4-30 (measurement
  class A/S, 10/12-cycle aggregation), AS/NZS 61000.3.x (harmonics planning levels), AEMO frequency
  operating standards.
- **Behind the meter**: AS/NZS 4777.2 (inverter connection, export limits, DRM), AS/NZS 4755
  (demand response), CSIP-AUS / IEEE 2030.5 (DER control), NEM12/NEM13 (metering data files).
- **Charging hardware**: IEC 61851 (modes, pilot signalling), IEC 62196 (connectors: Type 2, CCS2),
  IEC 62955 (6 mA DC residual current), ISO 15118 (Plug & Charge, V2G).
- **Charging software**: OCPP 1.6 and 2.0.1 specifications, OCPI, SunSpec Modbus specifications.
- **OT protocols**: Modbus Application Protocol (and the Modbus/TCP MBAP framing), DNP3 IEEE 1815,
  IEC 60870-5-101/104, IEC 61850, IEC 62056 (DLMS/COSEM with OBIS), OPC UA.
- **Operator practice**: ISA-18.2 (alarm management), IEC 81346 (reference designations / tag naming).

---

## 9. Learning order, if you want to go deeper yourself

1. **`P = V × I × PF`, kW vs kWh, three-phase, per-unit.** Everything else is a consequence. (1–2 hours)
2. **Read the OCPP 1.6 session lifecycle end to end.** You already know RPC and sessions; this is the
   fastest domain win and it produces our first traces. (half a day)
3. **Read the DNP3 or IEC 104 point/quality model.** It is the clearest statement of everything OTel
   lacks for OT — quality flags, event classes, integrity polls. (half a day)
4. **Read a SunSpec inverter register map** next to a real inverter's datasheet. Watch how much
   translation sits between the register and the value an engineer reads. (a few hours)
5. **Read one paper on voltage sags in distribution networks** to understand why the §4 narrative is
   physically plausible rather than a convenient plot device. (an hour)
6. Only then touch IEC 61850. It is a large standard, it is out of scope here, and it will eat a week
   without producing anything the repo needs.

The point of learning in that order is that steps 1–4 each map onto a milestone in `plan.md`, so the
domain knowledge arrives exactly when the code needs it.

---

## 10. Acronym glossary

§1 and §4 expand the acronyms that appear in their own tables. The rest of the document's acronyms are
here, grouped by where you'd meet them. A few entries are present precisely because people assume they
are acronyms and they aren't.

**Quantities and units**

| Acronym | Expands to |
|---|---|
| P / Q / S | real (W) / reactive (var) / apparent (VA) power |
| E | energy (Wh, kWh) — the integral of P over time |
| PF | power factor, P ÷ S |
| pu | per-unit: the value divided by nominal, so nominal is 1.0 pu |
| SoC | state of charge |
| SoH | state of health |
| C-rate | charge or discharge rate relative to capacity; 1C empties a full battery in one hour |
| THD | total harmonic distortion |
| PQ | power quality |

**Grid, market and metering**

| Acronym | Expands to |
|---|---|
| AEMO | Australian Energy Market Operator |
| NEM | National Electricity Market (QLD, NSW, ACT, VIC, SA, TAS) |
| SWIS | South West Interconnected System — Western Australia's grid |
| DNSP | distribution network service provider — owns the poles, wires and connection point |
| NMI | National Metering Identifier |
| FCAS | Frequency Control Ancillary Services |
| DER | distributed energy resource — rooftop solar, batteries, controllable loads |
| CSIP-AUS | Common Smart Inverter Profile — Australia, the Australian profile of IEEE 2030.5 |
| HV / MV / LV | high / medium / low voltage |
| CT | current transformer — where a meter's scaling ratios come from |
| PTP | Precision Time Protocol, IEEE 1588 |
| IRIG-B | Inter-Range Instrumentation Group time code B — the older substation time distribution |
| SNTP | Simple Network Time Protocol |

**Electrical equipment and protection**

| Acronym | Expands to |
|---|---|
| OLTC | on-load tap changer — how a transformer adjusts its output voltage |
| DGA | dissolved gas analysis — early fault detection in oil-filled transformers |
| MCB | miniature circuit breaker |
| MCCB | moulded-case circuit breaker |
| RCD | residual current device |
| RCBO | residual current breaker with overcurrent protection — an RCD and MCB in one |
| RDC-DD | residual direct current detecting device — the 6 mA DC protection in IEC 62955 |
| MPPT | maximum power point tracker |
| PV | photovoltaic |
| BESS | battery energy storage system |
| CC / CV | constant current / constant voltage — the two phases of a battery charge curve |

**Charging and mobility**

| Acronym | Expands to |
|---|---|
| EVSE | electric vehicle supply equipment — what people loosely call "the charger" |
| CSMS | charging station management system — OCPP's "central system" |
| CPO | charge point operator — owns and runs the charge points |
| eMSP | e-mobility service provider — the brand the driver holds a contract with |
| CP | control pilot — the signalling line between EVSE and vehicle |
| PP | proximity pilot — the cable current-rating line |
| CCS2 | Combined Charging System, Type 2 variant — the DC connector used in Australia |
| RFID | radio-frequency identification — tap-card authorisation |
| ISO 15118 | road vehicles, vehicle-to-grid communication interface — Plug & Charge and V2G |
| IEC 61851 | electric vehicle conductive charging system — defines Modes 1–4 |
| IEC 62196 | plugs, socket-outlets and vehicle connectors — Type 2 and CCS2 |

**Standards bodies and operator practice**

| Acronym | Expands to |
|---|---|
| IEC | International Electrotechnical Commission |
| IEEE | Institute of Electrical and Electronics Engineers |
| ISO | International Organization for Standardization |
| AS / AS/NZS | Australian Standard / Australian–New Zealand Standard |
| ISA | International Society of Automation — ISA-18.2 (alarm management), ISA-5.1 (instrument symbols) |
| IEC 81346 | the reference-designation standard that structures tags like `TX1` and `CB2` |

**Telemetry and protocols**

| Acronym | Expands to |
|---|---|
| OTel | OpenTelemetry |
| OTLP | OpenTelemetry Protocol |
| OT / IT | operational technology / information technology |
| TLS | Transport Layer Security |
| RS-485 | Recommended Standard 485 — the serial bus Modbus RTU runs over |
| MBAP | Modbus Application Protocol — the header that wraps Modbus in TCP |
| OBIS | OBject Identification System — the code scheme for metering objects |

**Not acronyms, despite appearances:** **Modbus** (a protocol name, originally from Modicon), **SunSpec**
(the alliance and its specifications), **historian** and **deadband** (ordinary words that happen to be
OT jargon).
