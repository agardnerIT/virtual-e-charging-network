# The `energy.*` semantic conventions, as a registry

`registry/energy.yaml` is a [Weaver](https://github.com/open-telemetry/weaver) registry: the
metric names, units, instruments and the bounded attributes this repo emits, in a form a tool can
read. 106 metrics, 54 attributes, validated on every `make registry-check`.

```bash
make registry-check    # weaver validates the file, then checks it against what the stack emits
make registry-stats    # weaver's own summary
make registry-live     # weaver live-check: the same vocabulary against the RUNNING stack (#27)
```

**The gate is two-way, and that is the point.** A registry that drifts from the code is worse than
none — it is a document that looks authoritative and is wrong. `tools/registry-coverage.py` fails
if:

- a metric is emitted by the edge collector (`collector/edge/site.yaml`) or by an application
  (`sim/ocpp/cp.py`, `csms.py`) and the registry does not describe it; **or**
- the registry describes a metric that nothing emits. A registry must not describe an imaginary
  device (D4).

Demonstrated: adding `energy.meter.humidity` to the edge config makes `make registry-check` fail
with the missing name and the file to edit.

## The runtime check: `make registry-live`

`registry-check` asks whether the registry agrees with **the code that emits** — it reads
`collector/edge/site.yaml` and the application source. `make registry-live` asks the stronger
question: does it agree with **what the running stack sends**? Weaver listens on an OTLP port, the
hub is recreated for the duration with one extra exporter and one extra pipeline
(`collector/hub/registry-tap.yaml`, merged by a second `--config`), and every finding is reported as
it arrives. `make up` never reads the overlay (`compose/registry-live.yml`) and the target puts the
hub back on its normal config on every exit path.

```bash
make registry-live                 # 60 s, exits non-zero on a violation
make registry-live DURATION=180    # longer window
make registry-live WRONG=1         # proof: one metric arrives as energy.meter.humidity, must fail
```

The full report (~9 MiB) lands in `outages/` with the other raw run output; the summary that maps to
this document is committed in `docs/measurements/registry-live-*.json`.

**Measured, first run on an M4-complete stack:** 65 807 advisories, 3 828 of them violations —
452 `unit_mismatch`, 3 253 `missing_attribute`, 117 `missing_metric` — against a registry that the
build-time gate was passing. After the fixes below: **0 violations**, plus ~3 000
`opt_in_attribute_not_present` at information level (the optional dimensions that aggregate data
points do not carry). That gap between the two checks is the argument for having both (findings #63).

### What the run found, and what was changed

- **47 metrics declared the wrong unit.** The generator only understood `metric.name == "…"` and
  every `IsMatch(...)` unit statement silently became `unit: "1"`. Units are now read from the edge
  config in both forms, and the registry carries V / A / Cel / kW.h / var / W·m⁻² as the devices
  send them.
- **Sixteen resource attributes were undeclared**: the nameplate and one-line data the pollers and
  the charge points stamp (`energy.meter.phases`, `energy.inverter.ac_rating_kw`,
  `energy.microgrid.inertia_h_s`, `energy.evse.count`, `energy.market.recording`, …). No build-time
  check could see them, because none of them names a metric.
- **Site identity and geo came off every metric group.** They are *resource* attributes: the hub
  copies them onto data points at the Prometheus boundary only (`transform/visual`), so a registry
  that demanded them per metric was describing the visual shaping, not the OTLP semantics. Same for
  `energy.device.timestamp`, which D51 says never rides on a metric at all.
- **Optional dimensions are `requirement_level: opt_in`, not `recommended`**: an aggregate voltage
  sample has no `energy.phase`, and calling that a violation of a recommendation produced 31 019
  false findings.
- **Two generic attributes were describing nothing.** `energy.curtailment.text` and
  `energy.fault_code.text` were never emitted; the edge writes them per device
  (`energy.inverter.fault_code.text`, `energy.windfarm.curtailment.text`), so the registry now names
  what is actually sent. D4 applies to attributes as much as to metrics: **a registry must not
  describe an imaginary device.**

### The filters, and the decision each one rests on

`schema/registry/.weaver.toml` carries three filters, and each is a decision already written down
elsewhere rather than a preference:

| Filter | Why | Decision |
|---|---|---|
| `not_stable` | every group here is `stability: development`; that is the claim, and 29 057 advisories said so | this file, below |
| `extends_namespace`, `illegal_namespace` | weaver's default advice polices the *standard* namespaces; this registry owns `energy.*` outright, so `energy.turbine` colliding with `energy.turbine.status.text` is our own naming | D44 |
| `missing_metric`, `missing_attribute` on `modbus.*` | register-map names are the input to these semantics, never part of them | D20 |
| `missing_attribute` on `service.*`, `telemetry.*`, `otel.collector.*` | the SDK's and the collector's own attributes; weaver 0.28 can only resolve a dependency from a v2 registry, which this is not | D44 |

Everything that survives those filters is either real drift in the stack or a real gap in this file.
The residual `opt_in_attribute_not_present` findings are the honest ones: they say which dimensions
*can* be on a family and are not on this point.

> Not done, and deliberately: a `validate` processor on the collector. Enforcing this registry on
the hot path is a different conversation, and #27's non-goals say so.

## Conventions this registry encodes

- **One prefix, groups inside it** (D44). The group id carries the family
  (`metric.energy.meter.voltage`), so a later split into `grid.*`/`solar.*`/`ev.*` is a rename of
  groups rather than of names.
- **The two claims of the repo are attributes, not prose**: `otel.data_point.quality` (a value that
  is present but not trustworthy) and the `energy.device.timestamp` / `energy.receive.timestamp`
  pair (provenance vs the only comparable clock, D42/D43). A reader of the schema learns both from
  the schema.
- **Out of scope:** the Modbus-side device shapes (`modbus.register.*`, `modbus.inverter.*`) are
  deliberately *not* registered. They are the input to these semantics, and their names come from a
  device's register map — a registry of one vendor's register file is the opposite of a semantic
  convention (D20).
- **Stability is `development`** on everything: this vocabulary is one repo's, not a standard, and
  saying otherwise is exactly the kind of claim `docs/standards.md` exists to prevent.

Publishing any of this upstream is **#16**, under the rule of two (D6).
