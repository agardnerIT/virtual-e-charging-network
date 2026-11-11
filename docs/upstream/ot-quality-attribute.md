# Upstream proposal: a first-class validity/quality concept for metrics

**Status:** prepared, not filed. This is a ready-to-file issue body plus the evidence package, because
filing it is a decision the maintainer of this repo should make in their own name (and with their
own account in the upstream repository). Nothing here is proposed on anyone's behalf yet.

**Target, in order of preference:** `open-telemetry/opentelemetry-specification` (the semantic
question) → `open-telemetry/opentelemetry-collector-contrib` (the `deadband`/change-of-state
processor is the smaller sibling, below).

**Rule of two (D6):** met. This has hurt twice, in two different places, in this repo:

1. **`otel.data_point.quality`** — a data point that is *present but not trustworthy* has no place to
   say so. Modbus, DNP3, IEC 60870-5-104 and OPC UA all carry a quality/status word on every value;
   OTLP does not. This repo puts it on a data point attribute, which works and is wrong: an
   attribute is part of series identity in every attribute-flattening backend, so a value that
   changes quality mints a new series (measured: 4 532 `energy_*` series in 15 minutes from one meter
   before the offending attributes were shed at the hub; `docs/sizing.md`).
2. **`deadband` at the source** — a collector has no stateful "drop this point unless it moved more
   than X since the last one" processor. `filter` cannot express it (it has no memory), so this repo
   implements deadband in the poller and again in the charge point
   (`sim/poller/poller.py`, `sim/ocpp/cp.py`). Every RTU and every OT historian has this.

## The proposal, in one paragraph

Give a metric data point a **validity** field, or (cheaper, and probably the realistic first step)
define a *semantic convention* for one: a bounded enumeration with agreed values and an agreed
meaning, so that a backend can index on it without the cardinality trap, and so that "stale because
the device's clock says so" is distinguishable from "the collector cannot reach the device".

## Why the existing answers are not enough

| Existing mechanism | Why it does not cover this |
|---|---|
| A data point attribute (what this repo does) | Works, and becomes part of series identity in Prometheus, Datadog, Mimir, ClickHouse-style stores. Four states × every metric = four series. Measured cost above. |
| OTLP `flags` (uint32) | Exists in the proto and is exactly the right shape, but has no defined semantics for OT validity beyond the no-recorded-value bit. A convention here would be a small change; the field already ships. |
| Log an event instead | Loses the association with the value that was bad, which is the whole point: an operator needs to know *that number* was untrustworthy. |
| `UpDownCounter`/`Gauge` type tricks | No. |
| Exemplars | They attach a trace to a sample, not a verdict about the sample. |

## Concrete ask (the smallest version that would have changed this repo)

1. A semantic convention `otel.data_point.quality` (or a documented use of the existing `flags`
   field) with four values and defined meanings:
   `good`, `stale` (the *device's* clock says this is old), `bad` (the device says the point exists but
   has no value — SunSpec/DNP3 have this), `comms-lost` (the collector cannot reach the device; the
   value is retained, not zeroed).
2. A statement that backends MAY index on it **without** treating it as a per-series identity — i.e.
   the "attribute, but a bounded one" carve-out that this repo argues for in `plan.md` D21.
3. Optional, and the reason this is worth doing: a way for a *collector* to set it, which is where the
   knowledge lives (the edge collector is the only component that knows both the transport state and
   the device clock).

## The evidence package

Everything below is measured in this repo, on a running stack, and is reproducible with one command.

| Claim | Evidence |
|---|---|
| Timestamps: a device clock and a receive clock disagree, and sorting by the wrong one inverts causality | `tools/clock-inversion.sh`, `docs/ot-primer.md` §5.1 — a session stops 3 s after the sag that caused it, and the meter's device timestamp puts the cause **236 s after** the effect |
| A wrong device clock is currently indistinguishable from stale data | a meter 300 s behind makes 28 of its series report `stale` while the link is clean and the charger is still charging (`plan.md` D43) |
| A quality attribute costs cardinality | `docs/sizing.md` §"Cardinality" — 4 532 series in 15 min from one meter, before the shed |
| Deadband has to live in the source, twice | `sim/poller/poller.py` (DEADBAND), `sim/ocpp/cp.py` (offered current), both with a 60 s heartbeat so "steady" stays distinguishable from "gone" |
| Four states are not enough either — the words are overloaded | `docs/findings.md` #46: a WAN outage keeps `quality=good` while delivery stops; a device outage sets `comms-lost` while delivery continues. Three different failures (delivery, device, time) currently share one word |

## What we would want from the discussion

- Is `flags` the right carrier, or does this want a field? (A field is a proto change; a convention
  over `flags` is a doc change.)
- Does the "bounded attribute" carve-out have precedent, or is cardinality a hard no?
- If the answer is "not now", say what would change it — this repo will keep the attribute either
  way, and would rather cite a decision than an absence.

## Non-goals of this proposal

- A quality taxonomy for every protocol. Four states, defined, is the ask.
- Anything about storage, sampling or alerting.
- A `deadband` processor in this issue. It is the sibling proposal, and it is smaller: a stateful
  processor with a threshold and a heartbeat, which contrib does not have and which this repo has now
  implemented by hand twice.
