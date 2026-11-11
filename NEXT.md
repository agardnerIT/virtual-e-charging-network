# NEXT — where the work is

**Work items are tracked as GitHub issues, not in this file.** The milestone is
**[M3 — the hard cases](https://github.com/agardnerIT/virtual-e-charging-network/milestone/1)**.

| # | Work item | Why it is in M3 |
|---|---|---|
| [#1](https://github.com/agardnerIT/virtual-e-charging-network/issues/1) | Metro depot — concurrent sessions and cardinality | ✅ **Done.** Fourth site `metro-tottenham`, 12 bays on one CP, 12 concurrent sessions, one trace each, series count measured. |
| [#2](https://github.com/agardnerIT/virtual-e-charging-network/issues/2) | Isolated microgrid — solar + wind + BESS, grid-forming | Same sag, no upstream network to explain it. |
| [#3](https://github.com/agardnerIT/virtual-e-charging-network/issues/3) | Multi-hour store-and-forward outage, measured | The last unmeasured claim in `docs/sizing.md`. |
| [#4](https://github.com/agardnerIT/virtual-e-charging-network/issues/4) | Clock skew — an effect before its cause | The dual-timestamp story, pushed until it breaks. |
| [#5](https://github.com/agardnerIT/virtual-e-charging-network/issues/5) | Regression gate and five-site footprint | Closes the milestone. |

Start with **#2**, then **#4**. #3 can run in the background against the existing wind site while
either is in progress — it needs a ≥2 h wall-clock outage and no new code.

## State of the build

`M0`, `M1a`, `M1b`, `M2` and `M3-1` (the metro depot) are complete and verified.

- Two collector layers (edge per site, one hub), **four** real sites, an impaired WAN with four
  independent links, and a visual layer (map + one-line mimic) fed only from the pipeline.
- **M2:** a charging session is one trace. An OCPP 1.6J charge point and a CSMS test double run in
  the rural OT zone; the charge point reads supply voltage over Modbus, so `make spine` produces a
  real sag → derate → `PowerLoss` transaction. OCPP frames are trace-correlated logs. The
  metric-exemplar leg is emitted and carried but **not stored by Prometheus** — a measured finding
  (plan.md D33), not a fake.
- **M3-1 (the metro depot):** a fourth site, `metro-tottenham`, with twelve bays on one OCPP charge
  point, started 15 s apart, over a 12 ms fibre link. Twelve concurrent sessions, each one trace,
  no crossed transaction ids, checked against the CSMS's own connector map. The mimic draws the bays
  as a table fed from the pipeline (2 cells at the rural site, 12 at the depot, same drawing). The
  series cost is measured before/after in `docs/sizing.md`; the site meter's load is its own model
  and does not sum the bays — stated, not faked (`docs/standards.md` §6).
- ~1.09 GiB for the whole stack against the 3 GB budget (D10).

Verify it before you change it:

```bash
make up                 # ends in a picture at http://127.0.0.1:8091
make queues             # five edges, all 0
make session-status     # every bay: supply voltage, state, kW, offered A, transaction
make session-status SITE=metro   # the depot's twelve
make spine              # sag -> derate -> failed session, in one command
make stats              # ~1.09 GiB
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
