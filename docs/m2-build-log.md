# M2 build log — complete

> Handover record for **M2: sessions as traces**. Read `plan.md` D30–D36 for the decisions this
> ended with, `NEXT.md` for where the work is tracked, and `docs/findings.md` for the M1 findings that
> still apply. This file is the engineering log: what was built, what broke, and what was measured.

**Status: M2 complete, verified end to end on 2026-10-15.**
`make up` → `make spine` produces a live session, a measured sag, a derate, and a failed
transaction, all visible on the rural mimic and in a single Jaeger trace.

---

## 1. What was built

| File | Role |
|---|---|
| `sim/ocpp/ocpp16.py` | OCPP 1.6J frame codec (CALL/CALLRESULT/CALLERROR), subprotocol `ocpp1.6`, shared by both ends so encoder and decoder cannot drift. |
| `sim/ocpp/telemetry.py` | OTLP/HTTP wiring for traces, metrics and logs; no-op fallbacks when `OTLP_ENDPOINT` is unset. |
| `sim/ocpp/csms.py` | CSMS test double: WebSocket server, real handlers, in-memory transactions, `Blocked` id tags, `PowerLoss` alarm, HTTP `/health` and `/transactions` on :9001. |
| `sim/ocpp/cp.py` | The charge point: OCPP client **and** Modbus client (supply voltage from `device-rural`), per-EVSE state machine, session trace, sag span, exemplar-bearing gauges, control HTTP on :8080. |
| `sim/ocpp/cpctl.py` | Operator control run *inside* the container — `status`, `start`, `stop`, `watch`. Mirrors `site/faultctl.py`. |
| `compose/docker-compose.yml` | `csms` and `ocpp-cp` on `ot-rural`; no host ports, OTLP → `edge-rural:4318`. |
| `Makefile` | `make session`, `session-stop`, `session-status`, `spine`, `exemplar`, `ocpp-logs`, `csms-logs`. |
| `hmi/app.js` | EVSE01/02 boxes are live: state, kW, offered A, session kWh, with quality rendered in place. New `evse`/`session` points in `POINTS`, and a newest-wins rule in the point map. |

## 2. The spine, as measured

```
$ make spine

supply 229.4/229.6/227.9 V (0.99 pu, good) | EVSE01 Charging  21.5 kW offered 32.0 A  tx 9  0.07 kWh
-- 0.88 pu: inside the band, charger derates but keeps charging --
supply 203.3/201.0/207.1 V (0.87 pu, good) | EVSE01 Charging  10.3 kW offered 17.2 A  tx 9  0.07 kWh
-- 0.78 pu: below the contactor threshold, session ends for PowerLoss --
supply 178.6/179.9/184.3 V (0.78 pu, good) | EVSE01 Charging   0.0 kW offered  0.0 A  tx 9  0.09 kWh
EVSE01 left Charging - session ended
```

The resulting trace (Jaeger, one session, 15 spans):

```
ocpp.session                       energy.session.outcome=failed_power_loss
                                   ocpp.stop.reason=PowerLoss
                                   energy.session.energy_kwh=0.633
  ocpp.authorize                   ocpp.id_tag.status=Accepted
  ocpp.start_transaction           ocpp.transaction.id=11
  ocpp.status_notification
  ocpp.meter_values  ×10
  energy.voltage_sag               energy.voltage.pu=0.717
                                   energy.derate.offered_a=0
                                   energy.sag.duration_s=3
                                   energy.sag.interrupted_session=True
  ocpp.stop_transaction            ocpp.stop.reason=PowerLoss
  ocpp.status_notification
```

The CSMS logs the same transaction independently, correlated by `ocpp.transaction.id`:

```
WARNING ocpp.csms CSMS: transaction 11 failed - charge point reported PowerLoss
```

## 3. Findings — the things that cost an hour

### 3.1 Prometheus' OTLP receiver does not ingest OTLP exemplars (D33)

The payoff leg of M2 was "a metric data point links to its session trace". It is wired and it
does not work, and the reason is now known precisely:

1. **The SDK emits exemplars.** `InMemoryMetricReader` shows an `Exemplar` with the span's
   `trace_id` and `span_id` for an `Observation(value, attributes, context)` whose context holds
   a sampled span.
2. **The OTLP/HTTP body carries them.** Decoded `ExportMetricsServiceRequest` from the exporter's
   own POST shows `exemplars: 1` with the right ids.
3. **Prometheus stores the metric and drops the exemplar.** Sending directly to Prometheus
   (bypassing both collectors) and querying `/api/v1/query_exemplars` returns `[]`, on **3.1.0**
   (the pin) and on **3.8.1** (latest at the time). `/api/v1/query` returns the metric.

So the collector chain is not implicated: the last hop cannot represent it. What survives is the
honest join — `ocpp.transaction.id` on the session span, on every OCPP log line, and on
`energy.session.energy`. The CP still stashes the sag span's context on each observation, so the
exemplars appear the day the receiver supports them. `make exemplar` asks the store and prints
`[]`; that is the DoD's "documented finding", not a failure.

### 3.2 Awaiting the boot exchange before reading replies deadlocks

`OcppClient.run()` originally did `await self.cp.on_connected()` (which sends `BootNotification`
and awaits its `CALLRESULT`) and only then entered `async for raw in conn`. The only reader was
behind the await, so every boot timed out after 10 s and the console filled with reconnects. Fixed
by starting the read loop as a task first. This is the classic request/response-over-one-socket
shape; the corollary is that any "connect and immediately hand-shake" logic must have its reader
running.

### 3.3 `LOG.info("OCPP %s %s %s", direction, action, extra=extra)` — three placeholders, two args

A copy of the same bug in both `csms.py` and `cp.py`. It only fires when a frame is logged, so it
survived import and startup and appeared as `TypeError: not enough arguments for format string`
killing the connection. `extra=` is not a positional argument.

### 3.4 A charge point should not restart a session into a live sag

First cut restarted the auto session as soon as the previous one ended, so a 20 s sag produced
three failed transactions and three `PowerLoss` alarms in twenty seconds. Now the auto loop waits
the idle gap **and** refuses to start while the supply is below the derate knee. An operator would
not accept a charger that retries into a fault; neither should the demo.

### 3.5 An idle EVSE is not `comms-lost`

`EvseSample.supply_quality` starts at `comms-lost` and was only updated inside the charging loop,
so EVSE02 — connected, healthy, simply not charging — rendered as lost. The supply measurement is
site-wide, not session-scoped: the metric callbacks now read the monitor's snapshot for every
EVSE on every collection.

### 3.6 An in-progress session has an orphan span tree until it ends

The session span is the root and only ends when the transaction does, while children are exported
as they finish. Mid-session, Jaeger shows `ocpp.authorize` etc. with a parent id that is not in
the trace yet. This is correct batching, not a bug, but it looks like one: demo on a **completed**
session, or accept that the root lands last. Do not "fix" it by ending the root early.

### 3.7 A restart mints duplicate series, so the HMI must be explicit about which wins

docs/findings.md #12 in practice: after an `ocpp-cp` restart, both the old and new `service.instance.id`
series exist for five minutes, and the HMI's point map keyed them to the same slot. Whichever
Prometheus returned last won, which for an idle EVSE was the stale `comms-lost` copy. The refresh
now keeps the point with the newer sample timestamp (the hub's receive time, one clock). This
matters more in M2 than in M1 because `energy.session.energy` also mints a series per transaction.

## 4. Definition of done

- [x] An OCPP charge point simulator and a CSMS test double run as containers in the rural OT zone.
- [x] A charging session is a single trace with correctly-nested spans and a span for the sag.
- [x] OCPP protocol frames appear as logs, and the charge-point copies carry the session's
      `trace_id`/`span_id` (verified on the exported OTLP log record; the CSMS copies are
      correlated by `ocpp.transaction.id`, per D31).
- [x] A metric exemplar is *emitted* and *carried* but **not stored by Prometheus** — documented
      finding (D33), not faked.
- [x] The rural mimic shows the EVSEs with live session state, not "not instrumented".
- [x] M1 regressions: sag, deadband, queue fill/drain, quality → `comms-lost` all still work.
- [x] `make stats` → `docs/sizing.md`: ≈832 MiB with the OCPP pair, well inside 3 GB.

## 5. Environment notes for the next session

- Playwright's bundled Chromium does not match the install; point `CHROME_PATH` at
  `~/Library/Caches/ms-playwright/chromium_headless_shell-1248/chrome-headless-shell-mac-arm64/chrome-headless-shell`
  (docs/findings.md #17).
- `docker compose exec` into a container that is being recreated can hang; bound the wait rather
  than looping forever.
- `make spine` assumes EVSE01's expo session is already running; `session start` is a no-op if it
  is, which is what you want.
