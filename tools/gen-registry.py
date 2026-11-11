"""Draft the Weaver registry from the edge config + the application instruments.

The registry is an authored artefact; this script is how the first draft was produced so that
names, units and descriptions could not be mistyped from a doc. After that, schema/registry/ is
the source of truth and `make registry-check` enforces the two-way correspondence.
"""
import re, yaml, collections

cfg = open('collector/edge/site.yaml').read()

# --- metric name / unit / description triples out of the OTTL statements ---------------
names = re.findall(r'- set\(metric\.name, "(energy\.[a-z0-9_.]+)"\)', cfg)
metrics = sorted(set(names))

units = {}
for m in re.finditer(r'- set\(metric\.unit, "([^"]+)"\)\s+where\s+(.+)$', cfg, re.M):
    unit, where = m.group(1), m.group(2).strip()
    for n in re.findall(r'"(energy\.[a-z0-9_.]+)"', where):
        units.setdefault(n, unit)
    # Most of the edge's unit statements are IsMatch(name, "^energy\.inverter\.(current|...)$"),
    # not name == "...". Missing that form wrote `unit: "1"` on 47 metrics whose values are V,
    # A, Cel, kW.h and so on - invisible to the build-time gate, which compares metric *names*,
    # and caught by `make registry-live` (#27) as 452 unit_mismatch findings on the first run.
    is_match = re.match(r'IsMatch\(metric\.name, "(.+)"\)$', where)
    if is_match:
        rx = re.compile(is_match.group(1).replace('\\\\.', '.'))
        for n in metrics:
            if rx.search(n):
                units.setdefault(n, unit)
descs = {}
for m in re.finditer(r'- set\(metric\.description, "([^"]+)"\)\s+where\s+metric\.name == "(energy\.[a-z0-9_.]+)"', cfg):
    descs[m.group(2)] = m.group(1)
# regex-based descriptions apply to a family: approximate by prefix
for m in re.finditer(r'- set\(metric\.description, "([^"]+)"\)\s+where\s+IsMatch\(metric\.name, "([^"]+)"\)', cfg):
    pass

# --- application instruments (charge point, CSMS, market feed), parsed from the source -----
app: dict[str, tuple[str, str, str]] = {}

def _parse_app_instruments(path: str) -> None:
    """name -> (instrument, unit, description) out of create_*(...) calls, so a new metric in an
    application cannot be missing from the registry (the check would catch it, but generating it
    correctly is better than failing on it)."""
    text = open(path).read()
    for m in re.finditer(
            r'create_(observable_gauge|counter|histogram|up_down_counter)\(\s*"([a-z][a-z0-9_.]+)"',
            text):
        kind, name = m.group(1), m.group(2)
        tail = text[m.end():m.end() + 600]
        unit = re.search(r'unit="([^"]+)"', tail)
        desc = re.search(r'description="([^"]+)"|description=\(\s*"([^"]+)"', tail)
        instrument = {"observable_gauge": "gauge", "counter": "counter",
                      "histogram": "histogram", "up_down_counter": "counter"}[kind]
        app[name] = (instrument, unit.group(1) if unit else "1",
                     (desc.group(1) or desc.group(2)) if desc else name)

for _f in ("sim/ocpp/cp.py", "sim/ocpp/csms.py", "sim/market/feed.py"):
    _parse_app_instruments(_f)

_unused_app = {
    'energy.evse.power.real': ('gauge', 'W', 'Real power delivered by one EVSE. Zero when it is not charging.'),
    'energy.evse.current.offered': ('gauge', 'A', 'Current offered after the supply-voltage derate. The number that falls during a sag.'),
    'energy.evse.voltage': ('gauge', 'V', 'Phase-to-neutral supply voltage at the EVSE, measured by the site meter over Modbus.'),
    'energy.evse.state': ('gauge', '1', 'OCPP 1.6 charge point status as a 1-of-N flag.'),
    'energy.session.energy': ('gauge', 'kW.h', 'Energy delivered in the current OCPP transaction.'),
    'energy.session.time_to_first_power': ('histogram', 's', 'StartTransaction to the first watt delivered.'),
    'energy.session.energy.requested': ('counter', 'kW.h', "Energy the vehicle asked for, integrated over the session."),
    'energy.session.energy.delivered': ('counter', 'kW.h', 'Energy actually delivered.'),
    'energy.session.power_quality_affected': ('counter', '1', 'Sessions in which the supply derate engaged at least once.'),
    'ocpp.transactions.active': ('gauge', '1', 'Transactions the CSMS believes are open.'),
    'ocpp.transactions.total': ('gauge', '1', 'Transactions the CSMS has started since it came up.'),
}
for n, (inst, unit, desc) in {**_unused_app, **app}.items():
    if n not in metrics:
        metrics.append(n)
    units.setdefault(n, unit)
    descs.setdefault(n, desc)

# Which attributes each metric can carry, and at what requirement level. None means
# `recommended`; 'opt_in' means "a dimension that some points of the family carry and aggregate
# points do not" — an aggregate voltage sample has no `energy.phase`, and saying it is recommended
# produced 31 019 `recommended_attribute_not_present` findings the first time #27 ran.
#
# Three things are deliberately NOT per-metric (they were, and both were wrong):
#   * site identity and geo are *resource* attributes. The hub copies them onto data points at the
#     Prometheus boundary only (hub.yaml transform/visual), so the OTLP stream never carries them
#     on a metric and a registry that demanded them per metric described the visual shaping, not
#     the semantics.
#   * `energy.device.timestamp` never rides on a metric at all (D51, the 0.5 MiB/min leak), so
#     declaring it per metric contradicted the decision it was written from.
def attrs_for(n):
    a: dict[str, str | None] = {}
    if n.startswith(('energy.meter.', 'energy.inverter.', 'energy.turbine.', 'energy.windfarm.',
                     'energy.pv.', 'energy.microgrid.', 'energy.bess.')):
        a.update({'otel.data_point.quality': None, 'energy.point': None,
                  'modbus.register.address': None, 'modbus.register.scale_factor': None,
                  'energy.receive.timestamp': None})
    if re.search(r'\.(voltage|current|power\.)', n) or n.endswith(('voltage', 'current')):
        a['energy.phase'] = 'opt_in'
    if n.startswith('energy.evse.'):
        a.update({'energy.evse.id': None, 'energy.connector.id': None})
    elif n.startswith('energy.session.'):
        a.update({'energy.evse.id': 'opt_in', 'energy.connector.id': 'opt_in'})
    if n.endswith('mppt.power') or '.mppt.' in n:
        a['energy.mppt'] = 'opt_in'
    if n.startswith(('energy.turbine.', 'energy.windfarm.')):
        a['energy.turbine'] = 'opt_in'
    if n == 'energy.pv.irradiance':
        a['energy.irradiance.plane'] = 'opt_in'
    if n.startswith('energy.microgrid.source.'):
        a['energy.source.name'] = 'opt_in'
    if n.startswith('energy.bess.status'):
        a['energy.bess.status.text'] = 'opt_in'
    if n.startswith('energy.bess.hold_reason'):
        a['energy.bess.hold_reason.text'] = 'opt_in'
    if n == 'energy.inverter.status':
        a['energy.inverter.status.text'] = 'opt_in'
    # The readable text is per device, because it is written per device on the edge: the generic
    # `energy.curtailment.text` / `energy.fault_code.text` this used to emit were never sent by
    # anything (D4: a registry must not describe an imaginary device).
    if n == 'energy.inverter.curtailment':
        a['energy.inverter.curtailment.text'] = 'opt_in'
    if n == 'energy.inverter.fault_code':
        a['energy.inverter.fault_code.text'] = 'opt_in'
    if n == 'energy.windfarm.curtailment':
        a['energy.windfarm.curtailment.text'] = 'opt_in'
    if n == 'energy.turbine.status':
        a['energy.turbine.status.text'] = 'opt_in'
    if n == 'energy.windfarm.status':
        a['energy.windfarm.status.text'] = 'opt_in'
    if n == 'energy.meter.energy':
        a['energy.direction'] = 'opt_in'
    if n == 'energy.meter.voltage':
        a['energy.voltage.reference'] = 'opt_in'
    if n.startswith('energy.market.'):
        a['energy.market.region'] = 'opt_in'
    if n == 'energy.microgrid.grid_forming_unit':
        a['energy.microgrid.grid_forming_source'] = 'opt_in'
    if n == 'ocpp.transactions.stopped':
        a['ocpp.stop.reason'] = 'opt_in'
    return a

def family(n):
    parts = n.split('.')
    return parts[1]

fams = collections.OrderedDict()
for n in metrics:
    fams.setdefault(family(n), []).append(n)

attributes = {
    'energy.site.id': ('string', 'Stable site identifier. Stamped by the edge collector.', ['rural-brokenhill', 'metro-tottenham', 'microgrid-kangaroo-island']),
    'energy.site.name': ('string', 'Human-readable site name.', ['Broken Hill EV Charging Site']),
    'energy.site.kind': ('string', 'Regime, not size: charging site, solar farm, wind farm or microgrid.', ['charging_site', 'solar_farm', 'wind_farm', 'microgrid']),
    'geo.latitude': ('double', 'Site latitude (WGS84).', None),
    'geo.longitude': ('double', 'Site longitude (WGS84).', None),
    'otel.data_point.quality': ('string', 'Validity of a data point that is present. good/stale/bad/comms-lost. Authored by the edge collector.', ['good', 'stale', 'bad', 'comms-lost']),
    'energy.phase': ('string', 'Phase, in IEC naming. The device says A/B/C; the edge collector translates.', ['L1', 'L2', 'L3']),
    'energy.direction': ('string', 'Energy direction for a cumulative register.', ['import', 'export']),
    'energy.evse.id': ('string', 'Billing-grade bay identifier from OCPP.', ['EVSE01', 'EVSE12']),
    'energy.connector.id': ('int', 'OCPP 1.6 connector number (1-based).', None),
    'energy.evse.state.text': ('string', 'Readable OCPP charge-point status, written by the edge collector.', ['Available', 'Charging']),
    'ocpp.transaction.id': ('int', 'OCPP 1.6 transaction id, allocated by the CSMS.', None),
    'energy.session.id': ('string', 'Session span identity. Unbounded by design: one per session.', ['9f2c1a4b7d3e']),
    'energy.mppt': ('int', 'MPPT tracker block index (SunSpec model 160).', [1, 2]),
    'energy.turbine': ('int', 'Turbine index within a wind farm (repeating vendor model).', [1, 2, 3]),
    'energy.irradiance.plane': ('string', 'Which plane an irradiance reading is in.', ['horizontal', 'plane_of_array']),
    'energy.source.name': ('string', 'Which generating source block a reading belongs to.', ['solar', 'wind']),
    'energy.point': ('string', 'The device-side point name, for provenance back to the register map.', ['VphA', 'W']),
    'energy.voltage.reference': ('string', 'What a voltage reading is referenced to. Bounded: the edge collector writes it.', ['phase_neutral', 'phase_phase']),
    'energy.market.region': ('string', 'NEM region the spot price belongs to. Bounded: five regions.', ['NSW1', 'VIC1', 'QLD1', 'SA1', 'TAS1']),
    'modbus.register.address': ('int', 'Absolute Modbus register address.', None),
    'modbus.register.scale_factor': ('int', 'The device scale factor applied (value = raw x 10^sf).', None),
    'energy.device.timestamp': ('double', 'The device clock at the moment of the reading. Provenance, carried on spans and logs; never a metric attribute (D51) and never a sort key (D42).', None),
    'energy.receive.timestamp': ('double', 'When the collector saw the value. The only comparable clock.', None),
    'energy.bess.status.text': ('string', 'Readable battery state.', ['idle', 'charging', 'discharging', 'hold']),
    'energy.bess.hold_reason.text': ('string', 'Why the grid former stopped doing what the busbar asked.', ['none', 'soc-high', 'soc-low', 'power-limit']),
    'energy.inverter.status.text': ('string', 'Readable inverter operating state.', ['off', 'sleeping', 'mppt', 'fault']),
    'energy.inverter.curtailment.text': ('string', 'Why the inverter is held down, readable. Bounded: five SunSpec curtailment states.', ['none', 'export_limited', 'inverter_clipped']),
    'energy.inverter.fault_code.text': ('string', 'Readable inverter fault code. Bounded: the model-103 fault table.', ['none']),
    'energy.windfarm.curtailment.text': ('string', 'Why the farm is held down, readable. Bounded: three connection states.', ['none', 'export_limited', 'connection_cap']),
    'energy.turbine.status.text': ('string', 'Readable turbine state.', ['idle', 'starting', 'producing', 'curtailed', 'fault']),
    'energy.windfarm.status.text': ('string', 'Readable farm state.', ['idle', 'producing']),
    'energy.microgrid.grid_forming_source': ('string', 'Which asset sets the voltage and frequency on an island.', ['bess']),
    'ocpp.stop.reason': ('string', 'OCPP 1.6 StopTransaction reason.', ['Local', 'PowerLoss', 'Other']),
}

def q(s):
    return '"' + s.replace('"', '\\"') + '"'

lines = ['# energy.* semantic conventions for this repo (Weaver registry).',
         '#',
         '# Authored, not generated: the first draft was produced from collector/edge/site.yaml so that',
         '# names, units and descriptions could not be mistyped, and `make registry-check` now keeps the',
         '# two in step in BOTH directions — a metric the edge emits and this file omits is a failing',
         '# build, and so is a metric here that nothing emits.',
         '#',
         '# Naming decision (D44): one public prefix, groups inside it that map onto the SIG boundaries',
         '# a split would use.',
         '',
         'groups:',
         '  - id: registry.energy.attributes',
         '    type: attribute_group',
         '    brief: The bounded attributes every energy signal can carry.',
         '    stability: development',
         '    attributes:']
for name, (typ, brief, examples) in attributes.items():
    lines += [f'      - id: {name}', f'        type: {typ}', f'        brief: {q(brief)}',
              '        stability: development']
    if examples:
        lines.append('        examples: [' + ', '.join(str(e) for e in examples) + ']')

briefs = {
 'meter': 'Feeder meter (SunSpec model 203 subset) at a site with a grid connection.',
 'inverter': 'PV inverter (SunSpec model 103/160/302/303 subset).',
 'pv': 'The resource that drives PV output.',
 'turbine': 'One wind turbine (repeating vendor model).',
 'windfarm': 'The wind farm connection point, metered separately from the machines.',
 'microgrid': 'An islanded microgrid: busbar, sources and load, with no feeder.',
 'bess': 'The grid-forming battery on an island.',
 'evse': 'A charge point connector and what it is delivering.',
 'session': 'A charging transaction and its outcome.',
}
# One group per *metric*, named by family (`metric.energy.meter.voltage`). That ID is the "groups
# inside one prefix" half of D44: the family is in the identity, so a later split is a rename of
# groups rather than of metric names. A family-level group with no attributes is rejected by
# weaver 0.28, which is how this shape was arrived at rather than chosen.
for fam, names_in_fam in fams.items():
    for n in names_in_fam:
        inst, unit, desc = {**_unused_app, **app}.get(n, ('gauge', units.get(n, '1'), descs.get(n, n)))
        lines += [f'  - id: metric.{n}', '    type: metric', f'    metric_name: {n}',
                  f'    brief: {q(desc)}', f'    instrument: {inst}', f'    unit: {q(unit)}',
                  '    stability: development', '    attributes:']
        for ref, level in attrs_for(n).items():
            lines.append(f'      - ref: {ref}')
            if level:
                lines.append(f'        requirement_level: {level}')
open('schema/registry/energy.yaml','w').write('\n'.join(lines) + '\n')
print('wrote schema/registry/energy.yaml with', len(metrics), 'metrics and', len(attributes), 'attributes')
