# The `energy.*` semantic conventions, as a registry

`registry/energy.yaml` is a [Weaver](https://github.com/open-telemetry/weaver) registry: the
metric names, units, instruments and the bounded attributes this repo emits, in a form a tool can
read. 94 metrics, 31 attributes, validated on every `make registry-check`.

```bash
make registry-check    # weaver validates the file, then checks it against what the stack emits
make registry-stats    # weaver's own summary
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
