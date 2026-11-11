# NEXT — where the work is

**Work items are tracked as GitHub issues, not in this file.** The milestone is
**[M3 — the hard cases](https://github.com/agardnerIT/virtual-e-charging-network/milestone/1)**.

| # | Work item | Why it is in M3 |
|---|---|---|
| [#1](https://github.com/agardnerIT/virtual-e-charging-network/issues/1) | Metro depot — concurrent sessions and cardinality | The site where two EVSE series become twelve, and where D36 has to hold. |
| [#2](https://github.com/agardnerIT/virtual-e-charging-network/issues/2) | Isolated microgrid — solar + wind + BESS, grid-forming | Same sag, no upstream network to explain it. |
| [#3](https://github.com/agardnerIT/virtual-e-charging-network/issues/3) | Multi-hour store-and-forward outage, measured | The last unmeasured claim in `docs/sizing.md`. |
| [#4](https://github.com/agardnerIT/virtual-e-charging-network/issues/4) | Clock skew — an effect before its cause | The dual-timestamp story, pushed until it breaks. |
| [#5](https://github.com/agardnerIT/virtual-e-charging-network/issues/5) | Regression gate and five-site footprint | Closes the milestone. |

Start with **#1**, then **#2**. Do not start both new sites at once: the depot is the one that
stresses cardinality, and proving it alone keeps the failure modes separable. #3 can run in the
background against the existing wind site while #1 is in progress.

## State of the build

`M0`, `M1a`, `M1b` and `M2` are complete and verified.

- Two collector layers (edge per site, one hub), three real sites, an impaired WAN with three
  independent links, and a visual layer (map + one-line mimic) fed only from the pipeline.
- **M2:** a charging session is one trace. An OCPP 1.6J charge point and a CSMS test double run in
  the rural OT zone; the charge point reads supply voltage over Modbus, so `make spine` produces a
  real sag → derate → `PowerLoss` transaction. OCPP frames are trace-correlated logs. The
  metric-exemplar leg is emitted and carried but **not stored by Prometheus** — a measured finding
  (plan.md D33), not a fake.
- ~832 MiB for the whole stack against the 3 GB budget (D10).

Verify it before you change it:

```bash
make up                 # ends in a picture at http://127.0.0.1:8091
make queues             # three edges, all 0
make session-status     # supply voltage, both EVSEs, live transaction
make spine              # sag -> derate -> failed session, in one command
make stats              # ~830 MiB
```

It may be night where you are: `make up SOLAR_CLOCK_HOUR=12` pins the solar sim to daytime.

## Before you write anything

1. [`plan.md`](plan.md) — decisions D1–D36, the source of truth. Add new ones as `D37`, `D38`, …
2. [`CONTRIBUTING.md`](CONTRIBUTING.md) — hard constraints, the split of responsibility, and the
   checklist before calling something done.
3. [`docs/findings.md`](docs/findings.md) — the things that cost an hour. Read the sections that
   match what you are about to touch.
4. [`docs/m2-build-log.md`](docs/m2-build-log.md) — how M2 was actually built, including the bugs.

## Conventions that matter most

- **The poller reads and scales; the edge collector gives meaning** (D20). Do not move semantics
  into the poller.
- **Quality is `otel.data_point.quality`** (D21), authored by the edge collector.
- **One fault only: voltage sag** (D29). Resist building a fault framework.
- **No host ports in the OT zone.** Control by `docker compose exec` (`make fault`, `make session`).
- Update `docs/sizing.md` with **measured** numbers and name the measurement (`docker stats` vs
  `VmRSS`).
