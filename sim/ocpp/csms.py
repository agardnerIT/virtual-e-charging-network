"""A minimal but protocol-real OCPP 1.6J central system (CSMS) test double.

Why a test double rather than a real CSMS
-----------------------------------------
The obvious real one is SteVe, which is Java + MySQL. plan.md §8 and D10 rule that out as a
*default*: a JVM and a database are the two most expensive things in the stack and the CSMS's
storage engine is not what this repo is about. What matters for telemetry is that the OCPP wire
protocol is real - real WebSocket upgrade, real `ocpp1.6` subprotocol, real JSON frame arrays,
real `BootNotification` / `Authorize` / `StartTransaction` / `MeterValues` / `StopTransaction`
exchange, real `transactionId` allocation. That is what this implements. SteVe stays an optional
profile for anyone who wants it.

What it does
------------
* Speaks OCPP 1.6J over WebSocket on `/ocpp/<chargePointId>`.
* Keeps transactions in memory: id, charge point, connector, idTag, meterStart/meterStop,
  stop reason.
* Answers every CALL with a real CALLRESULT, and rejects an idTag on a configured deny-list
  with a real `Blocked` `idTagInfo` - so a *failed* authorisation is a protocol event, not a
  printed string.
* Emits every frame it sends and receives as an OTLP log, on the same edge collector as the
  rest of the site, plus an alarm when a transaction stops for `PowerLoss`.

What it deliberately does not do
--------------------------------
No persistence, no charging profiles / smart charging, no firmware management, no security
profiles, no OCPI. `docs/standards.md` says so per action rather than implying conformance.

A note for the trace story: OCPP 1.6 carries no trace context. The CSMS therefore emits logs
(and no spans), and the CSMS side of a transaction is correlated to the charge point's session
trace by `ocpp.transaction.id` - a first-class OCPP field - not by a W3C `traceparent`, because
the protocol has no place to put one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from websockets.asyncio.server import ServerConnection, serve

import ocpp16
from telemetry import setup

LOG = logging.getLogger("ocpp.csms")


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _envi(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass
class Transaction:
    transaction_id: int
    charge_point_id: str
    connector_id: int
    id_tag: str
    meter_start_wh: float
    started_at: float
    meter_stop_wh: float | None = None
    stopped_at: float | None = None
    stop_reason: str | None = None
    last_meter_wh: float | None = None

    def snapshot(self) -> dict:
        return {
            "transactionId": self.transaction_id,
            "chargePointId": self.charge_point_id,
            "connectorId": self.connector_id,
            "idTag": self.id_tag,
            "meterStartWh": round(self.meter_start_wh, 1),
            "meterStopWh": None if self.meter_stop_wh is None else round(self.meter_stop_wh, 1),
            "energyWh": None if self.meter_stop_wh is None
                        else round(self.meter_stop_wh - self.meter_start_wh, 1),
            "stopReason": self.stop_reason,
            "durationS": round((self.stopped_at or time.time()) - self.started_at, 1),
            "open": self.stopped_at is None,
        }


class CsmsState:
    """In-memory transaction store. One lock; the HTTP endpoint runs on another thread."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.transactions: dict[int, Transaction] = {}
        self.next_transaction_id = 1
        self.charge_points: dict[str, dict] = {}
        self.blocked_id_tags = {
            t.strip() for t in _env("CSMS_BLOCKED_ID_TAGS", "BLOCKED-0000").split(",") if t.strip()
        }

    # -- charge point registry ---------------------------------------------------------
    def on_boot(self, cp_id: str, payload: dict) -> None:
        with self.lock:
            self.charge_points[cp_id] = {
                "chargePointId": cp_id,
                "vendor": payload.get("chargePointVendor"),
                "model": payload.get("chargePointModel"),
                "serialNumber": payload.get("chargePointSerialNumber"),
                "firmwareVersion": payload.get("firmwareVersion"),
                "lastSeen": time.time(),
            }

    def touch(self, cp_id: str) -> None:
        with self.lock:
            if cp_id in self.charge_points:
                self.charge_points[cp_id]["lastSeen"] = time.time()

    # -- transactions ------------------------------------------------------------------
    def start_transaction(self, cp_id: str, payload: dict) -> Transaction:
        with self.lock:
            tx = Transaction(
                transaction_id=self.next_transaction_id,
                charge_point_id=cp_id,
                connector_id=int(payload.get("connectorId", 1)),
                id_tag=payload.get("idTag", ""),
                meter_start_wh=float(payload.get("meterStart", 0.0)),
                started_at=time.time(),
            )
            self.transactions[tx.transaction_id] = tx
            self.next_transaction_id += 1
            return tx

    def meter_values(self, cp_id: str, payload: dict) -> Transaction | None:
        tx_id = payload.get("transactionId")
        with self.lock:
            tx = self.transactions.get(tx_id) if tx_id is not None else None
            if tx is None:
                return None
            for mv in payload.get("meterValue", []):
                for sv in mv.get("sampledValue", []):
                    if sv.get("measurand", "Energy.Active.Import.Register") == \
                            "Energy.Active.Import.Register":
                        try:
                            tx.last_meter_wh = float(sv.get("value", 0.0))
                        except (TypeError, ValueError):
                            pass
            return tx

    def stop_transaction(self, cp_id: str, payload: dict) -> Transaction | None:
        tx_id = payload.get("transactionId")
        with self.lock:
            tx = self.transactions.get(tx_id) if tx_id is not None else None
            if tx is None:
                return None
            tx.meter_stop_wh = float(payload.get("meterStop", tx.last_meter_wh or 0.0))
            tx.stopped_at = time.time()
            tx.stop_reason = payload.get("reason", "Local")
            return tx

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "chargePoints": list(self.charge_points.values()),
                "transactions": [t.snapshot() for t in self.transactions.values()],
                "blockedIdTags": sorted(self.blocked_id_tags),
            }


class Csms:
    """The OCPP message handlers. One instance per process; state is shared across connections."""

    def __init__(self, state: CsmsState, telemetry) -> None:
        self.state = state
        self.tracer = telemetry.tracer("vecn.ocpp-csms")
        self.meter = telemetry.meter("vecn.ocpp-csms")
        self._active = self.meter.create_observable_gauge(
            "ocpp.transactions.active",
            callbacks=[self._observe_active],
            unit="1",
            description="Transactions the CSMS currently believes are open.",
        )
        self._started = self.meter.create_observable_gauge(
            "ocpp.transactions.total",
            callbacks=[self._observe_total],
            unit="1",
            description="Transactions the CSMS has started since it came up (process-scoped).",
        )
        LOG.info("CSMS online: blocked idTags=%s",
                 ",".join(sorted(self.state.blocked_id_tags)) or "(none)")

    # -- metrics -----------------------------------------------------------------------
    def _observe_active(self, _options):
        from opentelemetry.metrics import Observation
        with self.state.lock:
            open_count = sum(1 for t in self.state.transactions.values() if t.stopped_at is None)
        return [Observation(open_count)]

    def _observe_total(self, _options):
        from opentelemetry.metrics import Observation
        with self.state.lock:
            total = len(self.state.transactions)
        return [Observation(total)]

    # -- frame logging -----------------------------------------------------------------
    def _log_frame(self, direction: str, action: str, unique_id: str,
                   payload: dict, cp_id: str, tx_id: int | None = None) -> None:
        """Emit one OCPP frame as a log record (D14: logs are for protocol frames).

        The CSMS has no span, so these records carry no `trace_id`; they are correlated to
        the charge point's session trace by `ocpp.transaction.id`. The charge point logs the
        same frames from inside its spans, so those copies *do* carry the trace id - which is
        the pair that lets a reader see both ends of one message.
        """
        extra = {
            "ocpp.message.direction": direction,
            "ocpp.action": action,
            "ocpp.unique_id": unique_id,
            "ocpp.charge_point.id": cp_id,
            "ocpp.frame": json.dumps(payload, separators=(",", ":")),
        }
        if tx_id is not None:
            extra["ocpp.transaction.id"] = tx_id
        level = logging.WARNING if action == "StopTransaction" and \
            payload.get("reason") == "PowerLoss" else logging.INFO
        if level == logging.WARNING:
            LOG.warning("OCPP %s %s tx=%s reason=PowerLoss - session failed",
                        direction, action, tx_id, extra=extra)
        else:
            LOG.info("OCPP %s %s %s", direction, action, extra=extra)

    # -- handlers ----------------------------------------------------------------------
    def _authorize(self, cp_id: str, payload: dict) -> dict:
        id_tag = payload.get("idTag", "")
        if id_tag in self.state.blocked_id_tags:
            LOG.warning("OCPP authorize REJECTED for idTag=%s", id_tag,
                        extra={"ocpp.charge_point.id": cp_id, "ocpp.id_tag": id_tag,
                               "ocpp.id_tag_status": "Blocked"})
            return {"idTagInfo": {"status": "Blocked"}}
        return {"idTagInfo": {"status": "Accepted", "expiryDate": None, "parentIdTag": None}}

    def _boot(self, cp_id: str, payload: dict) -> dict:
        self.state.on_boot(cp_id, payload)
        return {
            "status": "Accepted",
            "currentTime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "interval": _envi("CSMS_HEARTBEAT_INTERVAL_S", 60),
        }

    def _start(self, cp_id: str, payload: dict) -> dict:
        id_tag = payload.get("idTag", "")
        if id_tag in self.state.blocked_id_tags:
            return {"transactionId": 0, "idTagInfo": {"status": "Blocked"}}
        tx = self.state.start_transaction(cp_id, payload)
        return {
            "transactionId": tx.transaction_id,
            "idTagInfo": {"status": "Accepted", "expiryDate": None, "parentIdTag": None},
        }

    def _stop(self, cp_id: str, payload: dict) -> dict:
        tx = self.state.stop_transaction(cp_id, payload)
        reason = payload.get("reason", "Local")
        if tx is not None and reason == "PowerLoss":
            LOG.warning("CSMS: transaction %s failed - charge point reported PowerLoss",
                        tx.transaction_id,
                        extra={"ocpp.transaction.id": tx.transaction_id,
                               "ocpp.charge_point.id": cp_id,
                               "energy.session.energy_wh": round(
                                   (tx.meter_stop_wh or 0) - tx.meter_start_wh, 1)})
        return {"idTagInfo": {"status": "Accepted"}}

    def dispatch(self, cp_id: str, action: str, payload: dict, unique_id: str) -> dict:
        self.state.touch(cp_id)
        tx_id = payload.get("transactionId")
        self._log_frame("recv", action, unique_id, payload, cp_id, tx_id)
        handler = {
            "BootNotification": self._boot,
            "Heartbeat": lambda c, p: {"currentTime": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
            "StatusNotification": lambda c, p: {},
            "Authorize": self._authorize,
            "StartTransaction": self._start,
            "MeterValues": lambda c, p: self._meter_values(c, p),
            "StopTransaction": self._stop,
            "DataTransfer": lambda c, p: {"status": "Accepted"},
        }.get(action)
        if handler is None:
            raise ocpp16.OCPPProtocolError(f"NotSupported: {action}")
        return handler(cp_id, payload)

    def _meter_values(self, cp_id: str, payload: dict) -> dict:
        tx = self.state.meter_values(cp_id, payload)
        if tx is not None:
            # Health on the wire between meter readings is the one thing the CSMS can assert.
            pass
        return {}


# --------------------------------------------------------------------------------------
# WebSocket server
# --------------------------------------------------------------------------------------
async def _connection(conn: ServerConnection, csms: Csms) -> None:
    path = conn.request.path if conn.request else ""
    cp_id = path.rstrip("/").rsplit("/", 1)[-1] or "unknown"
    if conn.subprotocol != ocpp16.SUBPROTOCOL:
        LOG.warning("connection from %s negotiated subprotocol %r, expected %r",
                    cp_id, conn.subprotocol, ocpp16.SUBPROTOCOL)
    LOG.info("charge point %s connected over %s", cp_id, conn.subprotocol)
    try:
        async for message in conn:
            try:
                mtype, unique_id, action_or_payload, payload = ocpp16.parse(message)
            except ocpp16.OCPPProtocolError as exc:
                LOG.warning("unparseable frame from %s: %s", cp_id, exc)
                continue
            if mtype != ocpp16.CALL:
                continue
            action, call_payload = action_or_payload, payload or {}
            try:
                response = csms.dispatch(cp_id, action, call_payload, unique_id)
            except ocpp16.OCPPProtocolError as exc:
                reply = ocpp16.error(unique_id, "NotSupported", str(exc))
                await conn.send(reply)
                csms._log_frame("send", f"CALLERROR {action}", unique_id,
                                {"error": str(exc)}, cp_id, call_payload.get("transactionId"))
                continue
            await conn.send(ocpp16.result(unique_id, response))
            csms._log_frame("send", action, unique_id, response, cp_id,
                            call_payload.get("transactionId") or response.get("transactionId"))
    except Exception:  # noqa: BLE001 - a dropped charge point is normal
        LOG.debug("connection for %s ended", cp_id, exc_info=True)
    finally:
        LOG.info("charge point %s disconnected", cp_id)


class _ControlHandler(BaseHTTPRequestHandler):
    server_version = "vecn-csms/0.1"

    def log_message(self, fmt, *args):
        LOG.debug("http %s", fmt % args)

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        state: CsmsState = self.server.state  # type: ignore[attr-defined]
        if self.path == "/health":
            snap = state.snapshot()
            open_count = sum(1 for t in snap["transactions"] if t["open"])
            self._send(200, {"status": "ok", "chargePoints": len(snap["chargePoints"]),
                             "transactions": len(snap["transactions"]), "open": open_count})
        elif self.path in ("/transactions", "/"):
            self._send(200, state.snapshot())
        else:
            self._send(404, {"error": "not found"})


async def _amain() -> None:
    logging.basicConfig(
        level=_env("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )
    telemetry = setup("vecn-ocpp-csms", {
        "energy.device.kind": "csms",
        "energy.device.id": _env("CSMS_ID", "csms-local-01"),
        "energy.device.protocol": "ocpp1.6j",
    })
    state = CsmsState()
    csms = Csms(state, telemetry)

    httpd = ThreadingHTTPServer(("0.0.0.0", _envi("HTTP_PORT", 9001)), _ControlHandler)
    httpd.state = state  # type: ignore[attr-defined]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover
            pass

    port = _envi("CSMS_PORT", 9000)
    LOG.info("OCPP 1.6J CSMS listening on ws://0.0.0.0:%d/ocpp/<chargePointId>, control http :%d",
             port, _envi("HTTP_PORT", 9001))
    async with serve(lambda conn: _connection(conn, csms), "0.0.0.0", port,
                     subprotocols=[ocpp16.SUBPROTOCOL]):
        await stop.wait()
    telemetry.shutdown()


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
