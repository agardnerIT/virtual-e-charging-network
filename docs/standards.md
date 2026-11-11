# Standards: what each simulator claims, and how faithfully

> The rule this file exists for (plan.md §12): **"realistic" must not quietly become
> "invented"**. Every simulated value has a spec, a formula or a physical constant behind it,
> and where we implement a *subset* of a standard, this file says so rather than letting the
> reader assume conformance.

Three different things live in this file, and the difference matters because only one of them is
work:

- **out of scope by decision** — a protocol or behaviour this repo will not implement, with the
  decision that says so (OPC UA, DNP3, IEC 61850, ISO 15118 …).
- **deferred** — not implemented today, and a pull request implementing it faithfully would be
  welcome. Only these have issues, and there are exactly two: **#12** (`SetChargingProfile`, wanted
  because the M4 highway site needs a capacity cap) and **#23** (the register-level gaps: model 160
  scale factors, 302/303 points, `EvtVnd`, model 123's curves).
- **not claimed** — modelled, but explicitly not to the standard. Not a gap; a statement of what
  the numbers mean.

**This file is not a roadmap.** Every gap below carries its class, so a bare "not implemented"
does not read as a promise. If you want one of the *deferred* items, the two issues above are the
places to say so.

Fidelity is stated per device. "Subset" means: the points and semantics listed are real,
the byte layout follows the standard's conventions (integer registers, separate scale-factor
registers, 32-bit values across two registers with a vendor word order, a not-implemented
sentinel), but the model is **not** a complete implementation of the model number and will
not pass a SunSpec conformance test.

---

## 1. Southbound protocol: Modbus TCP

| | |
|---|---|
| Spec | MODBUS Application Protocol Specification V1.1b3 (Modbus.org) |
| Implemented | Function code 03 (read holding registers) only. TCP only (MBAP header, not RTU). |
| Client/server | Device simulator = server (`pymodbus` 3.16 `StartAsyncTcpServer`); poller = client. |
| Register base | 40000 (SunSpec convention); the image is 0-based from there. |
| Read size | The poller reads in chunks of ≤120 registers. Modbus caps a single read at 125 (0x7D) — a 187-register wind image is therefore three reads. |
| Not implemented (**out of scope**, D4) | Function codes 01/02/04/05/06/15/16; RTU/ASCII; serial; exceptions beyond "error". A read-only poller needs 03 and nothing else. |
| Honesty note | No timestamp and no quality flag exists anywhere in Modbus. Both are invented downstream, deliberately, and both are the point (plan.md D12, D21). |

### 2.0 The vocabulary is a registry, and it says what it is

`schema/registry/energy.yaml` is a [Weaver](https://github.com/open-telemetry/weaver) registry of the
semantic metric names, units, instruments and bounded attributes this stack emits — 94 metrics and
31 attributes, validated by `make registry-check` together with a two-way check against the emitters
([`schema/README.md`](../schema/README.md)).

Two things it deliberately is **not**: a claim of conformance to any published semantic convention,
and a claim of completeness. Every group is `stability: development`, the vocabulary is this repo's,
and publishing it upstream is issue #16 under the rule of two (D6).

## 2. SunSpec-shaped models

Spec: *SunSpec Modbus Specification* (SunSpec Alliance). Point names, scale-factor
conventions and the `Su/nS` start marker follow the standard. Model **lengths are our
subset's lengths**, which is exactly what a subset should report.

### 2.1 Model 1 — Common (subset)
`Mn`, `Md`, `Opt`, `Vr`, `SN`, `DA`. Strings are fixed-width register pairs. No `Model`
chain filtering is done by the poller; it reads the whole image and decodes every point.

### 2.2 Model 203 — Three-phase wye meter (subset) — rural site
`A`, `AphA/B/C`, `V`, `VphA/B/C`, `Hz`, `W`, `WphA/B/C`, `VA(+ph)`, `VAR(+ph)`,
`PF(+ph)`, `TotWhExp`, `TotWhImp`, `TmpCab`, and the `*_SF` registers for each.
Scale factors: A −1, V −1, Hz −2, W/VA/VAR 0, PF −3, TotWh −3, Tmp −1.
Not implemented (**out of scope**): the full 203 point set — THD, harmonics, demand registers,
events. A meter model is here to carry the sag and the energy register, not to be a power-quality
analyser.

### 2.3 Model 103 — Three-phase inverter (subset) — solar site
`A`, `AphA/B/C`, `PPVphAB/BC/CA`, `PhVphA/B/C`, `W`, `Hz`, `VA`, `VAr`, `PF`, `WH`,
`DCA`, `DCV`, `DCW`, `DCWH`, `TmpCab`, `TmpSnk`, `St`, `StVnd`, `Evt1`.
Scale factors: A 0 (1 A — a 4.2 MVA inverter at 690 V carries ~3.5 kA), V −1, Hz −2,
W/VA/VAR +3 (kW/kVA/kvar — 4 MW does not fit int16 at 1 W), PF −3, WH −1, Tmp −1.
`St` is the SunSpec 103 operating-state enum (0 off, 3 sleeping, 4 MPPT, 7 fault).
Implemented since #23: `EvtVnd` (vendor event bitfield, always 0 — the model does not invent vendor
faults) and `TmpTrns`. Not implemented (**out of scope**): the other control/curve models and event
history.

### 2.4 Model 160 — MPPT (subset, 2 repeating blocks) — solar site
`ID`, `DCA`, `DCV`, `DCW`, `Tmp` per block. The block **repeats** (`repeats=2`) and the
poller puts the block index on the wire as `modbus.register.instance`, so two trackers are
two data points of one metric, not two metric names.
Not implemented (**not claimed**, and stated rather than hidden): model 160's per-block
scale-factor registers — the points reference the
inverter model's `A_SF`/`V_SF`/`W_SF`/`Tmp_SF`, which in practice carry the same values.
This is the one place the subset is visible in the byte layout, and it is stated here.

### 2.5 Models 302 / 303 — Irradiance and temperature (subsets) — solar site
302: `GH` (global horizontal), `POA` (plane of array), `DFI` (diffuse horizontal), `DNI` (direct
normal), W/m². 303: `TmpBOM` (back of module), `TmpAmb`, `TmpTrns` (the step-up transformer).
Split into three components because the diffuse/direct split is the difference between an overcast
day (mostly diffuse, a tracker gets little) and a broken-cloud day (direct bursts) — not two more
readings of the same quantity. Not implemented (**deferred**, #23): optical cleanliness (`EC`).
Not implemented (**out of scope**): the per-point curve tables of 302's full model.

### 2.6 Model 123 — Immediate controls (2-point subset) — solar and wind
`WMaxLimPct` and `WMaxLimPct_SF`: the export limit as a percentage of nameplate AC.
This is the point a network operator actually writes. Not implemented (**out of scope**): the rest
of 123 — connection/ramp-rate controls, timers.

**Correction to #23's issue text:** ride-through is *not* in model 123. It is models **129 (VRT)**
and **130 (FRT)**, which this repo now carries as a **129 subset**: `VMinPct` (the voltage floor as
a percentage of nominal), `VMinTms` (how long the inverter must stay connected there) and `VrtSt`
(riding through / tripped). Those are the values a connection agreement sets and the ones an
engineer checks after a trip. The full model's repeating point table is **out of scope**, and this
model never trips — stated rather than implied.

### 2.7 Vendor model 64001 — device clock and provenance
SunSpec reserves 64000+ for vendor extensions. `UnixTime` (the device's own clock),
`ClockOffsetS`, `WordOrder` (the vendor 32-bit word order — a real 3am bug we reproduce),
plus `FaultCode` and `CurtailState`. Modbus carries no time, so a vendor clock register is
the only way a device can ever claim *when* a reading was taken.

## 3. Wind farm — vendor models, and why

**There is no widely-deployed SunSpec turbine model in the classic 100-series.** The models
the industry actually shipped are the inverter, meter and storage models. Rather than invent
a model number and imply it is standard, the wind farm uses vendor models that follow
SunSpec's register conventions:

- **64010 — Wind turbine, repeating (6 blocks).** `WTG_ID`, `WindSpeed`, `WindDir`,
  `RotorRpm`, `PitchAngle`, `W`, `VAr`, `WH`, `A`, `V`, `TmpNac`, `TmpGear`, `St`, `Avail`.
- **64011 — Wind farm connection point.** `FarmW`, `FarmVAr`, `FarmWH`, `Hz`, `TmpAmb`,
  `WindSpeedAvg`, `WindDirAvg`, `FarmSt`, `FarmAvail`.

The turbine `St` enum is ours and lives in this file: 0 idle, 1 starting, 2 producing,
3 curtailed, 7 fault. The farm aggregate is metered separately from the machines, as a real
connection point is, and differs from the machine sum by collector and transformer losses.

| Physics | Value | Source |
|---|---|---|
| Cut-in / rated / cut-out | 3 / 12 / 25 m/s | Typical 2.5 MW onshore class (IEC 61400-1 class II/III envelope) |
| Power curve below rated | cubic in wind speed | Standard aerodynamic approximation (P ∝ v³) |
| Rated power per turbine | 2.5 MW | Modelled machine |
| Farm rating | 15 MW (6 × 2.5) | Modelled farm |

## 4. Isolated microgrid — vendor models, and what is physics

Same rule as §3: **there is no SunSpec model for a grid-forming battery inverter with a
frequency reference**, and inventing a 1xx model number would be the one thing a domain reader
could not forgive. The microgrid's chain is vendor models that follow SunSpec's register
conventions:

- **64020 — Busbar / grid.** `Hz`, `V`, `RoCoF`, `GridFormingUnit` (which unit is the grid former).
- **64021 — BESS.** `SoC`, `W` (positive = discharge), `WH` (throughput), `MaxChargeW`,
  `MaxDischargeW`, `UsableWH`, `St`, `HoldReason`, `TmpCab`.
- **64022 — Source, repeating (2 blocks).** `SrcID` (1 solar, 2 wind), `W`, `AvailableW`, `Avail`.
  Repeats exactly like model 160 (MPPT) and the turbine model: one metric, one data point per
  source, with the source name attached by the edge collector.
- **64023 — Load.** `LoadW`, `EVW` (the EV block), `BalanceW` (generation − load, before the
  battery responded).

The two enums are ours and live here. BESS `St`: 0 idle, 1 charging, 2 discharging, 3 hold.
`HoldReason`: 0 none, 1 SoC high, 2 SoC low, 3 power limit.

### Modelled physics, with the equation

| What | Equation / value | Why it is defensible |
|---|---|---|
| Frequency dynamics | `dΔf/dt = (ΔP / S_base) · f_nom / (2H)`, H = 2 s, S_base = 1.5 MW | The swing equation. H = 2 s is deliberately low: an inverter-dominated island has almost no rotating inertia, which is the whole reason its frequency moves. |
| Grid-forming control | `P = (f_nom − f) / (f_nom · R) · S_base`, R = 2 % | Standard frequency droop. A grid-forming inverter responds to frequency, not to a schedule; that is what the term means. |
| Battery power limits | ±300 kW, ramped at 400 kW/`h` | An inverter cannot change power instantaneously. The ramp acts on the model's internal 20 Hz step, not on the poll rate. |
| State of charge | `SoC -= P·h / (η · kWh) · 100`, η = 0.95 one-way | Coulomb counting with a round-trip efficiency. |
| PV resource | `POA = 1050 · sin(elevation) · cloud`, derated −0.34 %/°C above 25 °C | Same irradiance and temperature model as the solar farm (§2.3). |
| Wind resource | Cube law between 3 m/s cut-in and 11.5 m/s rated | Same shape as the wind farm (§3). |
| Efficiency | 95 % round trip, 97–98.5 % inverter | Typical for LFP + a grid-forming inverter. |

### Invented outright, and labelled as such

- **The protection thresholds**: 49.4 Hz load shed, 50.35 Hz curtailment, and their ramp rates.
  Chosen so the event is visible on a 5 s metric cadence, *not* taken from any standard or from
  a real microgrid controller's settings.
- **The load cycle**: a 300 kW EV block, 90 s on, every 240 s. A demo schedule, not a forecast.
- **The 20 Hz internal integration step** (`sim/site/microgrid.py`). The swing equation is stiff
  — integrating it once per Modbus scan is numerically unstable and produces a model that
  oscillates between 46 and 55 Hz, which would be a property of the solver, not of a microgrid.
  The device therefore runs its own control loop faster than it is polled, like real equipment.
  This is the one place where the model's time step is a modelling decision worth stating.
- **The island's numbers**: 350 kWp PV, one 300 kW turbine, 300 kW / 500 kWh battery, 150 kW
  base load. A plausible small island, not a copy of any real installation.

### Not implemented at all (**out of scope** unless marked)

Protection relays, fault current and fault level, phase-angle and unbalanced operation,
harmonics, black start, islanding detection, reconnection synchronisation, transformer and
collector losses, an export market. A real islanded microgrid's hardest problem — supplying a
fault from inverter-based sources with no rotating machine behind it — is **not represented**.

## 5. Electrical reference numbers

| Quantity | Value | Standard |
|---|---|---|
| LV phase-neutral nominal | 230 V (rural), 398 V (generation, i.e. 690 V L-L) | AS 60038 |
| LV utilisation range | 0.94–1.10 pu | AS 60038 |
| Frequency | 50 Hz, 49.85–50.15 Hz normal band | AEMO / NEM |
| Pole-top / feeder voltage class | 22 kV | AS 60038 MV |
| Solar DC/AC ratio | 5300 kWp / 4200 kVA = 1.26 | Design choice, deliberately high enough to clip |
| Wind farm export limit | 15 MW | Modelled connection cap |
| Microgrid busbar | 400 V, 50 Hz nominal | AS 60038 LV, AEMO nominal frequency |
| Microgrid system base | 1.5 MVA, H = 2 s | Modelled island: low inertia by construction |
| Microgrid droop | 2 % | Typical grid-forming inverter droop setting |
| Frequency normal band | 49.85–50.15 Hz | AEMO / NEM normal operating band (drawn on the HMI trend) |

## 6. OCPP 1.6J (M2)

The charge point (`sim/ocpp/cp.py`) is a client; the CSMS (`sim/ocpp/csms.py`) is a test double. Both
speak the **JSON binding**, because 1.6S needs an XML stack for no benefit and 1.6J is what is deployed.

### Implemented, message by message

| Action | Direction | What is real |
|---|---|---|
| `BootNotification` | CP → CSMS | Real registration, real `Accepted` + `currentTime` + heartbeat interval. The CSMS keeps a charge-point registry. |
| `Heartbeat` | CP → CSMS | Answered with the CSMS clock. (The CP does not run the timer in this build; the exchange is implemented so a CSMS can demand it.) |
| `StatusNotification` | CP → CSMS | Per connector, for `Available` / `Preparing` / `Charging` / `Finishing`. `errorCode` is carried, always `NoError` today. Twelve connectors on the depot's CP means twelve of these per connection. |
| `Authorize` | CP → CSMS | Real `idTagInfo.status`; a configured deny-list returns `Blocked` and the session never starts. |
| `StartTransaction` | CP → CSMS | Real `transactionId` allocation, `meterStart` in Wh, `timestamp`, `idTagInfo`. A rejected idTag returns transaction 0. |
| `MeterValues` | CP → CSMS | Every 10 s with `Energy.Active.Import.Register` (Wh), `Power.Active.Import` (W), `Current.Offered` (A) and `Voltage` (V), each with `measurand`/`unit`/`context`. |
| `StopTransaction` | CP → CSMS | `meterStop`, `timestamp` and a real `reason`: `Local` or `PowerLoss`. |
| `DataTransfer` | CP → CSMS | Answered `Accepted`; no payload semantics claimed. |
| inbound CALL (any) | CSMS → CP | Answered `CALLERROR NotSupported`. No CSMS-initiated control is claimed. |

The frame codec (`ocpp16.py`) is shared by both ends, so the encoder and decoder cannot drift — the same
rule that makes `sunspec.py` shared by device and poller.

### Deliberately not claimed (**never**, unless an issue says otherwise)

- **Security profiles / TLS / client certificates** (**out of scope**). The WebSocket is plain `ws://`
  inside the OT zone; TLS would be the first thing to add if this ever left a laptop.
- **Smart charging** — charging profiles, `SetChargingProfile`, load balancing (**deferred: #12**). The
  derate is the charge point's own response to supply voltage, not an OCPP profile. This is the one
  gap on this list that an issue actually wants closed, because a site with a capacity cap is M4's
  site.
- **Firmware management, diagnostics, reservations, local auth lists, OCPI, ISO 15118 / Plug & Charge.**
- **Persistence.** The CSMS is in-memory by design (D34); a restart forgets transactions.
- **OCPP conformance.** This is a subset, labelled a subset, exercised by two charge points (2 and 12
  connectors) against a test double each.

The derate curve is ours, not OCPP's: at or above **0.94 pu** the EVSE offers nameplate (32 A); at or
below **0.80 pu** it offers nothing; in between it is linear. 0.94 pu is the AS 60038 lower utilisation
limit (216 V of 230 V); 0.80 pu is where a contactor drops out. Below 0.80 pu for **3 s** the transaction
ends for `PowerLoss`.

### One charge point, twelve connectors (M3-1)

The metro depot is **one charge point with twelve connectors**, not twelve charge points. That is
legal OCPP 1.6 — 1.6 is per-connector, and a power cabinet with twelve outlets is exactly the shape
it describes — and it is the shape that keeps the concurrency visible: twelve simultaneous
transactions share one WebSocket and one `uniqueId` counter, so a crossed transaction id or a shared
session context would show up in-process instead of being hidden by the network. What is **not**
claimed:

- **Multiple charge points per site.** A real depot usually has several CPs, each with one or two
  connectors, and a CSMS that knows them all. Twelve connectors on one CP is a fidelity trade for a
  single process, and it means the depot exercises the CSMS's registry far less than a real one would.
- **Load sharing between connectors.** Twelve bays drawing 22 kW each on one 400 V busbar is a load
  that would need a charging profile (`SetChargingProfile`) or a power-sharing cabinet's own control
  loop to be realistic. Neither is implemented; the offered current is a function of supply voltage
  only, per bay, independently.
- **A central CSMS.** There is one CSMS **per site**, in the site's own OT zone. A real CSMS is
  central, and a single one here would have to sit in both OT zones — which would undo the zone
  separation the repo exists to demonstrate. The instances are test doubles (D34) and hold
  transactions in memory.

### The site meter and the charge points are separate models

The depot's `M1` reads about 13 kW while its twelve bays draw ~260 kW, and the rural site's meter
reads ~14 kW beside one 22 kW bay. This is deliberate and it is a gap, not a measurement of the
chargers: **there is no power-flow model in the stack.** The site meter simulates a load of its own
(a building/HVAC load at a depot) and the charge point simulates what an EVSE offers; nothing sums
them, because nothing in this repo is a network simulator. A real depot meter would read the sum.

Read it the way a CPO would: the charge points are the metered, billing data (per transaction, from
the CP's own meter values), and M1 is the site's supply meter for its own load. Do not read the two
together as a balance. §7 lists this as not claimed, and `docs/findings.md` #42 records what it cost
to notice.

## 7. Things this repo does **not** claim

- IEC 61850 (MMS/GOOSE/SV), DNP3, IEC 60870-5-104, DLMS/COSEM, OPC UA — documented as
  mappings only (plan.md §2, D4).
- IEC 61400-25 (wind power plant communications) — not implemented (**out of scope**); the wind farm is
  Modbus/SunSpec-shaped, which is what D4 allows and what this file exists to say.
- SunSpec *conformance*. Subsets are labelled subsets.
- Real market settlement, real protection settings, real ISO 15118 / OCPP protocol depth.
- OCPP conformance of any kind — §6 lists what is implemented and what is not.
- A **power-flow model**. Site meters, inverters and charge points each simulate their own load or
generation; the meter does not sum the chargers, and no node balances (§6, findings #42).
- **A market simulation.** `energy.market.price` is a **recording replayed**, not a market model
  and not a live feed: `docs/measurements/market-prices-nsw1.csv` is a representative day (overnight
  trough, morning and evening peaks, a midday negative-price strip), and
  `energy.market.price.feed_age` exists precisely so a reader can see how old it is (about a month,
  since the recording is fixed). It is stamped with a site identity because the cost query joins on
  it, and that is the only sense in which it is "at" a site.
- **Protection, fault level or fault current** anywhere in the stack. Not at the microgrid
  (§4) and not at the rural site either: the "sag" is a multiplier on a voltage sample, not a
  network fault, and no protection relay is modelled.
