/* ---------------------------------------------------------------------------------------
   VECN operator view.

   Two rules govern everything in this file:

     1. Nothing here knows what a simulator is. Every number on the page comes back from a
        PromQL query, and Prometheus was fed over OTLP by the hub, one hop downstream of the
        impaired link. No view reads a device, a poller or a simulator directly (D18).

     2. A stale value is never drawn as a live one. Quality travels with the value and is
        rendered in place, at the value, in the same cell the operator is already reading.
        Gaps in a trend stay gaps.

   The Prometheus OTLP receiver renames metrics on the way in: the OTLP name `energy.meter.
   voltage` plus UCUM unit `V` becomes `energy_meter_voltage_volts`, and `kW.h` becomes
   `kW_h`. POINTS below is the single place that mapping lives.
   --------------------------------------------------------------------------------------- */

'use strict';

const PROM = 'prom';                 // same-origin reverse proxy (see nginx.conf)
const POLL_MS = 2000;                // how often the page re-queries, not the scan rate

// The M1a poller scans at 1 Hz. Live means "the hub received a point within a few scan
// intervals"; stale means "quiet, but the site itself is still sending"; beyond that the
// site is treated as off the air. Derived from the poll interval rather than hardcoded
// per-metric so that a site with a 10 s scan can be added without lying here.
// The poller scans at 1 Hz, but it does NOT report at 1 Hz: it applies a deadband, so a
// value that has not moved more than the threshold is not sent at all. Measured on the
// running rural meter: 34 838 of 110 850 readings suppressed - about a third of everything
// the poller reads never leaves the site.
//
// To keep "steady" distinguishable from "gone", the poller re-sends an unchanged value at
// least once every HEARTBEAT_S = 60 s (sim/poller/poller.py). That heartbeat is what any age
// threshold here has to clear: a fully healthy register can legitimately be a minute old.
// The first version of this page used 4 s and 30 s and would have painted every steady
// register amber while the plant was fine.
const SCAN_S = 1;
const HEARTBEAT_S = 60;
// The poller scans at 1 Hz but reports every OTEL_METRIC_EXPORT_INTERVAL_MS (default 5 s), so
// 5-10 s is a perfectly current reading. A units change here means a units change in
// compose/docker-compose.yml too - this only decides when the page says "now" rather than
// "unchanged", and it never decides trust.
const REPORT_S = 5;
const LIVE_AGE_S = Math.max(2 * REPORT_S, 4);  // "now" rather than "unchanged", for display only
const QUIET_AGE_S = HEARTBEAT_S * 1.5;         // 90 s: missed a heartbeat, but recent
const LOST_AGE_S = HEARTBEAT_S * 5;            // 300 s: the far end has stopped altogether

// AS 60038 nominal 230 V with the +10% / -6% utilisation range. Drawn on the trend so a
// sag is visibly a departure from a standard, not just a wobble in a line.
const V_NOMINAL = 230;
const V_BAND = [V_NOMINAL * 0.94, V_NOMINAL * 1.10];

/* Metric name (as Prometheus stores it) -> how to read it.

   `slot` is the binding the mimic draws into. A slot with no live point renders as absent,
   never as a zero. Prometheus appends the UCUM unit to the metric name, so `energy.meter.
   voltage` + `V` arrives as `energy_meter_voltage_volts` and `W/m2` as `watts_per_m2`;
   this table is the single place that mapping lives. */
const POINTS = {
  // --- point/feeder meter (M1a, rural site) ---
  energy_meter_voltage_volts:      { slot: 'voltage',     unit: 'V',    label: 'Voltage',        decimals: 1, otel: 'energy.meter.voltage' },
  energy_meter_current_amperes:    { slot: 'current',     unit: 'A',    label: 'Current',        decimals: 1, otel: 'energy.meter.current' },
  energy_meter_power_real_watts:   { slot: 'power',       unit: 'W',    label: 'Real power',     decimals: 0, otel: 'energy.meter.power.real' },
  energy_meter_power_apparent_VA:  { slot: 'power_s',     unit: 'VA',   label: 'Apparent power', decimals: 0, otel: 'energy.meter.power.apparent' },
  energy_meter_power_reactive_var: { slot: 'power_q',     unit: 'var',  label: 'Reactive power', decimals: 0, otel: 'energy.meter.power.reactive' },
  energy_meter_power_factor_ratio: { slot: 'pf',          unit: '',     label: 'Power factor',   decimals: 3, otel: 'energy.meter.power.factor' },
  energy_meter_frequency_hertz:    { slot: 'frequency',   unit: 'Hz',   label: 'Frequency',      decimals: 2, otel: 'energy.meter.frequency' },
  energy_meter_energy_kW_h:        { slot: 'energy',      unit: 'kWh',  label: 'Energy register', decimals: 1, otel: 'energy.meter.energy' },
  energy_meter_temperature_celsius:{ slot: 'temperature', unit: '\u00b0C', label: 'Meter temperature', decimals: 1, otel: 'energy.meter.temperature' },

  // --- the EVSEs and their sessions (M2, rural site) ---
  // These are the only points in the table that come from an application rather than a
  // device poller: the charge point speaks OCPP to the CSMS and Modbus to the meter, and
  // emits semantic names directly. Quality still arrives the same way - `energy.quality`
  // at the charge point, turned into `otel.data_point.quality` by the edge collector, so
  // the box that goes comms-lost is the box that is drawn that way.
  energy_evse_power_real_watts:        { slot: 'evse_power',   unit: 'W',   label: 'EVSE power',          decimals: 0, otel: 'energy.evse.power.real' },
  energy_evse_current_offered_amperes: { slot: 'evse_offered', unit: 'A',   label: 'Current offered',     decimals: 1, otel: 'energy.evse.current.offered' },
  energy_evse_voltage_volts:           { slot: 'evse_voltage', unit: 'V',   label: 'EVSE supply voltage', decimals: 1, otel: 'energy.evse.voltage' },
  energy_evse_state_ratio:             { slot: 'evse_state',   unit: '',    label: 'EVSE state',          decimals: 0, otel: 'energy.evse.state', text: 'energy_evse_state_text' },
  energy_session_energy_kW_h:          { slot: 'session_energy', unit: 'kWh', label: 'Session energy',    decimals: 3, otel: 'energy.session.energy' },

  // --- utility-scale PV inverter, AC side (M1b, solar site) ---
  energy_inverter_voltage_volts:      { slot: 'voltage',      unit: 'V',   label: 'AC voltage',      decimals: 1, otel: 'energy.inverter.voltage' },
  energy_inverter_voltage_line_volts: { slot: 'line_voltage', unit: 'V',   label: 'AC line voltage', decimals: 1, otel: 'energy.inverter.voltage.line' },
  energy_inverter_current_amperes:    { slot: 'current',      unit: 'A',   label: 'AC current',      decimals: 0, otel: 'energy.inverter.current' },
  energy_inverter_power_real_watts:   { slot: 'power',        unit: 'W',   label: 'AC power',        decimals: 0, otel: 'energy.inverter.power.real' },
  energy_inverter_power_apparent_VA:  { slot: 'power_s',      unit: 'VA',  label: 'AC apparent',     decimals: 0, otel: 'energy.inverter.power.apparent' },
  energy_inverter_power_reactive_var: { slot: 'power_q',      unit: 'var', label: 'AC reactive',     decimals: 0, otel: 'energy.inverter.power.reactive' },
  energy_inverter_power_factor_ratio: { slot: 'pf',           unit: '',    label: 'Power factor',    decimals: 3, otel: 'energy.inverter.power.factor' },
  energy_inverter_frequency_hertz:    { slot: 'frequency',    unit: 'Hz',  label: 'Frequency',       decimals: 2, otel: 'energy.inverter.frequency' },
  energy_inverter_energy_kW_h:        { slot: 'energy',       unit: 'kWh', label: 'AC energy',       decimals: 0, otel: 'energy.inverter.energy' },

  // --- PV inverter, DC side and per-tracker ---
  energy_inverter_dc_power_watts:     { slot: 'dc_power',     unit: 'W',   label: 'DC power',        decimals: 0, otel: 'energy.inverter.dc.power' },
  energy_inverter_dc_voltage_volts:   { slot: 'dc_voltage',   unit: 'V',   label: 'DC voltage',      decimals: 0, otel: 'energy.inverter.dc.voltage' },
  energy_inverter_dc_current_amperes: { slot: 'dc_current',   unit: 'A',   label: 'DC current',      decimals: 0, otel: 'energy.inverter.dc.current' },
  energy_inverter_dc_energy_kW_h:     { slot: 'dc_energy',    unit: 'kWh', label: 'DC energy',       decimals: 0, otel: 'energy.inverter.dc.energy' },
  energy_inverter_mppt_power_watts:   { slot: 'mppt_power',   unit: 'W',   label: 'MPPT power',      decimals: 0, otel: 'energy.inverter.mppt.power' },
  energy_inverter_mppt_voltage_volts: { slot: 'mppt_voltage', unit: 'V',   label: 'MPPT voltage',    decimals: 0, otel: 'energy.inverter.mppt.voltage' },
  energy_inverter_mppt_current_amperes:{ slot: 'mppt_current',unit: 'A',   label: 'MPPT current',    decimals: 0, otel: 'energy.inverter.mppt.current' },
  energy_inverter_mppt_temperature_celsius: { slot: 'mppt_temp', unit: '\u00b0C', label: 'MPPT temperature', decimals: 1, otel: 'energy.inverter.mppt.temperature' },

  // --- PV inverter, thermal and operating state ---
  energy_inverter_temperature_cabinet_celsius:  { slot: 'cabinet_temp',  unit: '\u00b0C', label: 'Cabinet temperature',  decimals: 1, otel: 'energy.inverter.temperature.cabinet' },
  energy_inverter_temperature_heatsink_celsius: { slot: 'heatsink_temp', unit: '\u00b0C', label: 'Heatsink temperature', decimals: 1, otel: 'energy.inverter.temperature.heatsink' },
  energy_inverter_status_ratio:       { slot: 'status',       unit: '',    label: 'Operating state', decimals: 0, otel: 'energy.inverter.status', text: 'energy_inverter_status_text' },
  energy_inverter_event_ratio:        { slot: 'event',        unit: '',    label: 'Event bitfield',  decimals: 0, otel: 'energy.inverter.event' },
  energy_inverter_fault_code_ratio:   { slot: 'fault_code',   unit: '',    label: 'Fault code',      decimals: 0, otel: 'energy.inverter.fault_code', text: 'energy_inverter_fault_code_text' },
  energy_inverter_curtailment_ratio:  { slot: 'curtailment',  unit: '',    label: 'Curtailment',     decimals: 0, otel: 'energy.inverter.curtailment', text: 'energy_inverter_curtailment_text' },
  energy_inverter_export_limit_percent: { slot: 'export_limit', unit: '%', label: 'Export limit',    decimals: 0, otel: 'energy.inverter.export_limit' },

  // --- the weather pair ---
  energy_pv_irradiance_watts_per_m2:     { slot: 'irradiance',   unit: 'W/m\u00b2', label: 'Irradiance', decimals: 0, otel: 'energy.pv.irradiance' },
  energy_pv_module_temperature_celsius:  { slot: 'module_temp',  unit: '\u00b0C', label: 'Module temperature',  decimals: 1, otel: 'energy.pv.module_temperature' },
  energy_pv_ambient_temperature_celsius: { slot: 'ambient_temp', unit: '\u00b0C', label: 'Ambient temperature', decimals: 1, otel: 'energy.pv.ambient_temperature' },

  // --- wind turbines, per machine (M1b, wind site) ---
  energy_turbine_power_real_watts:      { slot: 'turbine_power',   unit: 'W',    label: 'Turbine power',   decimals: 0, otel: 'energy.turbine.power.real' },
  energy_turbine_power_reactive_var:    { slot: 'turbine_power_q', unit: 'var',  label: 'Turbine reactive', decimals: 0, otel: 'energy.turbine.power.reactive' },
  energy_turbine_energy_kW_h:           { slot: 'turbine_energy',  unit: 'kWh',  label: 'Turbine energy',   decimals: 0, otel: 'energy.turbine.energy' },
  energy_turbine_current_amperes:       { slot: 'turbine_current', unit: 'A',    label: 'Turbine current',  decimals: 0, otel: 'energy.turbine.current' },
  energy_turbine_voltage_volts:         { slot: 'turbine_voltage', unit: 'V',    label: 'Turbine voltage',  decimals: 1, otel: 'energy.turbine.voltage' },
  energy_turbine_wind_speed_meters_per_second: { slot: 'turbine_wind', unit: 'm/s', label: 'Turbine wind speed', decimals: 1, otel: 'energy.turbine.wind_speed' },
  energy_turbine_wind_direction_deg:    { slot: 'turbine_dir',     unit: '\u00b0', label: 'Turbine wind direction', decimals: 0, otel: 'energy.turbine.wind_direction' },
  energy_turbine_rotor_speed_per_min:   { slot: 'turbine_rpm',     unit: 'rpm',  label: 'Rotor speed',      decimals: 1, otel: 'energy.turbine.rotor_speed' },
  energy_turbine_pitch_angle_deg:       { slot: 'turbine_pitch',   unit: '\u00b0', label: 'Blade pitch',   decimals: 1, otel: 'energy.turbine.pitch_angle' },
  energy_turbine_temperature_nacelle_celsius:  { slot: 'turbine_nac_temp',  unit: '\u00b0C', label: 'Nacelle temperature',  decimals: 1, otel: 'energy.turbine.temperature.nacelle' },
  energy_turbine_temperature_gearbox_celsius:  { slot: 'turbine_gear_temp', unit: '\u00b0C', label: 'Gearbox temperature', decimals: 1, otel: 'energy.turbine.temperature.gearbox' },
  energy_turbine_status_ratio:          { slot: 'turbine_status',  unit: '',     label: 'Turbine state',    decimals: 0, otel: 'energy.turbine.status', text: 'energy_turbine_status_text' },
  energy_turbine_availability_ratio:    { slot: 'turbine_avail',   unit: '',     label: 'Turbine availability', decimals: 0, otel: 'energy.turbine.availability' },

  // --- wind farm connection point ---
  energy_windfarm_power_real_watts:     { slot: 'power',          unit: 'W',    label: 'Farm power',       decimals: 0, otel: 'energy.windfarm.power.real' },
  energy_windfarm_power_reactive_var:   { slot: 'power_q',        unit: 'var',  label: 'Farm reactive',    decimals: 0, otel: 'energy.windfarm.power.reactive' },
  energy_windfarm_energy_kW_h:          { slot: 'energy',         unit: 'kWh',  label: 'Farm energy',      decimals: 0, otel: 'energy.windfarm.energy' },
  energy_windfarm_frequency_hertz:      { slot: 'frequency',      unit: 'Hz',   label: 'Farm frequency',   decimals: 2, otel: 'energy.windfarm.frequency' },
  energy_windfarm_ambient_temperature_celsius: { slot: 'ambient_temp', unit: '\u00b0C', label: 'Ambient temperature', decimals: 1, otel: 'energy.windfarm.ambient_temperature' },
  energy_windfarm_wind_speed_meters_per_second: { slot: 'wind_speed', unit: 'm/s', label: 'Farm wind speed', decimals: 1, otel: 'energy.windfarm.wind_speed' },
  energy_windfarm_wind_direction_deg:   { slot: 'wind_direction', unit: '\u00b0', label: 'Farm wind direction', decimals: 0, otel: 'energy.windfarm.wind_direction' },
  energy_windfarm_status_ratio:         { slot: 'status',         unit: '',     label: 'Farm state',       decimals: 0, otel: 'energy.windfarm.status', text: 'energy_windfarm_status_text' },
  energy_windfarm_availability_ratio:   { slot: 'availability',   unit: '',     label: 'Turbines available', decimals: 0, otel: 'energy.windfarm.availability' },
  energy_windfarm_export_limit_percent: { slot: 'export_limit',   unit: '%',    label: 'Export limit',     decimals: 0, otel: 'energy.windfarm.export_limit' },
  energy_windfarm_curtailment_ratio:    { slot: 'curtailment',    unit: '',     label: 'Curtailment',      decimals: 0, otel: 'energy.windfarm.curtailment', text: 'energy_windfarm_curtailment_text' },
  energy_windfarm_fault_code_ratio:     { slot: 'fault_code',     unit: '',     label: 'Farm fault code',  decimals: 0, otel: 'energy.windfarm.fault_code' },

  // --- isolated microgrid (M3-2, the fifth site) ---
  // Frequency first: on this site it is the primary signal, not voltage. A grid-forming
  // inverter holds the busbar at 230 V; nothing holds it at 50.00 Hz.
  energy_microgrid_frequency_hertz:        { slot: 'frequency',    unit: 'Hz',  label: 'Busbar frequency',   decimals: 3, otel: 'energy.microgrid.frequency' },
  energy_microgrid_rocof_hertz_per_second: { slot: 'rocof',        unit: 'Hz/s', label: 'RoCoF',             decimals: 3, otel: 'energy.microgrid.rocof' },
  energy_microgrid_voltage_volts:          { slot: 'voltage',      unit: 'V',   label: 'Busbar voltage',    decimals: 1, otel: 'energy.microgrid.voltage' },
  energy_microgrid_grid_forming_unit_ratio:{ slot: 'grid_forming', unit: '',    label: 'Grid-forming unit', decimals: 0, otel: 'energy.microgrid.grid_forming_unit' },
  energy_bess_state_of_charge_percent:     { slot: 'soc',          unit: '%',   label: 'Battery SoC',       decimals: 1, otel: 'energy.bess.state_of_charge' },
  energy_bess_power_real_watts:            { slot: 'bess_power',   unit: 'W',   label: 'Battery power',     decimals: 0, otel: 'energy.bess.power.real' },
  energy_bess_energy_kW_h:                 { slot: 'bess_energy',  unit: 'kWh', label: 'Battery throughput', decimals: 0, otel: 'energy.bess.energy' },
  energy_bess_energy_usable_kW_h:          { slot: 'bess_usable',  unit: 'kWh', label: 'Battery usable',    decimals: 0, otel: 'energy.bess.energy.usable' },
  energy_bess_power_max_charge_watts:      { slot: 'bess_max_charge', unit: 'W', label: 'Max charge power', decimals: 0, otel: 'energy.bess.power.max_charge' },
  energy_bess_power_max_discharge_watts:   { slot: 'bess_max_discharge', unit: 'W', label: 'Max discharge power', decimals: 0, otel: 'energy.bess.power.max_discharge' },
  energy_bess_status_ratio:                { slot: 'bess_status',  unit: '',    label: 'Battery state',     decimals: 0, otel: 'energy.bess.status', text: 'energy_bess_status_text' },
  energy_bess_hold_reason_ratio:           { slot: 'hold_reason',  unit: '',    label: 'Hold reason',       decimals: 0, otel: 'energy.bess.hold_reason', text: 'energy_bess_hold_reason_text' },
  energy_bess_temperature_celsius:         { slot: 'temperature',  unit: '\u00b0C', label: 'Battery temperature', decimals: 1, otel: 'energy.bess.temperature' },
  energy_microgrid_source_power_real_watts:{ slot: 'source_power', unit: 'W',   label: 'Source power',      decimals: 0, otel: 'energy.microgrid.source.power.real' },
  energy_microgrid_source_power_available_watts: { slot: 'source_available', unit: 'W', label: 'Source available', decimals: 0, otel: 'energy.microgrid.source.power.available' },
  energy_microgrid_source_availability_ratio: { slot: 'source_availability', unit: '', label: 'Source available flag', decimals: 0, otel: 'energy.microgrid.source.availability' },
  energy_microgrid_source_id_ratio:        { slot: 'source_id',    unit: '',    label: 'Source id',         decimals: 0, otel: 'energy.microgrid.source.id' },
  energy_microgrid_load_power_real_watts:  { slot: 'load_power',   unit: 'W',   label: 'Site load',         decimals: 0, otel: 'energy.microgrid.load.power.real' },
  energy_microgrid_load_power_ev_watts:    { slot: 'ev_power',     unit: 'W',   label: 'EV charging load',  decimals: 0, otel: 'energy.microgrid.load.power.ev' },
  energy_microgrid_load_power_balance_watts: { slot: 'balance',    unit: 'W',   label: 'Generation minus load', decimals: 0, otel: 'energy.microgrid.load.power.balance' },
};

//: Slots whose value is a power in W and is drawn in kW on the one-line.
const POWER_SLOTS = new Set(['power', 'power_s', 'power_q', 'dc_power', 'mppt_power', 'evse_power',
  'bess_power', 'bess_max_charge', 'bess_max_discharge', 'source_power', 'source_available',
  'load_power', 'ev_power', 'balance']);

/* Site templates. The topology is drawn in full — an electrical engineer reads a one-line
   diagram, and hiding the switchgear would make the page look like a JSON viewer. Anything
   the pipeline does not instrument is drawn dashed and labelled as such rather than filled
   with a plausible number (plan.md §12, "realistic must not quietly become invented"). */
const TEMPLATES = {
  charging_site: {
    summary: 'site load',
    sign: 1,   // meter convention: positive real power = import = load
    // The bays are NOT listed here: the mimic draws one cell per EVSE the pipeline actually
    // reports, so a two-bay rural site and a twelve-bay depot are the same drawing (M3-1).
    assets: ['GRID', 'CB1', 'TX1', 'BUSBAR', 'M1', 'EVSEn'],
    evse: true,
  },
  solar_farm: {
    summary: 'generation',
    sign: 1,   // the inverter's own convention is export-positive
    assets: ['PV', 'INV1', 'BUSBAR', 'TX1', 'GRID', 'M1'],
    evse: false,
    tx_note: '0.69 / 22 kV',
  },
  wind_farm: {
    summary: 'generation',
    sign: 1,
    assets: ['WTG1', 'WTG2', 'BUSBAR', 'TX1', 'GRID', 'M1'],
    evse: false,
    tx_note: '0.69 / 22 kV',
  },
  // An islanded site: no transformer to a grid, no connection point, no export. Sources on
  // the left, the grid former in the middle, load on the right, and a busbar that is the
  // only thing tying them together. `trend: 'frequency'` because frequency, not voltage, is
  // this site's primary signal.
  microgrid: {
    summary: 'islanded',
    sign: 1,
    assets: ['SOLAR', 'WIND', 'BESS', 'BUSBAR', 'LOAD'],
    evse: false,
    trend: 'frequency',
    v_nominal: 230,
  },
};

/* Where each site's label sits relative to its marker, in viewBox units. Only sites that
   need help are listed; everything else gets the default in drawMarkers(). */
const LABEL_OFFSET = {
  'rural-brokenhill': { dx: -26, dy: -20, anchor: 'end' },    // inland, away from Mildura
  'solar-mildura':    { dx: 30,  dy: 30,  anchor: 'start' },  // down toward the Murray
  'wind-snowtown':    { dx: -30, dy: 34,  anchor: 'end' },    // south-west, clear of both
  'metro-tottenham':  { dx: 34,  dy: 4,   anchor: 'start' },  // out to the west of Melbourne
};

const state = {
  sites: new Map(),      // site id -> { id, name, kind, lat, lon }
  points: new Map(),     // 'siteId|metric|phase|direction' -> { value, ts, quality, labels }
  newestTs: 0,           // freshest sample in the store, for clock-free "unchanged" ages
  geography: null,
  selected: null,
  trend: null,
  error: null,
};

/* ------------------------------------------------------------------ transport */

/* The browser's clock is not the authority on how old OT data is. Remote sites in this
   network have no reliable NTP (plan.md 5.5) - and neither does the machine looking at the
   map. Each response's HTTP Date header carries the store's own time, so ages are measured
   against that rather than against whatever the viewer's laptop believes.

   (This is not theoretical: the first version of this page looked perfectly healthy, and a
   screenshot taken with Chrome's --virtual-time-budget reported every site as stale, because
   that flag virtualises Date.now() while the sample timestamps stay real.) */
let serverSkewMs = 0;

function noteServerTime(res) {
  const hdr = res.headers.get('date');
  if (!hdr) return;
  const t = Date.parse(hdr);
  if (!Number.isNaN(t)) serverSkewMs = t - Date.now();
}

const nowMs = () => Date.now() + serverSkewMs;
const nowS = () => nowMs() / 1000;

function url(base, params) {
  const q = new URLSearchParams(params);
  return `${PROM}/api/v1/${base}?${q}`;
}

async function pq(expr) {
  const r = await fetch(url('query', { query: expr }), { cache: 'no-store' });
  if (!r.ok) throw new Error(`Prometheus ${r.status}`);
  noteServerTime(r);
  return (await r.json()).data.result;
}

async function prange(expr, seconds, step) {
  const end = Math.floor(nowS());
  const r = await fetch(url('query_range', {
    query: expr, start: end - seconds, end, step: `${step}s`,
  }), { cache: 'no-store' });
  if (!r.ok) throw new Error(`Prometheus ${r.status}`);
  noteServerTime(r);
  return (await r.json()).data.result;
}

/* ------------------------------------------------------------------ ingest */

const num = (v) => (v === undefined || v === null || v === '' ? null : Number(v));

function keyOf(siteId, metric, phase, direction, mppt, plane, turbine, evse, source) {
  // Direction is part of the identity: the site meter keeps separate import and export
  // registers, and keying without it silently loses one of them. The MPPT block number, the
  // irradiance plane, the wind turbine number and the EVSE id are part of it too - dropping
  // them makes tracker 2 (or EVSE02, or session 2) overwrite the first in a Map, which looks
  // exactly like a working page. Session energy is keyed by EVSE for the same reason: two
  // EVSEs charging at once are two series, not one.
  // The source name is part of the identity for the same reason: the microgrid's two
  // repeating source blocks report the same metric name, and keying without it makes the
  // turbine overwrite the array.
  return `${siteId}|${metric}|${phase || ''}|${direction || ''}|${mppt || ''}|${plane || ''}|${turbine || ''}|${evse || ''}|${source || ''}`;
}

async function refresh() {
  let sites, series;
  try {
    [sites, series] = await Promise.all([
      // Every site the pipeline has seen, whether or not it has instrumented points yet.
      pq('target_info{energy_site_id!=""}'),
      // Everything instrumented, every site, in one query.
      pq('{__name__=~"energy_(meter|inverter|pv|turbine|windfarm|evse|session|microgrid|bess)_.*",energy_site_id!=""}'),
    ]);
    state.error = null;
  } catch (e) {
    state.error = String(e.message || e);
    render();
    return;
  }

  for (const s of sites) {
    const m = s.metric;
    if (state.sites.has(m.energy_site_id)) continue;
    state.sites.set(m.energy_site_id, {
      id: m.energy_site_id,
      name: m.energy_site_name || m.energy_site_id,
      kind: m.energy_site_kind || 'unknown',
      lat: num(m.geo_latitude),
      lon: num(m.geo_longitude),
    });
  }

  const seen = new Set();
  for (const s of series) {
    const m = s.metric;
    const site = state.sites.get(m.energy_site_id);
    if (!site) continue;
    // A site can be known from its metrics before target_info is scraped.
    if (!seen.has(site.id) && site.lat === null) {
      site.lat = num(m.geo_latitude);
      site.lon = num(m.geo_longitude);
    }
    seen.add(site.id);

    const metric = m.__name__;
    const def = POINTS[metric];
    if (!def) continue;

    const k = keyOf(site.id, metric, m.energy_phase, m.energy_direction, m.energy_mppt, m.energy_irradiance_plane, m.energy_turbine, m.energy_evse_id, m.energy_source_name);
    const point = {
      metric,
      def,
      phase: m.energy_phase || null,
      direction: m.energy_direction || null,
      mppt: m.energy_mppt || null,
      plane: m.energy_irradiance_plane || null,
      turbine: m.energy_turbine || null,
      evse: m.energy_evse_id || null,
      source: m.energy_source_name || null,
      value: Number(s.value[1]),
      ts: Number(s.value[0]) * 1000,   // the store's sample time = the hub's receive time
      quality: m.otel_data_point_quality || 'unknown',
      labels: m,
    };
    // Device time is shed before the visual store (see collector/hub/hub.yaml), so the
    // skew between the device clock and the hub is not available here by design.
    //
    // Newest wins. A restart mints a new service.instance.id, and a new OCPP transaction
    // mints a new session-energy series, so the same key can be backed by more than one
    // series for the staleness window (docs/findings.md #12). Without this rule the map kept
    // whichever series Prometheus happened to return last, which for an idle EVSE was the
    // pre-fix `comms-lost` copy. The store's sample time is the hub's receive time, so this
    // is a comparison of two facts from one clock.
    const existing = state.points.get(k);
    if (existing && existing.ts > point.ts) continue;
    state.points.set(k, point);
  }

  // The freshest sample the store returned this round. Everything else is measured against
  // it, which is how a point's "unchanged" age is known without trusting any clock.
  state.newestTs = series.reduce((mx, s) => Math.max(mx, Number(s.value[0]) * 1000), 0);

  render();
}

/* Ages, two ways, because they mean different things:

     lag: how far behind the freshest sample in the network this point is. Clock-free.
     age: how long ago the hub received it. Uses the store's clock, not the viewer's. */
function lagS(p) { return state.newestTs ? (state.newestTs - p.ts) / 1000 : 0; }
function ageS(p) { return (nowMs() - p.ts) / 1000; }

/* The state of ONE point.

   Quality is the authority: it is what the edge collector asserted about the reading (D12,
   D21), and the edge derives it from the device's own clock and the transport - facts this
   page cannot see. Age overrides it only in the case the far end cannot express: the point
   has stopped arriving even at its heartbeat.

   Note what is deliberately absent: "the value has not changed recently, therefore it is
   untrustworthy". That is how a deadband works (D22). Half the readings never leave the
   site, so freshness and trust are different questions, and only the device end knows the
   second one. Every OT protocol that solved this carries a quality flag - DNP3 comms-lost,
   IEC 104's NT (not topical), OPC UA StatusCode. Modbus carries nothing, which is why the
   edge collector has to invent it (plan.md 11.1, the strongest contribution candidate). */
function pointState(p) {
  if (p.quality === 'bad' || p.quality === 'comms-lost' || p.quality === 'stale') return p.quality;
  const age = ageS(p);
  if (age > LOST_AGE_S) return 'comms-lost';
  if (age > QUIET_AGE_S) return 'stale';
  return 'good';
}

const STATE_RANK = { good: 0, stale: 1, bad: 2, 'comms-lost': 3 };

function pointsFor(siteId) {
  const out = [];
  for (const p of state.points.values()) {
    if (p.labels.energy_site_id === siteId) out.push(p);
  }
  return out;
}

/* One state for a whole site: the worst thing known about any of its points. */
function siteState(siteId) {
  if (state.error) return 'error';
  const pts = pointsFor(siteId);
  if (!pts.length) return 'none';
  let worst = 'good';
  for (const p of pts) {
    const s = pointState(p);
    if (STATE_RANK[s] > STATE_RANK[worst]) worst = s;
  }
  return worst;
}

/* Real power for the map readout, in the site's own direction. */
function sitePower(site) {
  const pts = pointsFor(site.id).filter((p) => p.def.slot === 'power' && !p.phase);
  if (!pts.length) return null;
  const p = pts[0];
  const tpl = TEMPLATES[site.kind] || { sign: 1 };
  return { value: (p.value * tpl.sign) / 1000, unit: 'kW', point: p };
}

/* ------------------------------------------------------------------ formatting */

function fmt(v, decimals) {
  if (v === null || v === undefined || Number.isNaN(v)) return '\u2014';
  return v.toLocaleString('en-AU', { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}

function fmtAge(seconds) {
  if (seconds < 1) return 'now';
  if (seconds < 60) return `${seconds.toFixed(0)}s`;
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  if (m < 60) return `${m}m ${s}s`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

function shortName(site) {
  // "Broken Hill EV Charging Site" -> "Broken Hill". A drawing labels a site by its place.
  return site.name.replace(/\s+(EV Charging Site|Solar Farm|Wind Farm)$/i, '');
}

/* ------------------------------------------------------------------ the map */

function projectPoint(lon, lat, box) {
  // Equirectangular, with longitude compressed by cos(latitude) so Australia is not
  // stretched. Good enough to place a site within a pixel of where an engineer expects it,
  // and it needs no projection library and no tile server.
  const kx = Math.cos(((-25.5 * Math.PI) / 180));
  const x = (lon - box.lon0) * kx;
  const y = (box.lat0 - lat);
  return [box.padX + x * box.scale, box.padY + y * box.scale];
}

function geographyBox(geo, w, h, pad) {
  let lon0 = 180, lon1 = -180, lat0 = -90, lat1 = 90;
  const walk = (c) => {
    if (typeof c[0] === 'number') {
      lon0 = Math.min(lon0, c[0]); lon1 = Math.max(lon1, c[0]);
      lat0 = Math.max(lat0, c[1]); lat1 = Math.min(lat1, c[1]);
    } else c.forEach(walk);
  };
  for (const f of geo.features) walk(f.geometry.coordinates);

  const kx = Math.cos(((-25.5 * Math.PI) / 180));
  const spanX = (lon1 - lon0) * kx;
  const spanY = lat0 - lat1;
  const scale = Math.min((w - 2 * pad) / spanX, (h - 2 * pad) / spanY);
  return {
    lon0, lat0, scale,
    padX: pad + (w - 2 * pad - spanX * scale) / 2,
    padY: pad + (h - 2 * pad - spanY * scale) / 2,
  };
}

function geographyExtent(geo, box) {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const f of geo.features) {
    const polys = f.geometry.type === 'MultiPolygon' ? f.geometry.coordinates : [f.geometry.coordinates];
    for (const poly of polys) {
      for (const ring of poly) {
        for (const [lon, lat] of ring) {
          const [x, y] = projectPoint(lon, lat, box);
          x0 = Math.min(x0, x); y0 = Math.min(y0, y);
          x1 = Math.max(x1, x); y1 = Math.max(y1, y);
        }
      }
    }
  }
  return { x0, y0, x1, y1 };
}

function ringPath(ring, box) {
  return ring.map(([lon, lat], i) => {
    const [x, y] = projectPoint(lon, lat, box);
    return `${i ? 'L' : 'M'}${x.toFixed(1)} ${y.toFixed(1)}`;
  }).join(' ') + ' Z';
}

function drawMapBase() {
  const svg = document.getElementById('map');
  const W = 1100, H = 820;
  const box = geographyBox(state.geography, W, H, 90);

  const paths = [];
  for (const f of state.geography.features) {
    const polys = f.geometry.type === 'MultiPolygon' ? f.geometry.coordinates : [f.geometry.coordinates];
    for (const poly of polys) {
      for (const ring of poly) {
        // Tasmania is the only ring entirely below 39 S. Taking that from the geometry beats
        // depending on the order the polygons happen to appear in the file.
        const isTas = ring.every(([, lat]) => lat < -39);
        paths.push(`<path class="land${isTas ? ' land-tas' : ''}" d="${ringPath(ring, box)}"/>`);
      }
    }
  }

  // Crop the viewBox to the drawing rather than a fixed 1100x820 canvas, so the continent
  // fills the panel instead of floating in it. The side margins are wide because the site
  // labels sit outboard of the coast.
  const span = geographyExtent(state.geography, box);
  const x0 = span.x0 - 170, y0 = span.y0 - 60;
  const vw = span.x1 - span.x0 + 340, vh = span.y1 - span.y0 + 130;
  svg.setAttribute('viewBox', `${x0.toFixed(0)} ${y0.toFixed(0)} ${vw.toFixed(0)} ${vh.toFixed(0)}`);

  // A graticule at 10 degrees and the Tropic of Capricorn: the reference lines an Australian
  // map is drawn with, and what makes a marker's position readable. Drawn over the land, not
  // under it - a faint rule beneath a filled landmass is invisible.
  const lines = [];
  for (let lon = 110; lon <= 160; lon += 10) {
    const [x] = projectPoint(lon, box.lat0, box);
    lines.push(`<line class="graticule" x1="${x.toFixed(1)}" y1="${y0.toFixed(0)}" x2="${x.toFixed(1)}" y2="${(y0 + vh).toFixed(0)}"/>`);
  }
  for (let lat = -40; lat <= -10; lat += 10) {
    const [, y] = projectPoint(box.lon0, lat, box);
    lines.push(`<line class="graticule" x1="${x0.toFixed(0)}" y1="${y.toFixed(1)}" x2="${(x0 + vw).toFixed(0)}" y2="${y.toFixed(1)}"/>`);
  }
  const [, ty] = projectPoint(box.lon0, -23.44, box);
  lines.push(`<line class="tropic" x1="${x0.toFixed(0)}" y1="${ty.toFixed(1)}" x2="${(x0 + vw).toFixed(0)}" y2="${ty.toFixed(1)}"/>`);
  lines.push(`<text class="graticule-label" x="${(x0 + 12).toFixed(0)}" y="${(ty - 8).toFixed(1)}">Tropic of Capricorn</text>`);

  svg.innerHTML = `<g class="land-group">${paths.join('')}</g>`
    + `<g class="graticule-group">${lines.join('')}</g>`
    + `<g id="markers"></g>`;
  svg._box = box;
}

function drawMarkers() {
  const svg = document.getElementById('map');
  const box = svg._box;
  if (!box) return;

  const parts = [];
  for (const site of state.sites.values()) {
    if (site.lat === null || site.lon === null) continue;
    const [x, y] = projectPoint(site.lon, site.lat, box);
    const st = siteState(site.id);
    const power = sitePower(site);
    const tpl = TEMPLATES[site.kind] || { summary: 'site' };

    // Labels are placed outboard of the coast with sea room. Hand-tuned per site, because
    // the coastline is not a rectangle and two sites 250 km apart will happily overprint
    // each other otherwise. Default: out to the right, or to the left near the east coast.
    const off = LABEL_OFFSET[site.id] ||
      (site.lon > 138 && site.lat < -30 ? { dx: -24, dy: -14, anchor: 'end' }
                                        : { dx: 24, dy: -14, anchor: 'start' });
    const { dx, dy, anchor } = off;

    const value = power
      ? `${fmt(power.value, 1)} kW ${tpl.summary}`
      : 'no instrumented points';

    parts.push(`
      <g class="marker q-${st}" data-site="${site.id}" tabindex="0" role="button"
         aria-label="${shortName(site)}, ${st}, ${value}">
        <circle class="halo" cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="15"/>
        <rect class="body" x="${(x - 8).toFixed(1)}" y="${(y - 8).toFixed(1)}" width="16" height="16"/>
        <rect class="hit" x="${(x - 22).toFixed(1)}" y="${(y - 22).toFixed(1)}" width="44" height="44"/>
        <line class="leader" x1="${x.toFixed(1)}" y1="${y.toFixed(1)}"
              x2="${(x + dx * 0.6).toFixed(1)}" y2="${(y + dy * 0.9).toFixed(1)}"
              stroke="#7d8981" stroke-width="0.8"/>
        <text class="label" x="${(x + dx).toFixed(1)}" y="${(y + dy).toFixed(1)}"
              text-anchor="${anchor}">${shortName(site)}</text>
        <text class="sub" x="${(x + dx).toFixed(1)}" y="${(y + dy + 21).toFixed(1)}"
              text-anchor="${anchor}">${value}</text>
      </g>`);
  }

  document.getElementById('markers').innerHTML = parts.join('');
  for (const g of document.querySelectorAll('.marker')) {
    g.addEventListener('click', () => openSite(g.dataset.site));
    g.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openSite(g.dataset.site); }
    });
  }
}

/* ------------------------------------------------------------------ the mimic */

function ch(v, unit, decimals, quality) {
  // One value, in place, with its state. This is the whole quality story: the number is
  // drawn in the state it is in, where the operator is already looking.
  if (v === null) {
    return `<tspan class="val val-absent">\u2014</tspan><tspan class="val-unit"> ${unit}</tspan>`;
  }
  return `<tspan class="val q-${quality}">${fmt(v, decimals)}</tspan><tspan class="val-unit"> ${unit}</tspan>`;
}

function bound(siteId, slot, phase, opts = {}) {
  for (const p of state.points.values()) {
    if (p.labels.energy_site_id !== siteId) continue;
    if (p.def.slot !== slot) continue;
    if ((p.phase || null) !== (phase || null)) continue;
    if (opts.mppt !== undefined && (p.mppt || null) !== opts.mppt) continue;
    if (opts.plane !== undefined && (p.plane || null) !== opts.plane) continue;
    if (opts.turbine !== undefined && (p.turbine || null) !== opts.turbine) continue;
    if (opts.evse !== undefined && (p.evse || null) !== opts.evse) continue;
    // The microgrid's two source blocks share a metric name, so "which source" is a filter
    // argument, exactly like "which phase" or "which turbine".
    if (opts.source !== undefined && (p.source || null) !== opts.source) continue;
    // The site meter keeps separate import and export registers under one slot; without this
    // the panel picked whichever series came back first and the depot's 128 MWh could read as
    // the static 12 kWh export register. Direction is part of a point's identity.
    if (opts.direction !== undefined && (p.direction || null) !== opts.direction) continue;
    return p;
  }
  return null;
}

function valueOf(siteId, slot, phase, opts = {}) {
  const p = bound(siteId, slot, phase, opts);
  if (!p) return { html: ch(null, '', 0, 'unknown'), quality: 'unknown', point: null };
  const def = p.def;
  let v = p.value;
  const isPower = POWER_SLOTS.has(def.slot);
  if (isPower) v = v / 1000;
  const unit = isPower ? `k${def.unit}` : def.unit;
  // kW gets a decimal on the diagram even though the raw watts reading is an integer: a
  // phase at 4.6 kW must not read as "5" on a one-line diagram.
  const dp = isPower ? 1 : def.decimals;
  return { html: ch(v, unit, dp, pointState(p)), quality: pointState(p), point: p };
}

/* Value without its unit, for a table that carries the units in a header row. */
function rawOf(siteId, slot, phase, opts = {}) {
  const p = bound(siteId, slot, phase, opts);
  if (!p) return { html: ch(null, '', 0, 'unknown'), quality: 'unknown', point: null };
  const isPower = POWER_SLOTS.has(p.def.slot);
  const v = isPower ? p.value / 1000 : p.value;
  return { html: ch(v, '', isPower ? 1 : p.def.decimals, pointState(p)), quality: pointState(p), point: p };
}

/* A cumulative register in GWh: 44 308 442 kWh is unreadable in place, and truncating it
   would hide that it is a lifetime counter, not a session figure. Below 1 GWh it reads in
   MWh, because "0.13 GWh" throws away more precision than a 128 MWh depot register deserves. */
function gwhText(p) {
  if (!p) return '<tspan class="val val-absent">\u2014</tspan>';
  const gwh = p.value / 1e6;
  const [v, unit] = gwh >= 1 ? [gwh, 'GWh'] : [p.value / 1e3, 'MWh'];
  return `<tspan class="val q-${pointState(p)}">${fmt(v, gwh >= 1 ? 2 : 1)}</tspan><tspan class="val-unit"> ${unit}</tspan>`;
}

/* A text-valued state (SunSpec status, curtailment reason) with the same quality treatment
   as a number. The edge collector attached the text; the view does not map code -> word. */
function textOf(siteId, slot, opts = {}) {
  const p = bound(siteId, slot, null, opts);
  if (!p || !p.def.text) return { html: '<tspan class="val val-absent">\u2014</tspan>', quality: 'unknown', point: null };
  const text = p.labels[p.def.text];
  const q = pointState(p);
  if (!text) return { html: `<tspan class="val val-absent">${fmt(p.value, 0)}</tspan>`, quality: q, point: p };
  return { html: `<tspan class="val q-${q}">${text}</tspan>`, quality: q, point: p };
}

/* Every bay this site reports. Read from the data, not from a template: a two-bay rural site
   and a twelve-bay depot must be the same drawing with a different number of cells, which is
   what "the difference between sites is environment and scale, not code paths" has to mean
   for the HMI too. A bay with no live series is simply absent - the mimic never invents one. */
function evseIdsFor(siteId) {
  const ids = new Set();
  for (const p of state.points.values()) {
    if (p.labels.energy_site_id === siteId && p.evse) ids.add(p.evse);
  }
  return [...ids].sort((a, b) => a.localeCompare(b, 'en-AU', { numeric: true }));
}

function renderMimic(site) {
  const tpl = TEMPLATES[site.kind];
  const svg = document.getElementById('mimic');

  if (!tpl) {
    svg.innerHTML = `<text x="20" y="60" class="note note-absent">No one-line template for site kind "${site.kind}". The map still places it; the mimic does not invent a topology for it.</text>`;
    return;
  }

  // A generation site is a different one-line: source -> inverter -> step-up -> grid, with a
  // DC side that a load site simply does not have. Drawing the charging spine for it and
  // bolting "not instrumented yet" in the corner was the honest M1b-part-1 placeholder; now
  // there are real points to place, so it gets its own diagram.
  if (!tpl.evse) {
    if (site.kind === 'wind_farm') renderWindMimic(site, tpl, svg);
    else if (site.kind === 'microgrid') renderMicrogridMimic(site, tpl, svg);
    else renderGenerationMimic(site, tpl, svg);
    return;
  }

  const isGen = tpl.sign < 0;
  const v = (slot, phase) => valueOf(site.id, slot, phase);
  const V1 = v('voltage', 'L1'), V2 = v('voltage', 'L2'), V3 = v('voltage', 'L3');
  const I1 = v('current', 'L1'), I2 = v('current', 'L2'), I3 = v('current', 'L3');
  const P1 = v('power', 'L1'), P2 = v('power', 'L2'), P3 = v('power', 'L3');
  const F = v('frequency'), PF = v('pf'), T = v('temperature');

  // Generation sites flow the other way: source on the left, grid on the right.
  const parts = [];

  const spine = 130;
  const drawAsset = (x, sym, tag, note, dashed) => {
    const cls = dashed ? 'asset-off' : 'asset';
    parts.push(`<g><rect class="${cls}" x="${x - 26}" y="${spine - 26}" width="52" height="52"/>
      <text class="tag" x="${x}" y="${spine + 4}" text-anchor="middle">${sym}</text>
      <text class="tag" x="${x}" y="${spine + 54}" text-anchor="middle">${tag}</text>
      <text class="note ${dashed ? 'note-absent' : ''}" x="${x}" y="${spine + 76}" text-anchor="middle">${note}</text></g>`);
  };

  // ---- the electrical spine -------------------------------------------------
  parts.push(`<line class="conductor" x1="40" y1="${spine}" x2="340" y2="${spine}"/>`);
  drawAsset(90, '~', 'GRID', 'NEM / DNSP', false);
  drawAsset(180, 'CB1', 'CB1', 'main switch', false);
  drawAsset(265, 'TX1', 'TX1', '22 kV / 400 V', false);

  // busbar
  parts.push(`<line class="busbar" x1="330" y1="${spine}" x2="860" y2="${spine}"/>`);
  parts.push(`<text class="tag" x="600" y="${spine + 26}" text-anchor="middle">BUSBAR 400 V</text>`);

  // ---- the site meter: the instrumented asset -------------------------------
  parts.push(`<line class="conductor" x1="400" y1="${spine}" x2="400" y2="220"/>`);
  parts.push(`<rect class="asset" x="310" y="220" width="350" height="162"/>`);
  parts.push(`<text class="tag" x="326" y="248">M1</text>`);
  parts.push(`<text class="note" x="366" y="248">site meter \u00b7 3-phase \u00b7 ${isGen ? 'export' : 'import'} positive</text>`);

  const rowY = [280, 310, 340];
  const rows = [['L1', V1, I1, P1], ['L2', V2, I2, P2], ['L3', V3, I3, P3]];
  rows.forEach(([name, V, I, P], i) => {
    const y = rowY[i];
    parts.push(`<rect class="phase-dot-${name}" x="326" y="${y - 10}" width="8" height="8"/>`);
    parts.push(`<text class="tag" x="346" y="${y}">${name}</text>`);
    parts.push(`<text x="480" y="${y}" text-anchor="end">${V.html}</text>`);
    parts.push(`<text x="580" y="${y}" text-anchor="end">${I.html}</text>`);
    parts.push(`<text x="650" y="${y}" text-anchor="end">${P.html}</text>`);
  });
  parts.push(`<line class="conductor-thin" x1="326" y1="354" x2="650" y2="354"/>`);
  parts.push(`<text class="note" x="326" y="374">per phase</text>`);
  parts.push(`<text class="note" x="480" y="374" text-anchor="end">V</text>`);
  parts.push(`<text class="note" x="580" y="374" text-anchor="end">A</text>`);
  parts.push(`<text class="note" x="650" y="374" text-anchor="end">kW</text>`);

  // ---- the bay board, or the generation source ------------------------------
  // M2 filled in two boxes hanging off the busbar. Two is not what a depot has: twelve boxes
  // on one row would be unreadable and twelve on two rows would not fit. So each bay is a
  // cell in a table fed from an LV board - the same asset rectangle, the same value markup and
  // the same quality colours as everywhere else on the page, at the type size a cell can
  // carry. Nothing here knows how many bays there are; `evseIdsFor` asks the pipeline.
  if (tpl.evse) {
    const bays = evseIdsFor(site.id);
    const MAX_BAYS = 12;          // two rows of six; beyond that the board would leave the frame
    const COLS = 6;
    const boardY = 412;
    const cellW = 126, cellH = 66, pitch = 136, left = 60;

    parts.push(`<line class="conductor" x1="866" y1="${spine}" x2="866" y2="${boardY}"/>`);
    parts.push(`<line class="busbar" x1="${left}" y1="${boardY}" x2="866" y2="${boardY}"/>`);
    parts.push(`<text class="tag" x="${left}" y="${boardY - 16}">LV BOARD 400 V</text>`);
    // Left of the instrument panel: right-anchored, this line lands on top of it.
    parts.push(`<text class="note" x="320" y="${boardY - 16}">` +
      `${bays.length} bay${bays.length === 1 ? '' : 's'} \u00b7 OCPP 1.6J</text>`);
    // The column legend is a caption under the board, once, instead of in every cell: a bay
    // cell has 126 units of width and the sag story needs the offered current to fit in it.
    parts.push(`<text class="note" x="${left}" y="${boardY + 26 + Math.ceil(Math.min(bays.length, MAX_BAYS) / COLS) * (cellH + 8)}">` +
      `per bay: kW \u00b7 A offered \u00b7 session kWh \u00b7 one trace per session</text>`);

    bays.slice(0, MAX_BAYS).forEach((tag, i) => {
      const x = left + (i % COLS) * pitch;
      const y = boardY + 18 + Math.floor(i / COLS) * (cellH + 8);
      const P = valueOf(site.id, 'evse_power', null, { evse: tag });
      const A = rawOf(site.id, 'evse_offered', null, { evse: tag });
      const E = rawOf(site.id, 'session_energy', null, { evse: tag });
      const S = bound(site.id, 'evse_state', null, { evse: tag });
      const q = S ? pointState(S) : 'unknown';
      // The state word comes from the edge collector, not from a code->word table here.
      const state = S ? (S.labels[S.def.text] || fmt(S.value, 0)) : 'no data';
      parts.push(`<line class="conductor-thin" x1="${x + cellW / 2}" y1="${boardY}" x2="${x + cellW / 2}" y2="${y}"/>`);
      parts.push(`<g class="bay">`);
      parts.push(`<rect class="${S && q === 'good' ? 'asset' : 'asset-off'}" x="${x}" y="${y}" width="${cellW}" height="${cellH}"/>`);
      parts.push(`<text class="bay-tag" x="${x + 9}" y="${y + 18}">${tag}</text>`);
      parts.push(`<text class="bay-state q-${q}" x="${x + cellW - 9}" y="${y + 18}" text-anchor="end">${state}</text>`);
      parts.push(`<text x="${x + 9}" y="${y + 41}">${P.html}</text>`);
      // Terse on purpose: 126 units of width, and "3.011 kWh" already fills a third of it.
      parts.push(`<text class="bay-small" x="${x + 9}" y="${y + 58}">${A.html}A \u00b7 ${E.html}kWh</text>`);
      parts.push(`</g>`);
    });
    if (bays.length > MAX_BAYS) {
      parts.push(`<text class="note note-absent" x="866" y="${boardY + 26 + 2 * (cellH + 8)}" text-anchor="end">` +
        `${bays.length - MAX_BAYS} more bays not drawn</text>`);
    }
  }

  // ---- the boundary the whole repo is about ---------------------------------
  parts.push(`<line class="conductor" x1="860" y1="${spine}" x2="910" y2="${spine}"/>`);
  parts.push(`<line x1="890" y1="50" x2="890" y2="570" stroke="#7d8981" stroke-width="1" stroke-dasharray="7 6"/>`);
  parts.push(`<text class="note note-absent" x="898" y="68">OT zone boundary</text>`);
  parts.push(`<rect class="asset" x="910" y="86" width="160" height="104"/>`);
  parts.push(`<text class="tag" x="990" y="120" text-anchor="middle">EDGE</text>`);
  parts.push(`<text class="tag" x="990" y="144" text-anchor="middle">COLLECTOR</text>`);
  parts.push(`<text class="note" x="990" y="172" text-anchor="middle">OTLP \u2192 hub</text>`);
  parts.push(`<text class="note" x="990" y="216" text-anchor="middle">over the impaired link</text>`);

  // ---- the instrument strip -------------------------------------------------
  // Four site-meter quantities that are not per phase, so they do not belong in the phase
  // table above. The M2 layout put them in the band the bay board now needs; they move to a
  // panel of their own right of the meter, which is also where a reader looks for them.
  const strip = [
    ['f', F, 'system frequency'],
    ['PF', PF, 'displacement factor'],
    // A lifetime register in kWh does not fit a 160-wide panel (and 42 917 342 kWh is
    // unreadable in place); GWh is the unit this is read in.
    ['E', { html: gwhText(bound(site.id, 'energy', null, { direction: 'import' })) },
      'import register'],
    ['\u03b8', T, 'enclosure temperature'],
  ];
  parts.push(`<rect class="asset" x="690" y="212" width="160" height="176"/>`);
  parts.push(`<text class="note" x="690" y="202">site meter \u00b7 not per phase</text>`);
  strip.forEach(([sym, val, note], i) => {
    const y = 246 + i * 40;
    parts.push(`<text class="tag" x="704" y="${y}">${sym}</text>`);
    parts.push(`<text x="836" y="${y}" text-anchor="end">${val.html}</text>`);
    parts.push(`<text class="bay-small" x="704" y="${y + 13}">${note}</text>`);
  });

  svg.innerHTML = parts.join('');
}

/* A generation one-line: PV arrays or wind turbines -> MPPT trackers -> central inverter ->
   step-up transformer -> grid. Every live value comes from `valueOf`, i.e. from Prometheus;
   anything with no instrumented point is drawn dashed and named, never guessed. */
function renderGenerationMimic(site, tpl, svg) {
  const v = (slot, phase, opts) => valueOf(site.id, slot, phase, opts);
  const parts = [];
  const spine = 130;

  const drawAsset = (x, sym, tag, note, dashed) => {
    const cls = dashed ? 'asset-off' : 'asset';
    parts.push(`<g><rect class="${cls}" x="${x - 26}" y="${spine - 26}" width="52" height="52"/>
      <text class="tag" x="${x}" y="${spine + 4}" text-anchor="middle">${sym}</text>
      <text class="tag" x="${x}" y="${spine + 54}" text-anchor="middle">${tag}</text>
      <text class="note ${dashed ? 'note-absent' : ''}" x="${x}" y="${spine + 74}" text-anchor="middle">${note}</text></g>`);
  };

  // ---- AC side: source -> inverter -> transformer -> grid ----
  parts.push(`<line class="conductor" x1="44" y1="${spine}" x2="640" y2="${spine}"/>`);
  drawAsset(90, '~', 'GRID', 'NEM / DNSP', false);
  drawAsset(205, 'TX1', 'TX1', tpl.tx_note || '0.69 / 22 kV', false);
  drawAsset(320, 'M1', 'M1', 'revenue meter', true);
  parts.push(`<line class="busbar" x1="360" y1="${spine}" x2="640" y2="${spine}"/>`);
  parts.push(`<text class="tag" x="500" y="${spine + 26}" text-anchor="middle">BUSBAR 0.69 kV</text>`);
  drawAsset(600, 'INV1', 'INV1', 'central inverter', false);

  // ---- DC side: trackers, then the arrays themselves (un-instrumented) ----
  parts.push(`<line class="conductor" x1="640" y1="${spine}" x2="860" y2="${spine}"/>`);
  drawAsset(700, 'MPPT', 'MPPT1', 'tracker 1', false);
  drawAsset(790, 'MPPT', 'MPPT2', 'tracker 2', false);
  parts.push(`<line class="conductor-thin" x1="860" y1="${spine}" x2="890" y2="${spine}"/>`);
  parts.push(`<rect class="asset-off" x="890" y="${spine - 22}" width="44" height="44"/>`);
  const isWind = site.kind === 'wind_farm';
  parts.push(`<text class="tag" x="912" y="${spine + 4}" text-anchor="middle">${isWind ? 'WTG' : 'PV'}</text>`);
  parts.push(`<text class="note note-absent" x="912" y="${spine + 46}" text-anchor="middle">${isWind ? 'turbines' : 'arrays'}</text>`);

  // ---- AC measurement panel (INV1 terminals) ----
  // Model 103 reports per-phase AC voltage and current but only TOTAL real power (there is
  // no per-phase W in the standard model), so the diagram shows what exists rather than
  // multiplying V by I itself and calling the result a measurement.
  parts.push(`<line class="conductor" x1="440" y1="${spine}" x2="440" y2="220"/>`);
  parts.push(`<rect class="asset" x="340" y="220" width="300" height="205"/>`);
  parts.push(`<text class="tag" x="356" y="245">AC \u00b7 INV1 terminals</text>`);
  parts.push(`<text class="note" x="356" y="264">3-phase \u00b7 export positive \u00b7 V / A</text>`);
  const rows = [
    ['L1', v('voltage', 'L1'), v('current', 'L1')],
    ['L2', v('voltage', 'L2'), v('current', 'L2')],
    ['L3', v('voltage', 'L3'), v('current', 'L3')],
  ];
  rows.forEach(([name, V, I], i) => {
    const y = 294 + i * 26;
    parts.push(`<rect class="phase-dot-${name}" x="356" y="${y - 10}" width="8" height="8"/>`);
    parts.push(`<text class="tag" x="376" y="${y}">${name}</text>`);
    parts.push(`<text x="500" y="${y}" text-anchor="end">${V.html}</text>`);
    parts.push(`<text x="628" y="${y}" text-anchor="end">${I.html}</text>`);
  });
  const VLL = v('line_voltage');
  parts.push(`<line class="conductor-thin" x1="356" y1="366" x2="628" y2="366"/>`);
  parts.push(`<text class="note" x="356" y="386">L-L</text>`);
  parts.push(`<text x="500" y="386" text-anchor="end">${VLL.html}</text>`);
  parts.push(`<text class="tag" x="356" y="410">P</text>`);
  parts.push(`<text x="628" y="410" text-anchor="end">${v('power').html}</text>`);

  // ---- DC measurement panel (per tracker and aggregate) ----
  parts.push(`<line class="conductor" x1="720" y1="${spine}" x2="720" y2="220"/>`);
  parts.push(`<rect class="asset" x="660" y="220" width="300" height="205"/>`);
  parts.push(`<text class="tag" x="676" y="245">DC \u00b7 MPPT trackers</text>`);
  parts.push(`<text class="note" x="676" y="264">per tracker, then the DC bus</text>`);
  parts.push(`<text class="note" x="806" y="282" text-anchor="end">kW</text>`);
  parts.push(`<text class="note" x="874" y="282" text-anchor="end">V</text>`);
  parts.push(`<text class="note" x="948" y="282" text-anchor="end">A</text>`);
  const mpptRows = [
    ['MPPT1', '1'], ['MPPT2', '2'],
  ].map(([name, inst]) => [name,
    rawOf(site.id, 'mppt_power', null, { mppt: inst }),
    rawOf(site.id, 'mppt_voltage', null, { mppt: inst }),
    rawOf(site.id, 'mppt_current', null, { mppt: inst })]);
  mpptRows.forEach(([name, P, V, A], i) => {
    const y = 304 + i * 24;
    parts.push(`<text class="tag" x="676" y="${y}">${name}</text>`);
    parts.push(`<text x="806" y="${y}" text-anchor="end">${P.html}</text>`);
    parts.push(`<text x="874" y="${y}" text-anchor="end">${V.html}</text>`);
    parts.push(`<text x="948" y="${y}" text-anchor="end">${A.html}</text>`);
  });
  parts.push(`<line class="conductor-thin" x1="676" y1="346" x2="948" y2="346"/>`);
  parts.push(`<text class="note" x="676" y="366">DC bus</text>`);
  parts.push(`<text x="806" y="366" text-anchor="end">${rawOf(site.id, 'dc_power').html}</text>`);
  parts.push(`<text x="874" y="366" text-anchor="end">${rawOf(site.id, 'dc_voltage').html}</text>`);
  parts.push(`<text x="948" y="366" text-anchor="end">${rawOf(site.id, 'dc_current').html}</text>`);
  parts.push(`<text class="note" x="676" y="396">DC energy</text>`);
  parts.push(`<text x="948" y="396" text-anchor="end">${gwhText(bound(site.id, 'dc_energy'))}</text>`);

  // ---- the boundary the whole repo is about ----
  parts.push(`<line x1="1006" y1="40" x2="1006" y2="600" stroke="#7d8981" stroke-width="1" stroke-dasharray="7 6"/>`);
  parts.push(`<rect class="asset" x="1016" y="70" width="92" height="104"/>`);
  parts.push(`<text class="tag" x="1062" y="104" text-anchor="middle">EDGE</text>`);
  parts.push(`<text class="tag" x="1062" y="126" text-anchor="middle">COLLECTOR</text>`);
  parts.push(`<text class="note" x="1062" y="152" text-anchor="middle">OTLP \u2192 hub</text>`);

  // ---- weather pair, the first thing a PV engineer checks ----
  parts.push(`<text class="tag" x="60" y="440">WEATHER</text>`);
  const weather = [
    ['POA', v('irradiance', null, { plane: 'plane_of_array' }), 'plane of array'],
    ['GHI', v('irradiance', null, { plane: 'horizontal' }), 'global horizontal'],
    ['Tmod', v('module_temp'), 'back-of-module'],
    ['Tamb', v('ambient_temp'), 'ambient air'],
    ['Tcab', v('cabinet_temp'), 'inverter cabinet'],
    ['Tsnk', v('heatsink_temp'), 'inverter heatsink'],
  ];
  weather.forEach(([sym, val, note], i) => {
    const y = 466 + i * 22;
    parts.push(`<text class="tag" x="60" y="${y}">${sym}</text>`);
    parts.push(`<text x="130" y="${y}">${val.html}</text>`);
    parts.push(`<text class="note" x="250" y="${y}">${note}</text>`);
  });

  // ---- state: what the plant is doing and why ----
  parts.push(`<text class="tag" x="620" y="440">STATE</text>`);
  const state = [
    ['f', v('frequency').html, 'grid frequency'],
    ['PF', v('pf').html, 'displacement factor'],
    ['S', v('power_s').html, 'apparent power'],
    ['Q', v('power_q').html, 'reactive power'],
    ['E', gwhText(bound(site.id, 'energy')), 'lifetime AC energy'],
    ['St', textOf(site.id, 'status').html, 'SunSpec operating state'],
    ['Cur', textOf(site.id, 'curtailment').html, 'why output is held down'],
    ['Lim', v('export_limit').html, 'export limit'],
  ];
  state.forEach(([sym, html, note], i) => {
    const y = 462 + i * 20;
    parts.push(`<text class="tag" x="620" y="${y}">${sym}</text>`);
    parts.push(`<text x="676" y="${y}">${html}</text>`);
    parts.push(`<text class="note" x="860" y="${y}">${note}</text>`);
  });

  svg.innerHTML = parts.join('');
}

/* A wind farm one-line: turbines -> collector bus -> step-up -> grid. Six machines are drawn
   individually because the interesting fact is that they disagree; a single farm number would
   hide the regime the second generation site exists to show (D17). */
function renderWindMimic(site, tpl, svg) {
  const v = (slot, phase, opts) => valueOf(site.id, slot, phase, opts);
  const parts = [];
  const spine = 120;

  const drawAsset = (x, sym, tag, note, dashed) => {
    const cls = dashed ? 'asset-off' : 'asset';
    parts.push(`<g><rect class="${cls}" x="${x - 26}" y="${spine - 26}" width="52" height="52"/>
      <text class="tag" x="${x}" y="${spine + 4}" text-anchor="middle">${sym}</text>
      <text class="tag" x="${x}" y="${spine + 54}" text-anchor="middle">${tag}</text>
      <text class="note ${dashed ? 'note-absent' : ''}" x="${x}" y="${spine + 74}" text-anchor="middle">${note}</text></g>`);
  };

  parts.push(`<line class="conductor" x1="44" y1="${spine}" x2="720" y2="${spine}"/>`);
  drawAsset(90, '~', 'GRID', 'NEM / DNSP', false);
  drawAsset(200, 'TX1', 'TX1', tpl.tx_note || '0.69 / 22 kV', false);
  drawAsset(310, 'M1', 'M1', 'revenue meter', true);
  parts.push(`<line class="busbar" x1="350" y1="${spine}" x2="720" y2="${spine}"/>`);
  parts.push(`<text class="tag" x="530" y="${spine + 26}" text-anchor="middle">COLLECTOR 33 kV</text>`);
  drawAsset(680, 'FC', 'FARM', 'collector / SCADA', false);

  // ---- the turbine row ----
  const turbines = ['1', '2', '3', '4', '5', '6'].map((n) => ({
    n,
    p: rawOf(site.id, 'turbine_power', null, { turbine: n }),
    ws: rawOf(site.id, 'turbine_wind', null, { turbine: n }),
  }));
  const hubY = 300;
  const busY = 374;
  parts.push(`<line class="conductor-thin" x1="90" y1="${busY}" x2="720" y2="${busY}"/>`);
  turbines.forEach((t, i) => {
    const x = 120 + i * 110;
    parts.push(`<line x1="${x}" y1="${hubY}" x2="${x}" y2="${busY}" stroke="var(--ink)" stroke-width="1.6"/>`);
    parts.push(`<circle cx="${x}" cy="${hubY}" r="2.6" fill="var(--ink)"/>`);
    parts.push(`<line x1="${x}" y1="${hubY}" x2="${x}" y2="${hubY - 18}" stroke="var(--ink)" stroke-width="1.6"/>`);
    parts.push(`<line x1="${x}" y1="${hubY}" x2="${x - 15}" y2="${hubY + 9}" stroke="var(--ink)" stroke-width="1.6"/>`);
    parts.push(`<line x1="${x}" y1="${hubY}" x2="${x + 15}" y2="${hubY + 9}" stroke="var(--ink)" stroke-width="1.6"/>`);
    parts.push(`<text class="tag" x="${x}" y="${hubY - 30}" text-anchor="middle">WTG${t.n}</text>`);
    parts.push(`<text x="${x}" y="${busY + 20}" text-anchor="middle">${t.p.html}</text>`);
    parts.push(`<text class="note" x="${x}" y="${busY + 38}" text-anchor="middle">${t.ws.html} wind</text>`);
  });

  // ---- the boundary the whole repo is about ----
  parts.push(`<line x1="1006" y1="40" x2="1006" y2="600" stroke="#7d8981" stroke-width="1" stroke-dasharray="7 6"/>`);
  parts.push(`<rect class="asset" x="1016" y="70" width="92" height="104"/>`);
  parts.push(`<text class="tag" x="1062" y="104" text-anchor="middle">EDGE</text>`);
  parts.push(`<text class="tag" x="1062" y="126" text-anchor="middle">COLLECTOR</text>`);
  parts.push(`<text class="note" x="1062" y="152" text-anchor="middle">OTLP \u2192 hub</text>`);

  // ---- farm aggregate at the point of connection ----
  parts.push(`<text class="tag" x="60" y="440">FARM</text>`);
  const farmRows = [
    ['P', v('power').html, 'real power at the connection point'],
    ['Q', v('power_q').html, 'reactive power'],
    ['f', v('frequency').html, 'grid frequency'],
    ['E', gwhText(bound(site.id, 'energy')), 'lifetime energy'],
    ['Avail', v('availability').html, 'turbines available'],
    ['Lim', v('export_limit').html, 'export limit'],
  ];
  farmRows.forEach(([sym, html, note], i) => {
    const y = 466 + i * 22;
    parts.push(`<text class="tag" x="60" y="${y}">${sym}</text>`);
    parts.push(`<text x="130" y="${y}">${html}</text>`);
    parts.push(`<text class="note" x="290" y="${y}">${note}</text>`);
  });

  // ---- met mast and farm state ----
  parts.push(`<text class="tag" x="620" y="440">MET MAST & STATE</text>`);
  const mastRows = [
    ['ws', v('wind_speed').html, 'mean wind across turbines'],
    ['dir', v('wind_direction').html, 'wind direction'],
    ['Tamb', v('ambient_temp').html, 'ambient temperature'],
    ['St', textOf(site.id, 'status').html, 'farm operating state'],
    ['Cur', textOf(site.id, 'curtailment').html, 'why output is held down'],
    ['Flt', v('fault_code').html, 'farm fault code'],
  ];
  mastRows.forEach(([sym, html, note], i) => {
    const y = 466 + i * 22;
    parts.push(`<text class="tag" x="620" y="${y}">${sym}</text>`);
    parts.push(`<text x="680" y="${y}">${html}</text>`);
    parts.push(`<text class="note" x="840" y="${y}">${note}</text>`);
  });

  svg.innerHTML = parts.join('');
}

/* ------------------------------------------------------------------ point list */

/* The microgrid one-line: sources -> busbar -> the grid former -> load.
   The distinction the issue asks for is drawn, not annotated: the BESS is the grid former and
   is drawn with a heavy outline and the words "grid-forming"; the two sources are grid-following
   and are drawn as ordinary boxes. Anything the pipeline does not instrument is dashed. */
function renderMicrogridMimic(site, tpl, svg) {
  const v = (slot, opts) => valueOf(site.id, slot, null, opts);
  const src = (name) => ({
    power: valueOf(site.id, 'source_power', null, { source: name }),
  });
  const solar = src('solar');
  const wind = src('wind');
  const F = v('frequency'), ROCOF = v('rocof'), VB = v('voltage');
  const SOC = v('soc'), PB = v('bess_power'), HOLD = textOf(site.id, 'hold_reason');
  const BST = textOf(site.id, 'bess_status'), BT = v('temperature');
  const LOAD = v('load_power'), EV = v('ev_power'), BAL = v('balance');

  const busY = 200;
  const parts = [];

  const box = (x, y, w, h, cls) => parts.push(`<rect class="${cls}" x="${x}" y="${y}" width="${w}" height="${h}"/>`);
  const label = (x, y, text, cls = 'tag', anchor = 'start') =>
    parts.push(`<text class="${cls}" x="${x}" y="${y}" text-anchor="${anchor}">${text}</text>`);

  // --- sources (grid-following: they take the busbar's frequency) ------------
  const source = (x, tag, note, S) => {
    box(x, 86, 190, 84, 'asset');
    label(x + 12, 110, tag);
    label(x + 12, 130, note, 'note');
    parts.push(`<text x="${x + 178}" y="158" text-anchor="end">${S.power.html}</text>`);
    parts.push(`<line class="conductor" x1="${x + 95}" y1="170" x2="${x + 95}" y2="${busY}"/>`);
  };
  source(40, 'PV1', 'solar \u00b7 grid-following', solar);
  source(250, 'WTG1', 'wind \u00b7 grid-following', wind);

  // --- the busbar: the only thing tying the island together -----------------
  parts.push(`<line class="busbar" x1="40" y1="${busY}" x2="1050" y2="${busY}"/>`);
  label(545, busY - 12, 'BUSBAR 400 V \u00b7 50 Hz nominal', 'tag', 'middle');
  parts.push(`<text x="545" y="${busY + 26}" text-anchor="middle">${F.html} \u00b7 RoCoF ${ROCOF.html} \u00b7 ${VB.html}</text>`);

  // --- the grid former: the BESS, drawn differently on purpose --------------
  box(40, 300, 260, 150, 'asset asset-gridformer');
  label(56, 326, 'BESS1 \u00b7 grid-forming');
  label(56, 348, 'it sets the voltage and the frequency', 'note');
  parts.push(`<line class="conductor" x1="170" y1="${busY}" x2="170" y2="300"/>`);
  parts.push(`<text x="56" y="378">SoC ${SOC.html}</text>`);
  parts.push(`<text x="56" y="404">P ${PB.html}</text>`);
  label(200, 404, 'discharge positive', 'note');
  parts.push(`<text x="56" y="430">${BST.html} \u00b7 hold ${HOLD.html}</text>`);
  label(56, 490, `battery ${BT.html}`, 'note');

  // --- the load: the EV block is the step the grid former has to survive ----
  box(700, 300, 250, 150, 'asset');
  label(716, 326, 'SITE LOAD');
  label(716, 348, 'incl. EV charging', 'note');
  parts.push(`<line class="conductor" x1="825" y1="${busY}" x2="825" y2="300"/>`);
  parts.push(`<text x="716" y="382">load ${LOAD.html}</text>`);
  parts.push(`<text x="716" y="408">of which EV ${EV.html}</text>`);
  parts.push(`<text x="716" y="434">renewables \u2212 load ${BAL.html}</text>`);

  // --- not instrumented, and drawn as such ---------------------------------
  box(1000, 300, 60, 150, 'asset-off');
  label(1030, 330, 'CB', 'tag', 'middle');
  label(1030, 352, 'none', 'note', 'middle');
  label(1030, 370, 'drawn', 'note', 'middle');
  label(1030, 388, 'dashed', 'note', 'middle');
  label(1030, 406, 'not', 'note', 'middle');
  label(1030, 424, 'modelled', 'note', 'middle');

  label(40, 560, 'Sources are grid-following: they follow what the busbar does. The battery is grid-forming: it decides. That is why frequency moves here and the voltage hardly does.', 'note');
  label(40, 580, 'No transformer, no connection point, no export: there is nowhere to sell this energy, so surplus is curtailed and deficit sheds load.', 'note');

  svg.innerHTML = parts.join('');
}

function renderPoints(site) {
  const pts = pointsFor(site.id);
  const body = document.getElementById('points');
  if (!pts.length) {
    body.innerHTML = `<tr class="is-absent"><td colspan="5">No instrumented points from this site yet.</td></tr>`;
    return;
  }
  const order = Object.keys(POINTS);
  pts.sort((a, b) => {
    const d = order.indexOf(a.metric) - order.indexOf(b.metric);
    return d !== 0 ? d : String(a.phase).localeCompare(String(b.phase));
  });

  body.innerHTML = pts.map((p) => {
    const q = pointState(p);
    const lag = lagS(p);
    // "unchanged" rather than "stale": a deadbanded register is quiet on purpose (D22).
    const unchanged = lag > LIVE_AGE_S ? `${fmtAge(lag)} unchanged` : 'now';
    const name = p.def.label
      + (p.evse ? ` ${p.evse}` : '')
      + (p.phase ? ` ${p.phase}` : '')
      + (p.mppt ? ` MPPT ${p.mppt}` : '')
      + (p.turbine ? ` WTG ${p.turbine}` : '')
      + (p.plane ? ` (${p.plane.replace(/_/g, ' ')})` : '')
      + (p.source ? ` (${p.source})` : '')
      + (p.direction ? ` (${p.direction})` : '');
    return `<tr>
      <td>${name}<br><span class="note">${p.def.otel}${p.direction ? '.' + p.direction : ''}</span></td>
      <td class="num">${fmt(p.value, p.def.decimals)}</td>
      <td class="unit">${p.def.unit}</td>
      <td class="q q-${q}">${q}</td>
      <td class="age">${unchanged}</td>
    </tr>`;
  }).join('');
}

/* ------------------------------------------------------------------ trend */

function renderTrend(site) {
  const svg = document.getElementById('trend');
  const note = document.getElementById('trend-note');

  if (!state.trend || !state.trend.length) {
    svg.innerHTML = '';
    svg.setAttribute('viewBox', '0 0 540 210');
    note.textContent = 'No voltage point from this site, so no trend to draw. Nothing is fabricated to fill the panel.';
    return;
  }

  const W = 540, H = 210, L = 50, R = 12, T = 20, B = 32;
  const rows = state.trend;
  const stepMs = 5000;
  // The band is a nominal-voltage band, and a generation site sits at 690 V L-L (398 V L-N),
  // not 230 V. Hard-coding the LV domestic band would draw every healthy inverter reading as
  // a violation, which is worse than drawing no band at all.
  const tpl = TEMPLATES[site.kind] || {};
  const isFreq = tpl.trend === 'frequency';
  document.getElementById('trend-title').textContent = isFreq ? 'Frequency, 15 minutes' : 'Voltage, 15 minutes';
  const vNom = tpl.v_nominal || (tpl.evse === false ? 398 : 230);
  // The band means "normal", and normal is a different question per site: ±6%/+10% voltage
  // for a distribution site, and the NEM's 49.85-50.15 Hz normal operating band for a
  // microgrid. Drawing a voltage band over a frequency axis would be a lie with a colour.
  const vBand = isFreq ? [49.85, 50.15] : [vNom * 0.94, vNom * 1.10];
  const t0 = Math.min(...rows.map((r) => r.values[0][0])) * 1000;
  const t1 = Math.max(...rows.map((r) => r.values[r.values.length - 1][0])) * 1000;

  const vals = rows.flatMap((r) => r.values.map((v) => Number(v[1])));
  let lo = Math.min(...vals, vBand[0]);
  let hi = Math.max(...vals, vBand[1]);
  const margin = Math.max(2, (hi - lo) * 0.12);
  lo -= margin; hi += margin;

  const px = (t) => L + ((t - t0) / Math.max(1, t1 - t0)) * (W - L - R);
  const py = (v) => T + (1 - (v - lo) / Math.max(1e-9, hi - lo)) * (H - T - B);

  const parts = [];
  parts.push(`<rect class="trend-band" x="${L}" y="${py(vBand[1]).toFixed(1)}" width="${(W - L - R).toFixed(1)}" height="${(py(vBand[0]) - py(vBand[1])).toFixed(1)}"/>`);
  for (let i = 0; i <= 3; i++) {
    const v = lo + ((hi - lo) * i) / 3;
    parts.push(`<line class="trend-grid" x1="${L}" y1="${py(v).toFixed(1)}" x2="${W - R}" y2="${py(v).toFixed(1)}"/>`);
    parts.push(`<text class="trend-axis" x="${L - 6}" y="${(py(v) + 4).toFixed(1)}" text-anchor="end">${v.toFixed(isFreq ? 2 : 0)}</text>`);
  }
  parts.push(`<rect class="trend-frame" x="${L}" y="${T}" width="${W - L - R}" height="${H - T - B}"/>`);
  parts.push(`<text class="trend-axis" x="${W - R}" y="${T - 2}" text-anchor="end">${isFreq ? 'Hz' : 'V'}</text>`);

  const colour = { L1: 'var(--l1)', L2: 'var(--l2)', L3: 'var(--l3)' };
  const palette = ['#2f6f4f', '#4f7fae', '#b07a2f', '#8a5a9e', '#3f8f8f', '#a04a4a'];
  const labelOf = (m) => m.energy_phase
    || (m.energy_turbine ? `WTG${m.energy_turbine}` : null)
    || (isFreq ? 'busbar' : null)
    || '\u2014';
  const strokeOf = (m) => colour[m.energy_phase]
    || palette[(Number(m.energy_turbine) || 1) - 1]
    || 'var(--ink)';
  for (const row of rows) {
    let d = '';
    let prevT = null;
    for (const [ts, v] of row.values) {
      const t = ts * 1000;
      // A gap in the data is a gap in the line. Two samples more than three steps apart
      // are two separate segments - never joined by a straight line.
      const broke = prevT !== null && (t - prevT) > stepMs * 3;
      d += `${d === '' || broke ? 'M' : 'L'}${px(t).toFixed(1)} ${py(Number(v)).toFixed(1)} `;
      prevT = t;
    }
    const stroke = strokeOf(row.metric);
    parts.push(`<path class="trend-line" d="${d.trim()}" stroke="${stroke}"/>`);
    const last = row.values[row.values.length - 1];
    parts.push(`<circle cx="${px(last[0] * 1000).toFixed(1)}" cy="${py(Number(last[1])).toFixed(1)}" r="2.5" fill="${stroke}"/>`);
  }

  const leg = rows.map((r) => `${labelOf(r.metric)} ${fmt(Number(r.values[r.values.length - 1][1]), isFreq ? 3 : 1)} ${isFreq ? 'Hz' : 'V'}`).join('   ');
  parts.push(`<text class="trend-axis" x="${L}" y="${H - 8}">${leg}</text>`);
  parts.push(`<text class="trend-axis" x="${W - R}" y="${H - 8}" text-anchor="end">15 min, 5 s steps</text>`);

  svg.innerHTML = parts.join('');
  note.textContent = isFreq
    ? `Shaded band is the NEM normal operating band (49.85\u201350.15 Hz). This site is islanded: frequency is set by the grid-forming battery, so it moves with the load. A break in the line is missing data, not a straight-line guess.`
    : `Shaded band is the AS 60038 utilisation range (0.94\u20131.10 pu of ${vNom} V phase-neutral = ${vBand[0].toFixed(0)}\u2013${vBand[1].toFixed(0)} V). One series per phase; a break in a line is missing data, not a straight-line guess.`;
}

/* ------------------------------------------------------------------ views */

function render() {
  if (state.error) {
    setPill('bad', 'pipeline unreachable');
  } else {
    const anyLive = [...state.sites.keys()].some((id) => siteState(id) === 'good');
    const count = state.points.size;
    setPill(anyLive ? 'live' : 'stale', anyLive ? `${count} series live` : 'no live data');
  }

  if (!state.selected) {
    drawMarkers();
  } else {
    const site = state.sites.get(state.selected);
    if (!site) { state.selected = null; render(); return; }
    renderMimic(site);
    renderPoints(site);
    renderTrend(site);
  }
}

function setPill(cls, text) {
  const el = document.getElementById('link-state');
  el.className = `pill pill-${cls === 'live' ? 'live' : cls === 'stale' ? 'stale' : 'bad'}`;
  el.textContent = text;
}

async function openSite(id) {
  state.selected = id;
  const site = state.sites.get(id);
  document.getElementById('view-map').hidden = true;
  document.getElementById('view-site').hidden = false;
  document.getElementById('tab-map').classList.remove('is-on');
  document.getElementById('tab-site').disabled = false;
  document.getElementById('tab-site').classList.add('is-on');

  document.getElementById('site-name').textContent = site.name;
  const tpl = TEMPLATES[site.kind];
  document.getElementById('site-meta').textContent =
    `${site.id}  \u00b7  ${site.kind.replace(/_/g, ' ')}  \u00b7  ${site.lat.toFixed(4)}, ${site.lon.toFixed(4)}`;
  document.getElementById('site-note').textContent = tpl
    ? `Values below are read from the local metrics store, which the hub feeds over OTLP.`
    : `Unknown site kind; the mimic does not guess a topology.`;

  await loadTrend(id);
  render();
}

async function loadTrend(id) {
  const site = state.sites.get(id);
  const tpl = site ? TEMPLATES[site.kind] : null;
  if (tpl && tpl.trend === 'frequency') {
    // This site's story is frequency, and one series is the whole story. Voltage is on the
    // one-line and in the point table; a voltage trend here would show a flat line and teach
    // the reader nothing.
    try {
      // One logical signal, so collapse the series a poller restart mints (a new
      // service.instance.id is a new series - docs/findings.md #12). Without this the legend
      // draws the same frequency three times, which looks like three measurements.
      state.trend = await prange(
        `max without (instance) (energy_microgrid_frequency_hertz{energy_site_id="${id}"})`, 900, 5);
    } catch { state.trend = []; }
    return;
  }
  try {
    // The inverter measures its own AC voltage, so a generation site has no `energy_meter_
    // voltage`; match both rather than special-casing the site here.
    state.trend = await prange(
      `{__name__=~"energy_(meter|inverter|turbine)_voltage_volts",energy_site_id="${id}",energy_phase=~"L1|L2|L3"}`, 900, 5);
  } catch { state.trend = []; }
  // A wind farm's per-turbine voltage has no phase label, so the phase filter above returns
  // nothing for it. Fall back to the turbines rather than showing an empty panel.
  if (state.trend.length === 0) {
    try {
      state.trend = await prange(
        `energy_turbine_voltage_volts{energy_site_id="${id}"}`, 900, 5);
    } catch { state.trend = []; }
  }
}

function backToMap() {
  state.selected = null;
  document.getElementById('view-map').hidden = false;
  document.getElementById('view-site').hidden = true;
  document.getElementById('tab-site').classList.remove('is-on');
  document.getElementById('tab-site').disabled = true;
  document.getElementById('tab-map').classList.add('is-on');
  render();
}

/* ------------------------------------------------------------------ boot */

async function boot() {
  const res = await fetch('data/australia.geo.json');
  state.geography = await res.json();
  drawMapBase();

  document.getElementById('back').addEventListener('click', backToMap);
  document.getElementById('tab-map').addEventListener('click', backToMap);

  await refresh();
  // The trend is a range query and has no business running every two seconds.
  setInterval(() => { if (state.selected) loadTrend(state.selected).then(render); }, 30000);
  setInterval(refresh, POLL_MS);
}

boot();
