# Recorded measurements

Every file here is output from a run against a live stack, with the method stated in it or next to
it. Nothing is hand-typed, and nothing is a projection.

| File | What it is | Produced by | Method |
|---|---|---|---|
| `microgrid-step-20261111T140954Z.csv` | A 300 kW load step on the island, sampled every 2 s: frequency, battery power and SoC, load, and the hold reason | `tools/microgrid-step-measure.sh` | PromQL against the OTLP-fed store, **newest sample per metric** (a poller restart mints a new series — findings #12; taking `result[0]` reads a dead process, D41) |
| `poller-soak-*.csv` (+ `.log`) | A soak of the long-lived Python processes, one sample every 30 s: `docker stats` and `VmRSS` | `tools/poller-soak.sh` | Both numbers on purpose: they disagree by ~20 %, and D10's budget is written against `docker stats` (findings #35) |
| `market-prices-nsw1.csv` | 288 five-minute NEM-style spot prices for NSW1: overnight trough, morning and evening peaks, a midday negative-price strip | Written once, by hand, then replayed by `sim/market/feed.py` | **Invented shape, not a real trading day** — the file's own header says so. The point is that a cost is derivable from telemetry, not that the prices are real (D48) |
| outage runs | A multi-hour link outage and its drain: queue depth, disk, and every edge's exporter counters | `tools/outage-measure.sh`, summarised by `tools/outage-summary.py` | The collectors' own `otelcol_*` counters plus Docker's volume sizes; the summary is a separate program so a run of hours can be re-analysed without re-running it |

Two conventions worth knowing before you compare any of these:

- **A counter reset is not data loss.** A collector restart resets its cumulative counters, so
  `tools/outage-summary.py` finds the last decrease and baselines from the sample after it, and says
  on the page whether that happened.
- **A series keeps counting after its data stops**, for the store's five-minute lookback. Every
  count in these files is taken either after that window clears or with `last_over_time(...[45s])`,
  because a plain `count()` produced two wrong numbers before this convention existed (findings #49).

`docker stats` versus `VmRSS` is the other recurring trap: `docs/findings.md` #35, and `docs/sizing.md`
names which one every figure came from.
