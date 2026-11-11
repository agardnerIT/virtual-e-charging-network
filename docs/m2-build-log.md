# M2 build log — in progress

> Written by the agent that started M2, so a fresh session can continue without re-deriving
> anything. Read `NEXT.md` §1/§4/§5 for the task definition, `plan.md` for decisions D1–D29.
> This file records **what has been decided, what has been written, and what is left**.

Status at handover: **M2 started. Four files written, nothing wired into compose yet.**
No code has been run end-to-end. `plan.md` and `NEXT.md` have *not* been updated for M2 yet.

---

## 1. Decisions locked during recon (do not re-litigate without reason)

| # | Decision | Why |
|---|---|---|
| M2-1 | **OCPP 1.6J**, not 2.0.1. | plan.md §4 literally names a failed `StartTransaction`; 1.6J is the deployed base in Australia; its message-by-message shape maps most literally onto the span tree (2.0.1 collapses start/stop into `TransactionEvent`). |
| M2-2 | The **charge point owns the session trace**. The CSMS emits **logs only** (no spans). | OCPP carries no `traceparent`; two traces cannot be joined by the protocol. One owner keeps "a session is a single trace" true. The CSMS is correlated by `ocpp.transaction.id` — a finding to write up, not fake. |
| M2-3 | **`BootNotification` gets its own short trace** (`ocpp.charge_point.boot`), separate from the session trace. | In OCPP 1.6 a boot is a property of the CP's *connection*, not of a transaction. Session trace = `Authorize → StartTransaction → MeterValues… → sag → StopTransaction`. Document this modelling choice. |
| M2-4 | The CP **reads supply voltage from `device-rural` over Modbus** using the shared `sunspec.py`. | One injection (`make fault SITE=rural`) then propagates for real: sag → derate → drop. No second fault-injection point, no authored causality. |
| M2-5 | CP + CSMS both run in the **rural OT zone** (`ot-rural`), per NEXT.md §4. CSMS is a **test double**, not SteVe+MySQL (D10). | Task says so; SteVe is Java+MySQL and ruled out as default by plan.md §8 / D10. |
| M2-6 | The CP emits **semantic `energy.evse.*` / `energy.session.*` names directly** (it is an app, not a device poller), but sets `energy.quality` (not `otel.data_point.quality`) so the edge collector is still the single author of quality semantics. | Keeps D20/D21 coherent: the edge collector maps `energy.quality` → `otel.data_point.quality`, and its datapoint block is not name-gated, so it runs for OCPP data too. |
| M2-7 | Session = root span; sag = child span `energy.voltage_sag`; the **exemplar on `energy.evse.power.real` is captured from the sag span's context**, so it points into the session trace at the sag. | This is the payoff: metric data point → session trace. |
| M2-8 | Expo default session on EVSE01 so `make up` ends in a live session; `make spine` = one command for sag → derate → drop; session auto-restarts after a gap so the demo repeats. | Success criterion is a ten-minute demo (plan.md §1). |

## 2. SDK / environment findings from probing (saves the next session an hour)

- **Pinned OTel Python SDK is 1.45.1**, `opentelemetry-exporter-otlp-proto-http` 1.45.1, in the `sim` image.
- **Exemplars ARE supported in the SDK**, contrary to the common claim:
  - `opentelemetry.sdk.metrics._internal.exemplar.Exemplar` exists
    (`filtered_attributes, value, time_unix_nano, span_id, trace_id`).
  - Default filter is `TraceBasedExemplarFilter` (samples when the measurement's `context` holds a
    sampled span). Overridable with `OTEL_METRICS_EXEMPLAR_FILTER=always_on`.
  - `Observation(value, attributes, context)` accepts a context; the async-gauge callback path does
    `context=api_measurement.context or get_current()`.
  - The OTLP/HTTP metric encoder **does** serialise exemplars (`_encode_exemplars`).
  - ⇒ Plan: stash `context.get_current()` (or the sag span's context via `trace.set_span_in_context`)
    on each EVSE at sample time, return it as the `Observation` context. Still **unknown** whether
    the Collector passes them through and whether **Prometheus' OTLP receiver** ingests them —
    that is the empirical test and, if negative, the documented finding NEXT.md §4 asks for.
- **Log correlation works out of the box**: `LoggingHandler._translate` builds the SDK `LogRecord`
  with `context=get_current()`, and `LogRecord.__init__` derives `trace_id`/`span_id` from the current
  span. So logging inside a span gives trace-correlated OCPP frame logs.
- Available in the image: `opentelemetry.sdk.trace`, `opentelemetry.sdk._logs`,
  `...proto.http.trace_exporter`, `...proto.http._log_exporter`, `...proto.http.metric_exporter`.
- **`websockets==15.0.1` installs and runs on `python:3.12-slim` arm64.** Use the modern asyncio API:
  `websockets.asyncio.server.serve` and `websockets.asyncio.client.connect`; handler takes a
  `ServerConnection`, path via `conn.request.path`.
- **Heredocs into `docker exec` need `-i`** (`docker exec -i ... python - <<EOF`), or you get no output.

## 3. Files written (on disk)

```
sim/ocpp/__init__.py    package note; both roles import from here
sim/ocpp/ocpp16.py      OCPP 1.6J frame codec (CALL/CALLRESULT/CALLERROR), SUBPROTOCOL="ocpp1.6"
sim/ocpp/telemetry.py   OTLP/HTTP wiring: TracerProvider + MeterProvider + LoggerProvider,
                        `setup(role, attributes, export_interval_ms)`, no-op fallback if the
                        OTLP_ENDPOINT env var is unset
sim/ocpp/csms.py        CSMS test double: WebSocket server, real handlers, in-memory transactions,
                        PowerLoss alarm, OTLP frame logs, HTTP :9001 /health /transactions
```

`csms.py` env: `CSMS_PORT` (9000), `HTTP_PORT` (9001), `OTLP_ENDPOINT`, `CSMS_BLOCKED_ID_TAGS`
(default `BLOCKED-0000`), `CSMS_HEARTBEAT_INTERVAL_S`, `CSMS_ID`.

## 4. Remaining work (checklist)

- [ ] **`sim/ocpp/cp.py`** — the charge point. WebSocket client (`OcppClient` with uniqueId→future
      correlation + inbound-CALL handling), `SupplyMonitor` (Modbus read of `device-rural`, chunked
      ≤120 regs, decode via `sunspec.map_for("meter")`), per-EVSE state machine
      (Available/Preparing/Charging/SuspendedEVSE/Finishing/Faulted), session trace + sag span,
      OCPP-frame logs inside spans, exemplar-bearing metrics, HTTP control on :8080
      (`GET /health`, `POST /session/start`, `POST /session/stop`).
      Derate rule: `pu = min(VphA,VphB,VphC)/230`; offered A = 32 × clamp((pu−0.80)/(0.94−0.80),0,1);
      `pu < 0.80` for >3 s ⇒ `StopTransaction` reason `PowerLoss`.
      Session duration ~600 s, MeterValues ~10 s, idle gap ~20 s, auto-session on EVSE01.
- [ ] `sim/requirements.txt`: add `websockets==15.0.1`.
- [ ] `sim/Dockerfile`: `COPY ocpp/ ./ocpp/`.
- [ ] `compose/docker-compose.yml`: `csms` (ws :9000, http :9001) and `ocpp-cp` on `ot-rural`;
      CP OTLP → `edge-rural:4318`; no host ports.
- [ ] `Makefile`: `make session`, `make spine` (start session → sag on device-rural → watch drop),
      an OCPP-log view target. Keep `make fault`/`make curtail` shape.
- [ ] `hmi/app.js`: add EVSE `POINTS` (`energy_evse_*`, `energy_session_*`), `evse` to `keyOf`/`bound`,
      add `evse` to the refresh regex `energy_(meter|inverter|pv|turbine|windfarm)_…`, and replace the
      dashed EVSE01/02 boxes with live state/power/session cells. `index.html` legend note.
- [ ] **Run the exemplar test end to end** and write the finding either way
      (`/api/v1/query_exemplars` on Prometheus 3.1.0). Do not fake it.
- [ ] M1 regressions: sag, deadband, queue fill/drain, quality → `comms-lost`.
- [ ] `make stats` → update `docs/sizing.md` (state the measurement: `docker stats` vs `VmRSS`).
- [ ] `docs/standards.md`: OCPP 1.6J fidelity section (actions implemented, what is not claimed).
- [ ] `plan.md`: add D30+ for the M2 decisions, update status/strikethroughs.
- [ ] `NEXT.md`: replace with the M3 handover once M2 is done.
- [ ] `README.md`: the session-as-trace walkthrough.

## 5. Networking / naming conventions already in use (match these)

- Metric: `energy.evse.power.real` (+W), `energy.evse.current.offered` (+A),
  `energy.evse.voltage` (+V, `energy_phase` L1/L2/L3), `energy.evse.state` (+1,
  `energy_evse_state_text`), `energy.session.energy` (+kW.h).
- Attributes: `energy.evse.id` (EVSE01/02), `energy.connector.id`, `ocpp.transaction.id`,
  `ocpp.id_tag`, `energy.vehicle.id`, `energy.session.id`.
- Prometheus mangles names with units (NEXT.md §7.16): `energy.evse.power.real` + `W` →
  `energy_evse_power_real_watts`. `POINTS` in `hmi/app.js` is the one place that mapping lives.
- `target_info` is how sites are enumerated in the visual store; the hub copies the five bounded
  site attrs onto datapoints at the Prometheus boundary (`transform/visual`).

## 6. Open question to flag, not guess

Whether the **Collector chain plus Prometheus' OTLP receiver preserves exemplars**. Probe it;
if the chain drops them, record where (collector `groupbyattrs`? the OTLP receiver?) and say so in
the DoD tick and in `docs/sizing.md`/`NEXT.md`, per NEXT.md §4.
