# Standards: what each simulator claims, and how faithfully

> The rule this file exists for (plan.md §12): **"realistic" must not quietly become
> "invented"**. Every simulated value has a spec, a formula or a physical constant behind it,
> and where we implement a *subset* of a standard, this file says so rather than letting the
> reader assume conformance.

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
| Not implemented | Function codes 01/02/04/05/06/15/16; RTU/ASCII; serial; exceptions beyond "error". |
| Honesty note | No timestamp and no quality flag exists anywhere in Modbus. Both are invented downstream, deliberately, and both are the point (plan.md D12, D21). |

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
Not implemented: the full 203 point set (THD, harmonics, demand registers, events).

### 2.3 Model 103 — Three-phase inverter (subset) — solar site
`A`, `AphA/B/C`, `PPVphAB/BC/CA`, `PhVphA/B/C`, `W`, `Hz`, `VA`, `VAr`, `PF`, `WH`,
`DCA`, `DCV`, `DCW`, `DCWH`, `TmpCab`, `TmpSnk`, `St`, `StVnd`, `Evt1`.
Scale factors: A 0 (1 A — a 4.2 MVA inverter at 690 V carries ~3.5 kA), V −1, Hz −2,
W/VA/VAR +3 (kW/kVA/kvar — 4 MW does not fit int16 at 1 W), PF −3, WH −1, Tmp −1.
`St` is the SunSpec 103 operating-state enum (0 off, 3 sleeping, 4 MPPT, 7 fault).
Not implemented: the control/curve models (123 is a 2-point subset below), event history,
the `EvtVnd`/`TmpTrns` points.

### 2.4 Model 160 — MPPT (subset, 2 repeating blocks) — solar site
`ID`, `DCA`, `DCV`, `DCW`, `Tmp` per block. The block **repeats** (`repeats=2`) and the
poller puts the block index on the wire as `modbus.register.instance`, so two trackers are
two data points of one metric, not two metric names.
Not implemented: model 160's per-block scale-factor registers — the points reference the
inverter model's `A_SF`/`V_SF`/`W_SF`/`Tmp_SF`, which in practice carry the same values.
This is the one place the subset is visible in the byte layout, and it is stated here.

### 2.5 Models 302 / 303 — Irradiance and temperature (subsets) — solar site
302: `GH` (global horizontal), `POA` (plane of array), W/m². 303: `TmpBOM` (back of module),
`TmpAmb`. Not implemented: diffuse/DNI/optical cleanliness (302), the other 303 temperature
points.

### 2.6 Model 123 — Immediate controls (2-point subset) — solar and wind
`WMaxLimPct` and `WMaxLimPct_SF`: the export limit as a percentage of nameplate AC.
This is the point a network operator actually writes. Not implemented: the rest of 123
(voltage/frequency ride-through curves, connection/ramp-rate controls, timers).

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

## 4. Electrical reference numbers

| Quantity | Value | Standard |
|---|---|---|
| LV phase-neutral nominal | 230 V (rural), 398 V (generation, i.e. 690 V L-L) | AS 60038 |
| LV utilisation range | 0.94–1.10 pu | AS 60038 |
| Frequency | 50 Hz, 49.85–50.15 Hz normal band | AEMO / NEM |
| Pole-top / feeder voltage class | 22 kV | AS 60038 MV |
| Solar DC/AC ratio | 5300 kWp / 4200 kVA = 1.26 | Design choice, deliberately high enough to clip |
| Wind farm export limit | 15 MW | Modelled connection cap |

## 5. Things this repo does **not** claim

- IEC 61850 (MMS/GOOSE/SV), DNP3, IEC 60870-5-104, DLMS/COSEM, OPC UA — documented as
  mappings only (plan.md §2, D4).
- IEC 61400-25 (wind power plant communications) — not implemented; the wind farm is
  Modbus/SunSpec-shaped, which is what D4 allows and what this file exists to say.
- SunSpec *conformance*. Subsets are labelled subsets.
- Real market settlement, real protection settings, real ISO 15118 / OCPP protocol depth.
