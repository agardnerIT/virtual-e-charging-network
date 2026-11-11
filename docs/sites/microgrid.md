# The isolated microgrid — the site where frequency moves

`microgrid-kangaroo-island` is the fifth site and the only one with **no feeder behind it**.
Solar, one wind turbine and a battery on a busbar, and nothing else. There is no NEM to hold
50 Hz and no upstream network to explain a sag, so the site's signature signal is
**frequency**, not voltage.

That contrast is the point (plan.md D17): the same event — load exceeds generation — produces
a voltage sag at the rural site (long feeder, high source impedance) and a frequency excursion
here (no grid, no inertia). Two sites, two different measurements, one pipeline.

```
    PV1 (3-phase inverter)  ─┐
                             ├── BUSBAR 400 V ──┬── SITE LOAD (incl. the EV block)
    WTG1 (one turbine)    ───┤                  │
                             │                  │
    BESS1 ── grid-forming ───┘                  └── (nothing: an island has no export)
```

## What is different from the other four sites

| | rural / depot | solar / wind | microgrid |
|---|---|---|---|
| Regime | load on a feeder | generation onto a feeder | island |
| Who sets the voltage | the network | the network | the battery inverter |
| Who sets the frequency | the NEM | the NEM | the battery inverter |
| Signature event | voltage sag | curtailment / clipping | frequency excursion + battery at limit |
| Primary metric | `energy.meter.voltage` | `energy.inverter.power.real` | `energy.microgrid.frequency` |

The site also has the **worst clock** of the five (11 s behind): an island out of range of any
time source is exactly where a device clock is wrong by tens of seconds, which is
[#4](https://github.com/agardnerIT/virtual-e-charging-network/issues/4)'s territory.

## The model, in the words an engineer would use

All of it lives in `sim/site/microgrid.py`, one Modbus device with a vendor register chain
(`sim/sunspec.py`, models 64020–64023). No OCPP and no charge point: a microgrid controller
sees chargers as **load**, so the EV block is `LoadW`/`EVW` in the register file rather than a
protocol peer. A bay-level view of the microgrid would add nothing that the rural site and the
depot do not already demonstrate.

**Droop, not scheduling.** The battery does not follow a power setpoint; it responds to
frequency, `P = (f_nom − f) / (f_nom · R) · S_base` with R = 2 %. That is what grid-forming
means in this model, and it is why a load step **settles at an offset** instead of returning
to exactly 50.00 Hz. Typical offset at this site: 0.05–0.25 Hz.

**The swing equation, with low inertia.** `dΔf/dt = (ΔP / S_base) · f_nom / (2H)`, H = 2 s,
S_base = 1.5 MW. A NEM-connected synchronous fleet runs at H ≈ 5–6 s with thousands of MW
behind it; an inverter-dominated island does not, which is why a 300 kW step moves frequency
here and would not move the NEM at all.

**State of charge integrates what the battery actually did**, with a 95 % one-way efficiency.
Its power limits are hard. At the SoC floor it stops discharging, frequency falls, and at
49.4 Hz the controller sheds the EV block. At the ceiling the battery stops charging, surplus
pushes frequency up, and at 50.35 Hz both sources are curtailed. `energy.bess.hold_reason`
says which limit is in play — that is the "hold when the grid former is at limit" the issue
asked for, and it is a metric, not prose.

**The load steps on its own.** A 300 kW block of EV charging starts every four minutes and
runs for 90 s, so `make prom Q='energy_microgrid_frequency_hertz'` has something to show
without anyone typing a command. The step is also available on demand:

```bash
make microgrid-status            # busbar frequency, RoCoF, BESS power/SoC/hold, sources
make step KW=300 DURATION=90     # +300 kW for 90 s (an operational event, not a fault)
make step-clear
docker compose -f compose/docker-compose.yml exec -T device-microgrid \
  python site/faultctl.py status
```

`make step` is deliberately **not** a fault. One fault mechanism only (D29), and it is the
voltage sag at the rural site. A load step is something that happens to a microgrid every day.

## Why the frequency does not fall over when the battery saturates

Two protections, modelled because without them the model is unphysical:

- **Under-frequency load shedding** at 49.4 Hz, proportional and ramped, on the EV block only.
- **Over-frequency curtailment** at 50.35 Hz, applied to *every* source (at 3 a.m. the wind is
  the only thing making power, so a controller that can only spill solar would let an island
  climb until something tripped).

The thresholds are **chosen to be visible on a 5 s metric cadence**, not taken from any
standard. `docs/standards.md` §4 says so, next to the rest of the fidelity disclosures.

## The measured step

`tools/microgrid-step-measure.sh` records a step from the store: frequency, battery power and
SoC, the load and the hold reason, every 2 s. A recorded run is in `docs/measurements/`
(`microgrid-step-20261111T140954Z.csv`), and the numbers were, for a 300 kW step held for 50 s
through a steady demand of 174 kW:

| | before the step | during the step |
|---|---|---|
| site load | 174 kW | **450 kW** (the EV block, 300 kW of it) |
| busbar frequency | 49.98–50.00 Hz | **49.70–49.90 Hz** |
| battery power | +12 to +37 kW (drifting) | **+253 to +300 kW** — at its limit in 2 of 22 samples |
| renewables | 137–197 kW | 138–300 kW (wind moving) |
| state of charge | 70.2 % | 69.7 % (0.5 % in 90 s, ~6 kWh from a 500 kWh pack) |
| `hold_reason` | 0 none | 0 none (it touched the limit without sitting on it) |

Frequency span across the run: **0.30 Hz**, from a load that nearly tripled.

Read it as an operator would: the load tripled, the busbar moved by 0.16 Hz, and the battery
did the work. Now short the battery out of the model — a 300 kW step on a 1.5 MW island with
H = 2 s is about 1 Hz/s of RoCoF — and the same event drops frequency through the shed
threshold in under a second. That difference *is* the grid-forming service, and it is why
`energy.microgrid.rocof` is the metric to watch on this site.

## What the HMI draws

`hmi/app.js` → `renderMicrogridMimic`. Sources on the left, busbar across the middle, the
battery below the busbar with a **heavier outline** and the words "grid-forming", the load on
the right, and the unimplemented breaker dashed and labelled. The trend panel switches from
voltage to frequency and bands it at the NEM's normal operating range (49.85–50.15 Hz).
The distinction the issue asked for is drawn, not annotated: a reader can see which asset
decides the frequency.

## Not claimed

- No protection relay model, no fault current, no phase-angle or unbalanced model, no harmonics.
  A real island's hardest problem — fault level supplied by inverters — is not represented at all.
- No transformer, no connection point, no export market. Surplus is curtailed, full stop.
- The 4-minute load cycle is a demo schedule, not a load forecast.
- No black-start sequence, no islanding detection, no reconnection synchronisation.
