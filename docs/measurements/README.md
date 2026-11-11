# Recorded measurements

Every file here is output from a run against a live stack, with the method stated in it or next to
it. Nothing is hand-typed, and nothing is a projection.

| File | What it is | Produced by | Method |
|---|---|---|---|
| `microgrid-step-20261111T140954Z.csv` | A 300 kW load step on the island, sampled every 2 s: frequency, battery power and SoC, load, and the hold reason | `tools/microgrid-step-measure.sh` | PromQL against the OTLP-fed store, **newest sample per metric** (a poller restart mints a new series — findings #12; taking `result[0]` reads a dead process, D41) |
| `poller-soak-before-*.csv` | The poller leak, *before* D51: 30 minutes, one sample every 30 s, `docker stats` and `VmRSS` | `tools/poller-soak.sh` | Both numbers on purpose: they disagree by ~20 %, and D10's budget is written against `docker stats` (findings #35) |
| `poller-soak-after-*.csv` | The same soak after the fix: the acceptance evidence for D51 / #25 | `tools/poller-soak.sh` | Same method, same targets, so the two files are comparable — which is the whole point of keeping the "before" |
| `market-prices-nsw1.csv` | 288 five-minute NEM-style spot prices for NSW1: overnight trough, morning and evening peaks, a midday negative-price strip | Written once by hand, then replayed by `sim/market/feed.py` | **Invented shape, not a real trading day** — the file's own header says so. The point is that a cost is derivable from telemetry, not that the prices are real (D48) |
| `registry-live-*.json` | The registry checked against a **running** stack: weaver live-check, one 60 s window, every finding counted by id and level. The `-wrong` file is the deliberate-failure proof | `tools/registry-live.sh` (`make registry-live`) | Weaver taps the hub's OTLP stream through a second `--config` pipeline for the window, then the hub is restored to its normal config. The summary is kept here; the ~9 MiB full report goes to `outages/`, which is scratch (findings #63, #64) |
| outage runs | A multi-hour link outage and its drain: queue depth, disk, and every edge's exporter counters | `tools/outage-measure.sh`, summarised by `tools/outage-summary.py` (the finished run is copied here) | The collectors' own `otelcol_*` counters. Disk is `du` on the queue volume — allocated blocks, because `file_storage` writes a *sparse* file, so `docker system df -v` (apparent size) and `du` (blocks) differ by ~2x and both are right about different questions (findings #59) |

Two conventions worth knowing before comparing any of these:

- **A counter reset is not data loss.** A collector restart resets its cumulative counters, so
  `tools/outage-summary.py` finds the last decrease and baselines from the sample after it, and says
  on the page whether that happened.
- **A series keeps counting after its data stops**, for the store's five-minute lookback. Every
  count in these files is taken either after that window clears or with `last_over_time(...[45s])`,
  because a plain `count()` produced two wrong numbers before this convention existed (findings #49).

`docker stats` versus `VmRSS` is the other recurring trap: `docs/findings.md` #35, and `docs/sizing.md`
names which one every figure came from.
