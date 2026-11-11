# The generation sites — solar and wind

Both generation sites are real Modbus devices on the **same image and same poller** as the rural
meter — they differ only by environment (plan.md D10). They exist as a pair because solar and wind
are different *regimes*, and the contrast is measurable rather than decorative.

Which SunSpec models each device implements, and how faithful each subset is, is in
[`docs/standards.md`](../standards.md) §2 and §3. This file is the story of the two regimes.

## Solar / Mildura — a SunSpec model-103 inverter

The device carries a **model-103 inverter** with two **MPPT trackers** (maximum-power-point
trackers, model 160), an **irradiance and panel-temperature station** (models 302/303) and an
**export limit** (model 123).

It is deliberately oversized against its inverter — **DC/AC = 1.26** (5 300 kWp of panels against
4 200 kVA of inverter) — so on a clear day it walks **clips**: the flat top of the AC curve is a
design choice, not a bug. Cell temperature derates it, so a hot clear day produces less than a cool
one.

```bash
make prom Q='energy_inverter_power_real_watts{energy_site_id="solar-mildura"}'   # clipped flat top
make up SOLAR_CLOCK_HOUR=12        # force a daytime curve for a demo that runs at night
make curtail SITE=solar PCT=60     # operator curtailment, distinct from inverter clipping
make curtail SITE=solar PCT=100    # clear
```

`SOLAR_CLOCK_HOUR` exists because a solar farm at 2am is correctly asleep — honest, but not a demo.
Operator curtailment and inverter clipping are different facts, so they have different controls.

**Modelled, not measured:** the physics. Irradiance follows a solar-geometry curve, cell temperature
derates the DC output, and the inverter clips at nameplate AC. The register map, the scale factors
and the protocol are real. The numbers a reader might check — cut-in, DC/AC ratio, model lengths —
are listed in `docs/standards.md` §2.

## Wind / Snowtown — six turbines and a connection point

Six turbines are expressed as repeating SunSpec-shaped **vendor** blocks (model 64010, one block per
machine) plus a farm connection point (model 64011). There is no widely deployed SunSpec turbine
model in the 100-series, so inventing a model number would have implied conformance that does not
exist — see plan.md D27 and `docs/standards.md` §3.

Power goes as the cube of wind speed between cut-in and rated (3 and 12 m/s), so a single machine
swings hard while the farm total stays comparatively steady — the opposite shape to solar — and it
generates at 2am when the solar farm is correctly asleep.

```bash
make prom Q='energy_turbine_power_real_watts{energy_site_id="wind-snowtown"}'    # six disagree
make curtail SITE=wind PCT=0       # full curtailment
```

The wind model is a mean-reverting random walk, not a free one: the first cut wandered to its 2 m/s
clamp and the farm idled for minutes (`docs/findings.md` #21). Sensor noise is gated by the physical
driver, so a dark solar farm produces exactly zero rather than a few noisy kW (#22).

**Modelled, not measured:** the wind resource and the aerodynamic curve. The cube law, cut-in/rated/
cut-out and per-turbine rating are classic approximations, tabulated in `docs/standards.md` §3 with
their source. The farm connection point meters the farm separately from the machines and differs from
the machine sum by collector and transformer losses, as a real one does.

## What the pair is for

The map needs "power station" anchors at both ends of the country, and the pipeline needs two
generation shapes that a single dashboard has to survive: a smooth, clipped midday plateau and six
noisy machines that disagree with each other. Both are drawn as their own one-line mimic — arrays →
MPPT trackers → inverter → step-up → grid, and six turbines on a collector bus → step-up → grid. See
[`docs/hmi.md`](../hmi.md).

## Since #23: the diffuse/direct split, the transformer, and ride-through

Three additions that make the farm's curves mean more than "it is sunny":

- **`energy.pv.irradiance.diffuse` and `.direct`** (SunSpec 302 `DFI`/`DNI`). Global horizontal alone
  cannot tell an overcast day from a broken-cloud one: the first is mostly diffuse (a tracker gets
  little), the second is direct bursts (a tracker gets everything for a minute). Two metric names,
  because they are two resources.
- **`energy.pv.transformer_temperature`** (model 303 `TmpTrns`). The step-up transformer is the
  slowest thermal time constant on a site and the thing that derates a farm on a hot still afternoon
  — a different failure from an inverter heatsink, and it was simply not modelled before.
- **The ride-through values** (`energy.inverter.ride_through_voltage`, `_time`, `_state`, model 129
  subset). Not a behaviour — a value: the voltage floor the inverter must stay connected down to, and
  for how long. This model never trips, and says so.

`make sunspec-check` asserts every one of them survives encode → decode within one count of its scale
factor, under both vendor word orders.
