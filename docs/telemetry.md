# What you should see

This page answers three questions from someone who has just run `make up` and knows
observability but not this repo:

1. **What sites are there, and what are they?**
2. **What telemetry comes out** — metrics, traces, logs, the collectors' own numbers — and how do I
   split it to build a chart?
3. **Where are the collector configs**, and how do I point all of it at *my* backend?

Everything below was checked against the running stack, not read off the plan. Where a number is
measured, the query that produces it is given so you can re-measure.

---

## 1. The sites, spelled out

Six sites. Three are **loads** (they consume electricity — sites with cars charging), two are
**generators** (they produce it), and one is an **island** (it makes its own frequency). All six run
the same images and the same collector config; they differ only by environment variables and by what
is plugged into the site network (plan.md D10).

| Site id | Name in the data (`energy.site.name`) | `energy.site.kind` | What it physically is | Devices at the site | Bays | WAN link |
|---|---|---|---|---|---|---|
| `rural` | Broken Hill EV Charging Site | `charging_site` | A small charging site on the weak end of a rural feeder. Voltage sags when a load switches on. | SunSpec meter + a 2-bay OCPP charge point + a CSMS | 2 | 4G, impaired (toxiproxy) |
| `solar-mildura` | Mildura Solar Farm | `solar_farm` | A utility-scale solar farm. | SunSpec 103 inverter with 2 MPPT trackers, irradiance and temperature sensors | — | impaired |
| `wind-snowtown` | Snowtown Wind Farm | `wind_farm` | Six wind turbines plus the farm's connection point (metered separately from the machines). | One SunSpec-shaped farm controller exposing 6 turbines | — | impaired |
| `metro-tottenham` | Tottenham Metro Depot | `charging_site` | A bus/van depot: twelve bays charging at once on one charge point. | SunSpec meter + a 12-bay OCPP charge point + its own CSMS | 12 | 12 ms fibre + jitter |
| `highway-hume` | Hume Highway Service Centre | `charging_site` | A motorway service centre: six bays on a 100 kW supply, with a site capacity cap pushed as an OCPP `ChargePointMaxProfile`. | SunSpec meter + a 6-bay OCPP charge point + its own CSMS | 6 | impaired |
| `microgrid-kangaroo-island` | Kangaroo Island Microgrid | `microgrid` | An island: solar, one wind turbine and a battery, with no feeder and no NEM behind it. Frequency is set by the battery inverter, not by the grid. | One SunSpec-shaped microgrid controller (busbar, BESS, 2 sources, load) | — (EV load modelled) | impaired |

Read that table once and the rest of the repo makes sense. Two things surprise people:

- **`rural` and `metro-tottenham` have the same kind.** `energy.site.kind` distinguishes *regimes*
  (charging site vs solar farm vs wind farm), not sizes. The depot is a charging site with twelve
  bays and 260 kW of load; the rural site is a charging site with two bays. Split by
  `energy.site.name` when you want the individual site, by `energy.site.kind` when you want the
  regime. (#1 built the depot precisely so the second instance would audit the first.)
- **The generators have no bays, and the charging sites have no turbines.** So the metric families
  in §3.3 are site-shaped: `energy.meter.*` at the three feeder-connected sites, `energy.evse.*` /
  `energy.session.*` only at the two charging sites, `energy.inverter.*`/`energy.pv.*` only at
  Mildura, `energy.turbine.*` / `energy.windfarm.*` only at Snowtown, and
  `energy.microgrid.*`/`energy.bess.*` only on the island. A chart that queries all six sites at
  once will show gaps, and that is correct rather than broken.
- **The island is the odd one out on purpose.** It has no `energy.meter.*` at all: there is no
  feeder meter because there is no feeder. Its primary signal is
  `energy_microgrid_frequency_hertz`, which moves because a grid-forming battery inverter sets it
  (D17, `docs/sites/microgrid.md`).

Site identity is not set by the simulators. The **edge collector** stamps all five site attributes
(id, name, kind, latitude, longitude) on every signal on the way out, so every backend and every view
can place the data without knowing anything about the site (D20, `collector/edge/site.yaml`).

The series count is a moving quantity and quoting it as a single number is how the earlier
version of this page got it wrong twice. It moves because of live sessions (two attributes —
`energy.session.id`, `ocpp.transaction.id` — are per-transaction), because a restarted collector
mints new series, and because a site whose link is down keeps its series in the store's lookback for
five minutes after the data stops. So count only series that reported recently:

```
make prom Q='count(last_over_time({__name__=~"energy_.*"}[45s]))'
make prom Q='count by (energy_site_name) (last_over_time({__name__=~"energy_.*"}[45s]))'
```

The shape, per site, is stable: **six series per charging bay** (voltage, current, power, state,
session energy, offered current), **~90 at Snowtown** (6 turbines × 13 signals plus farm-level),
**~45 at Mildura** (inverter + irradiance + the ride-through registers), **~27 on the island**
(23 families, of which the four source families carry two data points each), and **two market
series** (price and feed age, stamped with the rural site's identity because the cost query joins on
it). Measured totals for the current six-site stack are in `docs/sizing.md`.

`make prom Q='count({__name__=~"energy_.*"})'` — without the `last_over_time` — also works, and
includes the residue: it is the query that produced the wrong numbers, which is why it is the second
one mentioned here rather than the first.

`docs/sizing.md` has the before/after tables that isolate the cost of the twelve bays, and of a
whole site, from that churn.

---

## 2. The path a number takes, and who owns what

```
device (Modbus TCP)  ->  poller  ->  edge collector  ->  impaired WAN  ->  hub  ->  stores
                       (OT zone)     (OT zone)          (toxiproxy)      (NOC)     (NOC)
```

Each hop owns exactly one job, and this is the part that makes the data trustworthy:

| Hop | Authors | Deliberately does not |
|---|---|---|
| Poller (`sim/poller/poller.py`) | Reads Modbus registers, scales them, applies deadband, emits **device-shaped** names (`modbus.register.vpha`), the device's own clock reading, and a quality word (§3.4) | Know what "voltage" means, know the site, add units |
| Edge collector (`collector/edge/site.yaml`) | Site identity, semantic names, UCUM units, descriptions, phase A/B/C → L1/L2/L3, `otel.data_point.quality`, the receive timestamp, the disk-backed queue | Talk to any backend |
| WAN (`impairment/link.sh` → toxiproxy) | Delay, loss, bandwidth, outage — see §8 | Anything semantic |
| Hub collector (`collector/hub/hub.yaml`) | Fan-out, and the cardinality rules for the visual store (§7) | Touch the data on the backend-of-record path |

The OCPP side is the same shape with different emitters: the charge point (`sim/ocpp/cp.py`) and the
CSMS (`sim/ocpp/csms.py`) are applications, so they emit semantic names directly; they still let the
edge collector stamp the site, and they still set `energy.quality` for the edge to convert.

---

## 3. Metrics

### 3.1 Names: written with dots, stored with underscores

OTel metric names are dotted (`energy.meter.voltage`). Prometheus mangles them: dots become
underscores and the UCUM unit becomes a suffix, so `energy.meter.voltage` with unit `V` lands as
`energy_meter_voltage_volts`. If you point at a different backend you will see the dotted name; in
Prometheus you see this:

| OTel name + unit | In Prometheus |
|---|---|
| `energy.meter.voltage` (V) | `energy_meter_voltage_volts` |
| `energy.meter.power.real` (W) | `energy_meter_power_real_watts` |
| `energy.meter.energy` (kW.h) | `energy_meter_energy_kW_h` |
| `energy.meter.power.apparent` (VA) | `energy_meter_power_apparent_VA` (case is kept) |
| `energy.evse.state` (1) | `energy_evse_state_ratio` |
| `energy.inverter.export_limit` (%) | `energy_inverter_export_limit_percent` |
| `energy.pv.irradiance` (W/m2) | `energy_pv_irradiance_watts_per_m2` |
| `energy.turbine.rotor_speed` (/min) | `energy_turbine_rotor_speed_per_min` |
| `energy.turbine.wind_direction` (deg) | `energy_turbine_wind_direction_deg` |
| `energy.meter.temperature` (Cel) | `energy_meter_temperature_celsius` |

The full mapping table also lives in `POINTS` in `hmi/app.js`, which is the one place the mimic
declares what it draws — and, machine-readably, in [`schema/registry/energy.yaml`](../schema/README.md):
a Weaver registry of every metric name, unit, instrument and bounded attribute this stack emits.
`make registry-check` validates it *and* asserts it agrees with the emitters in both directions, so
a new metric cannot land in a collector without also landing in the vocabulary.

### 3.2 The dimensions you can split on

Everything below is a **data point attribute**, which Prometheus turns into a label. They are
bounded — a fixed vocabulary or a fixed count — so none of them mints a series per poll (that trap is
D25 and `docs/findings.md` #9).

| Label in Prometheus | OTel attribute | Values seen | Present on | Use it to |
|---|---|---|---|---|
| `energy_site_name` | `energy.site.name` | the four names above | everything | split by site |
| `energy_site_id` | `energy.site.id` | `rural`, `solar-mildura`, `wind-snowtown`, `metro-tottenham`, `microgrid-kangaroo-island` | everything | split by site, stable key |
| `energy_site_kind` | `energy.site.kind` | `charging_site`, `solar_farm`, `wind_farm`, `microgrid` | everything | split by regime |
| `geo_latitude`, `geo_longitude` | `geo.*` | one per site | everything | place it on a map |
| `otel_data_point_quality` | `otel.data_point.quality` | `good`, `stale`, `bad`, `comms-lost` | everything | filter out or count untrustworthy values (§3.4) |
| `energy_phase` | `energy.phase` | `L1`, `L2`, `L3` | meter and inverter voltage/current/power | per-phase charts; find an unbalanced or single-phased sag |
| `energy_direction` | `energy.direction` | `import`, `export` | `energy.meter.energy` (and inverter/farm energy where a direction applies) | separate consumption from export |
| `energy_evse_id` | `energy.evse.id` | `EVSE01` … `EVSE12` | `energy.evse.*`, `energy.session.*` | one line per bay |
| `energy_connector_id` | `energy.connector.id` | `1` … `12` | same | the OCPP connector number |
| `energy_evse_state_text` | `energy.evse.state.text` | `Available`, `Preparing`, `Charging`, `Finishing`, `Faulted`, `Unavailable` | `energy.evse.state` | a bay-state chart without decoding the enum |
| `ocpp_transaction_id` | `ocpp.transaction.id` | int, per charge point | `energy.session.energy` | join a metric point to a session trace |
| `energy_session_id` | `energy.session.id` | 12-hex | `energy.session.energy` | join to the trace's root span; **unbounded by design** (one per session) |
| `energy_mppt` | `energy.mppt` | `1`, `2` | `energy.inverter.mppt.*` | per-tracker charts |
| `energy_turbine` | `energy.turbine` | `1` … `6` | `energy.turbine.*` | per-machine charts |
| `energy_source_name` | `energy.source.name` | `solar`, `wind` | `energy.microgrid.source.*` | one line per source on the island |
| `energy_bess_status_text`, `energy_bess_hold_reason_text` | `energy.bess.status.text`, `energy.bess.hold_reason.text` | `idle`/`charging`/`discharging`/`hold`, `none`/`soc-high`/`soc-low`/`power-limit` | `energy.bess.status`, `energy.bess.hold_reason` | why the grid former is (or is not) doing something |
| `energy_microgrid_grid_forming_source` | `energy.microgrid.grid_forming_source` | `bess` | `energy.microgrid.grid_forming_unit` | which asset is the grid former |
| `energy_irradiance_plane` | `energy.irradiance.plane` | `horizontal`, `plane_of_array` | `energy.pv.irradiance` | GH vs POA |
| `energy_inverter_status_text`, `energy_inverter_curtailment_text`, `energy_turbine_status_text`, `energy_windfarm_status_text`, `energy_windfarm_curtailment_text`, `energy_inverter_fault_code_text` | `…status.text`, `…curtailment.text` | see the descriptions | the corresponding enum metric | read state without a lookup table |
| `energy_point` | `energy.point` | SunSpec point name, e.g. `VphA`, `W`, `TmpCab` | Modbus-derived metrics | trace a value back to its register |
| `modbus_register_address`, `modbus_register_scale_factor` | same | register address, scale | Modbus-derived metrics | provenance: which register, what scaling |
| `job`, `instance` | `service.namespace/service.name`, `service.instance.id` | `vecn/modbus-poller`, `vecn/vecn-ocpp-cp` | everything | provenance, and one-off `instance` churn after a restart (#12) |

Three payload values were **shed at the Prometheus boundary on purpose** (D25):
`energy.receive.timestamp`, `energy.device.timestamp`, `modbus.register.raw`. Since D51 two of them
are no longer emitted as data point attributes at all: a per-sample attribute is a memory leak in
the SDK before it is a cardinality problem in the store (findings #57), so the device clock rides on
spans and logs and the metric path carries `modbus_device_clock_error_seconds` — one bounded value
per device. The hub still sheds the names, for data from older collectors. They are payload, not
dimensions — keeping them in Prometheus mints a new series on every poll (measured: 4 532 series in
15 minutes from one meter). They survive on the backend-of-record path and in traces/logs. So in
Prometheus you do **not** get the device clock; use `modbus_device_clock_offset_seconds` (§3.5) or a
trace.

### 3.2.1 If you do not want the per-phase series

39 % of the active `energy_*` series exist only to carry a phase label (measured: 84 of 213). The
visual store can be told to drop them, leaving the device's own aggregate under the same metric
name:

```bash
make up VISUAL_PHASES=drop     # the dashboard store gets the aggregate; the backend of record keeps both
```

That is a hub-side `filter` on the visual pipeline only — no averaging, no invented values,
nothing removed from the backend-of-record path (`docs/sizing.md` §"Per-phase cardinality"). Default
is `keep`, so nothing changes unless you ask.

### 3.3 The catalogue, by device family

**Every site** — the site meter. 14 series (the energy register has two directions, voltage has
phases):

| Metric (Prometheus) | Unit | Split by | What it is |
|---|---|---|---|
| `energy_meter_voltage_volts` | V | `energy_phase` | Phase-to-neutral voltage. The sag lives here. |
| `energy_meter_current_amperes` | A | `energy_phase` | Line current. |
| `energy_meter_frequency_hertz` | Hz | — | 50 Hz nominal; NEM normal band ~49.85–50.15. |
| `energy_meter_power_real_watts` | W | `energy_phase` (where per-phase) | Positive = import. |
| `energy_meter_power_apparent_VA` | VA | `energy_phase` | Sizing quantity for cables/switchgear. |
| `energy_meter_power_reactive_var` | var | `energy_phase` | Positive = inductive. |
| `energy_meter_power_factor_ratio` | 1 | `energy_phase` | Unsigned; sign of var carries lead/lag. |
| `energy_meter_energy_kW_h` | kW.h | `energy_direction` | Cumulative register, import and export. |
| `energy_meter_temperature_celsius` | Cel | — | Meter enclosure. |

**The two charging sites** — charge point and sessions. Six series per bay, plus four
session-scoped SLO instruments (§3.7):

| Metric | Unit | Split by | What it is |
|---|---|---|---|
| `energy_evse_voltage_volts` | V | `energy_phase`, `energy_evse_id`, `energy_connector_id` | Supply voltage **as the charger reads it over Modbus** — the reason a sag becomes a derate. |
| `energy_evse_current_offered_amperes` | A | evse, connector | Offered current. Falls linearly between 0.94 pu and 0.80 pu. |
| `energy_evse_power_real_watts` | W | evse, connector | Delivered power. |
| `energy_evse_state_ratio` | 1 | evse, connector | OCPP status enum; `energy_evse_state_text` is the readable form. |
| `energy_session_energy_kW_h` | kW.h | evse, `ocpp_transaction_id`, `energy_session_id` | One series **per live transaction**; ends when the session does. |

`energy.session.*` is OCPP-side state, not a device register: the charge point emits it directly.
`ocpp_transactions_active_ratio` and `ocpp_transactions_total_ratio` come from the CSMS.

**The market (M4, #22)** — two series, and the only signal here that is not a measurement of
anything physical:

| Metric (Prometheus) | Unit | Split by | What it is |
|---|---|---|---|
| `energy_market_price_AUD_per_MW_h` | AUD/MWh | `energy_market_region` | NEM-style spot price, **recorded and replayed** in phase with the time of day. Not a live feed, not a market model. |
| `energy_market_price_feed_age_seconds` | s | region | How old the replayed price is against the wall clock. A recorded feed is not a live one, and this is the number that says so (~27 days, because the recording is fixed). |

The point is not the price: it is that a **cost is derivable from telemetry**. The HMI does exactly
that on the site page — session energy delivered × the spot price — and labels the price as
recorded in the same breath:

```
sum(energy_session_energy_kW_h{energy_site_id="rural-brokenhill"}) * on() group_left()
  max(energy_market_price_AUD_per_MW_h) / 1000
```

**The island (`microgrid-kangaroo-island`)** — busbar, battery, sources, load. 27 series:

| Metric (Prometheus) | Unit | Split by | What it is |
|---|---|---|---|
| `energy_microgrid_frequency_hertz` | Hz | — | **The site's primary signal.** Set by the grid-forming battery, not by the NEM. Moves with the load/generation imbalance; a load step settles at a droop offset instead of returning to 50.00. |
| `energy_microgrid_rocof_hertz_per_second` | Hz/s | — | Rate of change of frequency. Low inertia is the signature of an inverter-based island. |
| `energy_microgrid_voltage_volts` | V | — | Busbar voltage, held by the grid former — it dips far less under load than the rural feeder does. |
| `energy_microgrid_grid_forming_unit_ratio` | 1 | — | Which unit is the grid former. It is data, not a footnote, so a chart can label the asset. |
| `energy_bess_state_of_charge_percent` | % | — | Battery state of charge, integrated from what the battery actually did. |
| `energy_bess_power_real_watts` | W | — | **Positive = discharge** into the busbar, negative = charging from surplus. |
| `energy_bess_energy_kW_h` | kW.h | — | Lifetime throughput (charge + discharge). |
| `energy_bess_power_max_charge_watts`, `energy_bess_power_max_discharge_watts` | W | — | Nameplate limits. The interesting question is how close the battery is to them. |
| `energy_bess_status_ratio` | 1 | — | 0 idle, 1 charging, 2 discharging, 3 hold. `energy_bess_status_text` is the readable form. |
| `energy_bess_hold_reason_ratio` | 1 | — | 0 none, 1 SoC high, 2 SoC low, 3 power limit — *why* the grid former stopped doing what the busbar asked. |
| `energy_microgrid_source_power_real_watts` | W | `energy_source_name` | Delivered power per source (solar, wind). |
| `energy_microgrid_source_power_available_watts` | W | `energy_source_name` | What that source *could* make. The gap to the delivered value is curtailment. |
| `energy_microgrid_source_availability_ratio` | 1 | `energy_source_name` | Availability flag per source. |
| `energy_microgrid_source_id_ratio` | 1 | `energy_source_name` | 1 solar, 2 wind — the device's own block id. |
| `energy_microgrid_load_power_real_watts` | W | — | Site load including EV charging. |
| `energy_microgrid_load_power_ev_watts` | W | — | The EV block alone: the load step the grid former has to survive. |
| `energy_microgrid_load_power_balance_watts` | W | — | Generation − load *before* the battery responded. Positive is surplus. |

The whole island is **one** Modbus device polled by **one** poller through **one** edge collector,
which is the same shape as every other site: the difference is what the register file contains, not
how the telemetry moves.

**Mildura** — inverter, 32 series. Since #23 the family also carries the irradiance split
(`energy_pv_irradiance_diffuse_watts_per_m2`, `..._direct_...`), the step-up transformer
temperature (`energy_pv_transformer_temperature_celsius`) and the ride-through values from model
129 (`energy_inverter_ride_through_voltage_percent`, `..._time_milliseconds`,
`energy_inverter_ride_through_state_ratio`). AC side (`current`, `voltage`, `voltage_line`, `frequency`,
`power.real`, `power.apparent`, `power.reactive`, `power_factor`, `energy`), DC side (`dc.current`,
`dc.voltage`, `dc.power`, `dc.energy`), per-tracker (`mppt.current`, `mppt.voltage`, `mppt.power`,
`mppt.temperature` with `energy_mppt`), temperatures (`temperature.cabinet`,
`temperature.heatsink`), status (`status`, `status_vendor`, `event`, `fault_code`, `curtailment`,
`export_limit`), and the PV environment (`pv.irradiance` with `energy_irradiance_plane`,
`pv.module_temperature`, `pv.ambient_temperature`). Units are in §3.1 and in the collector's
descriptions; every metric the edge renames gets a one-line description that a backend can show.

**Snowtown** — 13 signals × 6 turbines, plus 12 farm-level signals:

| Per turbine (`energy_turbine`, 1–6) | Farm connection point (`energy_windfarm`) |
|---|---|
| `power.real`, `power.reactive`, `energy` | `power.real`, `power.reactive`, `energy`, `frequency` |
| `current`, `voltage` | `ambient_temperature`, `wind_speed`, `wind_direction` |
| `wind_speed`, `wind_direction`, `rotor_speed`, `pitch_angle` | `status`, `availability` |
| `temperature.nacelle`, `temperature.gearbox` | `export_limit`, `curtailment`, `fault_code` |
| `status`, `availability` | |

### 3.4 Quality: the attribute that says "present but not trustworthy"

`otel.data_point.quality` is authored at the edge from the quality word the poller or charge point
sent, and carries four values:

| Value | Means | When it is set |
|---|---|---|
| `good` | The device answered and the value is current | normal operation — nearly every series, nearly always |
| `stale` | The device answered, but the device's own clock says the reading is old | device clock lag beyond the poller's staleness window |
| `bad` | The register exists but has no valid value | decode failure / sentinel register |
| `comms-lost` | The last value is retained, but the device stopped answering | device container stopped, Modbus timeout |

This is the repo's central workaround: OTel has no native status for a data point, every OT protocol
does (DNP3 flags, IEC 104 quality bits, OPC UA StatusCode). It is a data point attribute so that it
travels with the value — `energy_meter_voltage_volts{otel_data_point_quality="comms-lost"}` is a real
series.

**A WAN outage does not change quality.** Quality describes the device reading at the site; the WAN
failing pauses *delivery*, so the site's queue grows and your store sees nothing new — see §8. To see
`comms-lost`, stop the device, not the link:

```bash
docker compose -f compose/docker-compose.yml stop device-rural
make prom Q='energy_meter_voltage_volts{energy_site_id="rural"}'
```

### 3.5 Two extra numbers that are not device signals

| Metric | What it is |
|---|---|
| `modbus_device_clock_offset_seconds` | The device's clock minus the poller's, per device. This is the clock-skew story (issue #4, D12/D23) before it reaches a trace. |
| `modbus_device_register_word_order_ratio` | Which word order the poller negotiated for 32-bit registers. Provenance for every float. |

### 3.6 Charts to build in the first ten minutes

```promql
# Load and generation on one axis, split by the physical site
sum by (energy_site_name) (energy_meter_power_real_watts)

# ...or by regime, which is the coarser and more interesting split
sum by (energy_site_kind) (energy_meter_power_real_watts)

# Per-phase voltage at one site: the sag is visible here, and only on the phases it hits
energy_meter_voltage_volts{energy_site_id="rural"}

# Every bay at the depot, one line each, charging or not
energy_evse_state_ratio{energy_site_id="metro-tottenham"}

# Which bays are actually delivering power right now
energy_evse_power_real_watts{energy_site_id="metro-tottenham"} > 0

# Energy delivered by completed sessions, by bay (one series per session, so sum it)
sum by (energy_evse_id) (energy_session_energy_kW_h)

# Per-turbine output next to the farm's connection point
energy_turbine_power_real_watts{energy_site_id="wind-snowtown"}
energy_windfarm_power_real_watts{energy_site_id="wind-snowtown"}

# Solar: AC vs DC, and the two trackers disagreeing under a cloud edge
energy_inverter_power_real_watts{energy_site_id="solar-mildura"}
energy_inverter_dc_power_watts{energy_site_id="solar-mildura"}
sum by (energy_mppt) (energy_inverter_mppt_power_watts)

# Is the data trustworthy? Count series by quality rather than averaging over the question
count by (energy_site_name, otel_data_point_quality) ({__name__=~"energy_.*"})

# Is the site still sending at all? (see §8 — a link outage and a stoppage look different)
count_over_time(energy_meter_power_real_watts{energy_site_id="wind-snowtown"}[1m])

# How far behind is the device's clock?
modbus_device_clock_offset_seconds

# Load management: how close the highway hub's bays are to the site cap (M4, #12)
energy_site_load_power_watts{energy_site_id="highway-hume"}
  / (energy_site_capacity_current_amperes{energy_site_id="highway-hume"} * 690)

# ... and *why* a bay is below its maximum: the grid, or our own policy
count by (energy_evse_throttled_by) (energy_evse_current_offered_amperes)

# The island's frequency against the NEM's normal band, with the battery's state beside it
energy_microgrid_frequency_hertz{energy_site_id="microgrid-kangaroo-island"}
energy_bess_state_of_charge_percent{energy_site_id="microgrid-kangaroo-island"}
energy_bess_hold_reason_text{energy_site_id="microgrid-kangaroo-island"}

# What the last hour of charging cost, at the recorded spot price (M4, #22)
sum(increase(energy_session_energy_delivered_kW_h_total[1h]))
  * on() group_left() max(energy_market_price_AUD_per_MW_h) / 1000
```

`make prom Q='...'` sends any of these through the `ops` box; Prometheus publishes no host port.

---

### 3.7 The four session-level SLO instruments (M5, #13)

These are the only metrics here that are **not** a device reading or an EVSE gauge: they are
per-session outcomes, and they are attribute-free on purpose (an SLO is a ratio over a fleet; a
per-session dimension would mint a series per session for a number nobody reads per session).

| Metric | Type | Meaning |
|---|---|---|
| `ocpp_transactions_stopped_total{ocpp_stop_reason}` | counter | Sessions the CSMS accepted a `StopTransaction` for, by OCPP 1.6 reason: `Local` (normal), `PowerLoss` (the supply decided), `Other`. `Local / all` is the success rate. |
| `energy_session_time_to_first_power_seconds` | histogram | StartTransaction → first watt delivered. The SLO an operator feels: a bay that says `Charging` and delivers nothing. |
| `energy_session_energy_requested_kW_h_total` | counter | The **vehicle's** ask (its requested current) integrated over the session. |
| `energy_session_energy_delivered_kW_h_total` | counter | What actually flowed. Delivered/requested is the derate-and-sag SLO. |
| `energy_session_power_quality_affected_total` | counter | Sessions where the supply derate engaged at least once. Per 100 sessions, that is power-quality exposure. |

`make slo` prints all four with their targets; [`docs/alarms.md`](alarms.md) is the reasoning behind
the targets and the alarm list derived from the same data.

## 4. Traces

**One charging session is one trace**, owned by the charge point. This is the repo's answer to "APM
cannot model a ten-minute transaction whose end reason is a physical measurement".

```
ocpp.session                    root, CLIENT      ocpp.stop.reason, energy.session.energy_kwh,
├── ocpp.authorize              CLIENT            energy.session.duration_s, energy.evse.id,
├── ocpp.start_transaction      CLIENT            energy.connector.id, energy.session.id,
├── ocpp.meter_values           CLIENT × every 10 s   ocpp.transaction.id, ocpp.id_tag
├── energy.voltage_sag          INTERNAL, spans the sag
│   ├── (event) sag.start        L1/L2/L3 voltages, pu
│   └── (event) sag.end          recovery
└── ocpp.stop_transaction        CLIENT           ocpp.stop.reason, energy.session.energy_kwh
```

`BootNotification` and connector `StatusNotification`s are a **separate** trace: a boot is a property
of the connection, not of a transaction.

```
ocpp.boot                        root
├── ocpp.boot_notification       ocpp.boot.status, ocpp.boot.heartbeat_interval_s
└── ocpp.status_notification     one per connector
```

How to find them: the Jaeger UI at <http://127.0.0.1:16687> (override with `JAEGER_PORT`), service
`vecn-ocpp-cp` — and note the depot's charge point shares that service name, so use
`ocpp.charge_point.id` to tell the sites apart. Span attributes worth filtering on:
`ocpp.transaction.id`, `ocpp.stop.reason` (`Local`, `PowerLoss`, `Other`),
`energy.session.outcome` (`failed_power_loss`, `authorization_rejected`, `start_rejected`, `error`),
`energy.evse.id`, `energy.voltage.pu`.

The join that works today between the metric and the trace is `ocpp.transaction.id`. The
metric→exemplar→trace leg is wired end to end and Prometheus drops the exemplar (D33): `make exemplar`
returns `[]`, by design of the store, not of the pipeline.

---

## 5. Logs

Logs are **OCPP frames and simulator decisions**, not access logs. Both OCPP roles log every frame
they send or receive with the same fields:

| Field | Meaning |
|---|---|
| `ocpp.action` | `Authorize`, `StartTransaction`, `MeterValues`, `StopTransaction`, `BootNotification`, `StatusNotification` |
| `ocpp.message.direction` | `in` / `out` |
| `ocpp.charge_point.id` | which charge point |
| `ocpp.unique_id` | the OCPP message id |
| `ocpp.transaction.id` | present once a transaction exists |
| `ocpp.frame` | the full JSON frame, as a string |

The charge point's copies carry the session's `trace_id`, so a log line and a span are the same
event. The CSMS's copies cannot carry a traceparent (OCPP 1.6 has nowhere to put one) and are joined
by `ocpp.transaction.id` instead — `docs/standards.md` §6.

Where they go: **nowhere persistent, by design** (D45). The hub's `logs` pipeline ends in the `debug`
exporter, and neither Prometheus nor Jaeger is a log store. Read them with:

```bash
make logs          # the hub: every site's telemetry, including log records
make ocpp-logs     # the charge point's frames and warnings
make csms-logs     # the CSMS's view of the same conversation
```

This is the one gap a new user hits immediately, so it is stated in `docs/sizing.md` and §9 below
rather than hidden.

**If you want a log store**, it is one profile and three lines, and it was measured rather than
argued about: `docker compose -f compose/docker-compose.yml --profile loki up -d loki`, then add
the `otlphttp/loki` exporter and a `logs/record` pipeline on the hub — the exact YAML is a comment
in [`collector/hub/hub.yaml`](../collector/hub/hub.yaml), and the verified query is

```
{service_name="vecn-ocpp-cp"} | json | ocpp_transaction_id="42"
```

Loki costs **~50 MiB** on this stack with real OCPP traffic (`docs/sizing.md` §"Loki, measured"),
which is why the reason it is not the default is *scope* — one signal does not deserve a second
query language in a teaching stack — and not footprint. The CSMS's copies are the only frames a
log store adds that the trace cannot give you.

---

## 6. The collectors' own telemetry

Both collector layers export their own metrics over HTTP: edge on `:8888`, hub on `:8889` (the only
one on a host port). These are `otelcol_*` — queue depth, queue capacity, refused/dropped spans,
export failures. `make queues` reads them through the `ops` box:

```bash
make queues        # queued vs capacity, per edge
make queue-detail  # the per-signal breakdown
```

The three that matter when something is wrong:

| Metric | Watch for |
|---|---|
| `otelcol_exporter_queue_size` / `_capacity` | rising = the link is down or too slow; capacity is 5 000 batches per edge |
| `otelcol_exporter_send_failed_*` | non-zero = the hub refused it, not that the link is slow |
| `otelcol_receiver_refused_*` | non-zero = the edge is dropping what a site sent it (the site outran the edge) |

---

## 7. Where the configs are, and how to point this at your backend

```
collector/edge/site.yaml      the only site config; device semantics, quality, timestamps, disk queue
collector/hub/hub.yaml        the only hub config; two metric pipelines, fan-out to stores
impairment/toxiproxy.json     the four WAN links (proxies only — toxics are applied by link.sh, #41)
impairment/link.sh            the link profiles (§8)
dashboards/prometheus/        Prometheus config (OTLP receiver on, 6 h retention)
dashboards/jaeger/            Jaeger v2 config
compose/docker-compose.yml    one block per site: env vars, networks, no host ports in the OT zone
```

**The rule that makes this easy: you only ever edit the hub.** Everything a site means is already
decided at the edge; the hub is where data leaves for a store. A site's identity, names and quality
look identical to every backend, so swapping the backend cannot change what a number means.

Two ways to point it at your own backend.

**A. Change the endpoint (same protocol, OTLP).** The hub's two `visual` exporters take their
endpoints from environment variables in `compose/docker-compose.yml`:

```yaml
hub:
  environment:
    VISUAL_METRICS_ENDPOINT: "https://otlp.your-backend.example:4318"
    VISUAL_TRACES_ENDPOINT: "https://otlp.your-backend.example:4317"
```

That is the whole change — the config's comments name it as the only place a backend endpoint
appears (D2, D15). Add a headers/auth block to the exporters in `hub.yaml` if your backend needs one.

**B. Add a second exporter, and decide which pipeline it belongs to.** The hub runs two metric
pipelines from the same receiver, and the difference is the point:

| Pipeline | Receives | Then | Use it for |
|---|---|---|---|
| `metrics` | everything | `batch` → `debug` | **the backend of record.** Untouched: the two timestamps, `modbus.register.raw`, resource attributes all intact. This is the one to give a real backend. |
| `metrics/visual` | everything | `transform/visual` → `batch` → Prometheus | **the local proof of life.** Sheds the three payload attributes, copies the five site attributes from resource to data point, because Prometheus flattens data point attributes into series identity (D25). |

So to add a real backend, add an exporter and put it on `metrics`, not on `metrics/visual`:

```yaml
exporters:
  otlphttp/backend:
    endpoint: "https://otlp.your-backend.example"
    headers:
      authorization: "Bearer ${env:BACKEND_TOKEN}"

service:
  pipelines:
    metrics:
      receivers: [otlp]
      processors: [batch]
      exporters: [debug, otlphttp/backend]   # untouched data
```

If you point a Prometheus-shaped store at `metrics/visual` instead, you inherit its cardinality
rules — which exist because of a measured failure, not a preference: 4 532 series in 15 minutes from
one meter (`docs/findings.md` #9).

**What you must not do:** edit `collector/edge/site.yaml` to change a backend, or add an exporter
there. The edge is the site's only egress and the thing that must survive an outage; one exporter
per site, to the hub, is the design (D16). Two collector layers, and only two.

**Ports you can reach from the host:** hub OTLP `4317`/`4318` and self-telemetry `8889`; toxiproxy
control `8474`; HMI `127.0.0.1:8091`; Jaeger `127.0.0.1:16687`; Perses `127.0.0.1:8080` (opt-in).
Prometheus deliberately has no host port — use `make prom`. Nothing in an OT zone publishes a port
at all; control is `docker compose exec` (`make fault`, `make session`).

---

## 8. toxiproxy: the problem patterns, and what they do to the telemetry

Each site owns one proxy (`rural-wan`, `solar-wan`, `wind-wan`, `metro-wan`, `microgrid-wan`), so links
degrade independently. `make link SITE=<rural|solar|wind|metro|microgrid>
PROFILE=<wet|degraded|satellite|down>` (`impairment/link.sh`) resets the link, then applies one profile.

| Profile | Toxics applied | What the telemetry does — measured |
|---|---|---|
| `wet` | rural/solar/wind: none. **metro:** 12 ms + ±4 ms latency | Clean baseline. Metro samples carry 12 ms of extra delivery delay; edge queues stay at 0. |
| `degraded` | 250 ms latency + 2 % packet loss | **No queue growth, no quality change, no gap.** Measured over 20 s on wind: queue stayed 0. TCP absorbs 2 % loss and 250 ms is well under the 5 s export interval. If you want to *see* this profile, plot delivery lag, not the data. |
| `satellite` | 650 ms + 5 % loss + 32 kB/s bandwidth | Measured over 45 s on wind: queue touched 0–1 items — one site's telemetry still fits in 32 kB/s. Effects are occasional 1-batch queueing and up to ~0.7 s extra delivery delay. For a visible backlog, raise `POLL_INTERVAL_MS`/add sites, or use `down`. |
| `down` | Proxy disabled (new connections refused) | Edges cannot reach the hub: `make queues` climbs about one item per export interval (measured 3 → 6 → 9 in 45 s at 5 s exports, then 21 after an edge restart), and `count_over_time(...[1m])` for that site goes to 0 — the store sees a gap. **Quality does not change** (§3.4). Restore with `PROFILE=wet`: the queue drains and the parked batches are written back under their **original** timestamps, so a short outage leaves no hole in the chart — measured on wind across a 100 s outage: queue 28 → 0, and 40 samples in the following 4 minutes with no gap. The multi-hour version of this is issue #3. |

Three traps that will otherwise cost you an hour:

1. **A disabled proxy does not kill an established gRPC stream.** `enabled:false` refuses new
   connections; an edge that is already connected keeps delivering while its queue grows (measured:
   samples kept arriving for over a minute after `PROFILE=down`). The queue still fills, so `make
   queues` and the stream disagree. Restart the edge to get a real outage:
   `docker compose -f compose/docker-compose.yml restart edge-wind`. Worth knowing before you
   conclude the impairment is broken.
2. **A WAN outage is not a device outage.** Quality stays `good`; the gap is in delivery. Conversely,
   stopping a device container gives you `comms-lost` at full line rate. Two different failures, two
   different signals — and conflating them is the usual reason a "stale data" alarm fires for the
   wrong thing.
3. **`PROFILE=wet` for metro is 12 ms, not "clear".** The depot's backhaul is fibre with jitter and
   that is its baseline; `make up` applies it. `make profiles` prints the live toxics array so you can
   confirm what is actually applied — `impairment/toxiproxy.json` cannot carry toxics (#41).

Which problem pattern goes with which question:

| You want to show | Use | Look at |
|---|---|---|
| A site keeps working through a bad link | `PROFILE=satellite` | unchanged values, slightly later arrival, tiny queue |
| Telemetry survives a total outage, nothing lost | `PROFILE=down`, wait, `PROFILE=wet` | `make queues` climbing then draining; no gap after restore |
| The store shows a gap while a site is unreachable | `PROFILE=down` + edge restart | `count_over_time(...[1m])` = 0, HMI marks the site stale |
| A value that is present but untrustworthy | stop the device container | `otel_data_point_quality="comms-lost"` |

---

## 9. What you will not see

So that a missing thing reads as a decision, not a bug:

- **No log store.** Logs land in the hub's `debug` exporter (§5). Add a Loki/OTLP backend at the hub.
- **No exemplars in Prometheus.** The exemplar is emitted and carried; Prometheus' OTLP receiver drops
  it (D33). Join on `ocpp.transaction.id`.
- **6 hours of metrics retention.** Prometheus is a proof of life, not a historian. The edge queues —
  not this disk — are the store-and-forward story.
- **No power-flow model.** The depot's meter is its own load and does not sum its twelve bays;
  `docs/standards.md` §6 says so. The numbers are each honest; their sum is not a promised total.
- **One fault only: voltage sag.** No fault framework, no other physics (D29).
- **No IEC 61850, DNP3, OPC UA, real protection or settlement**, and OCPP conformance is not claimed:
  `docs/standards.md` is the complete list.

Next: `docs/sites/` for what each site's physics does to the numbers, `docs/findings.md` for the
traps, `plan.md` for why each of these is the way it is.
