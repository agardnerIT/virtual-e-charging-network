# The map and the mimic

The views are ours (`hmi/`), not a vendored dashboard, and they are fed the same way everything else
is: over OTLP. The hub fans out to Prometheus and Jaeger; the page queries Prometheus through its own
origin and never talks to the store directly, and never reads a simulator (plan.md D18).

**Ports.** Map `http://127.0.0.1:8091` (`make hmi`), Jaeger `http://127.0.0.1:16687`. Both are
overridable (`HMI_PORT`, `JAEGER_PORT`) because this host has an unrelated ssh port-forward sitting on
the defaults (`docs/findings.md` #32). Prometheus publishes no host port at all — use `make prom`.

## The map

Every site is placed from attributes the **edge collector** stamps: `energy.site.id`,
`energy.site.kind`, `geo.latitude`, `geo.longitude`. The map therefore needs no knowledge of the
sites — add a site to the pipeline and it appears.

The one thing that made this work in Prometheus: resource attributes do not land on series. They go to
a synthetic `target_info` series, so the five bounded site attributes are copied resource → data point
at the Prometheus boundary `transform/visual` in `collector/hub/hub.yaml` (plan.md D25). The page needs
no `on(job) group_left` join.

## The mimic

A one-line diagram per site, and a different one per regime:

- **Load site** — grid → CB1 → transformer → busbar → meter with live per-phase voltage, current and
  power in place, and every bay on an LV board beneath it: 2 cells at the rural site, 12 at the
  depot. Same drawing, count read from the pipeline.
- **Solar farm** — arrays → MPPT trackers → inverter → step-up → grid, with irradiance, DC and AC
  values. There is no per-phase real power on the inverter (model 103 carries total `W` only), so the
  diagram shows the total rather than multiplying V by I and calling it a measurement.
- **Wind farm** — six turbines on a collector bus → step-up → grid.

Two drawing rules matter more than the geometry:

- **Equipment with no instrumented point is drawn dashed and labelled**, never filled with a plausible
  number.
- **Quality is shown at the value**, not in a legend, and stale is never drawn as live. A value that
  has gone `comms-lost` looks it, wherever it came from — the edge collector authors
  `otel.data_point.quality` for meters and applications alike (plan.md D21, D35).

## What is deliberately *not* treated as staleness

A value that simply has not changed. The poller deadbands, so about a third of readings never leave
the site (`docs/sizing.md`), and the page must not draw a steady register as a dead one. Freshness and
trust are different questions; trust comes from `otel.data_point.quality`.

## Watching it

`make fault`, `make curtail` and the link profiles all show up here live. The mimic is the surface the
voltage sag is meant to be watched on: run `make spine` with the rural page open and watch the EVSE
derate, then go `comms-lost` when the transaction fails.

Two screenshot findings, if you want to capture it: Chrome's `--virtual-time-budget` virtualises
`Date.now()` and every site renders as stale, and the page polls every 2 s so no Playwright
`networkidle` wait will ever fire. Use a real clock and `domcontentloaded` (`docs/findings.md` #16,
#17). `tools/shot.js` is the working recipe.

## Why this stack and not Grafana

Measured, not preferred: Prometheus + Jaeger + our own `hmi/` is 80 MiB of the stack, Perses 162 MiB,
Grafana OSS 276 MiB (plan.md D24). RAM did not decide it — all three fit the budget. Neither Perses
nor Grafana draws an SVG one-line diagram with live values *in place*, which is the deliverable.
Perses is kept as the `perses` compose profile for a trends view; nothing depends on it, and the
`make up STACK=perses` flag the plan mentions is not wired into the Makefile yet.
