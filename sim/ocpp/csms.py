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
        #: Optional hook, set by the CSMS so a stop is counted where the meter is (SLOs, #13).
        self.on_stop = None
        #: The last site profile pushed to each charge point, so a reconnect can be re-capped
        #: (M4, #12). OCPP has no "read my profile" call, which is why the CSMS must remember.
        self.profiles: dict[str, float] = {}
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
            # OCPP 1.6's StopTransaction reason is what a session-success SLO is measured on:
            # `Local` means the driver or the schedule ended it, `PowerLoss` means the supply
            # did. Anything else is `Other`. Bounded vocabulary, so it is a safe attribute.
            tx.stop_reason = payload.get("reason", "Local")
            if self.on_stop is not None:
                self.on_stop(tx.stop_reason)
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
        # CSMS-initiated CALLs need two things a request/response double does not: the live
        # connection per charge point, and a future per outstanding uniqueId (M4, #12).
        self.connections: dict[str, object] = {}
        self.pending: dict[str, "asyncio.Future[dict]"] = {}
        self.loop: "asyncio.AbstractEventLoop | None" = None
        self._unique = 0
        # The stop hook exists so the *state* object can stay a plain in-memory record while the
        # counting lives where the meter is (SLOs, #13).
        state.on_stop = self._count_stop
        self.state = state
        self.tracer = telemetry.tracer("vecn.ocpp-csms")
        self.meter = telemetry.meter("vecn.ocpp-csms")
        self._active = self.meter.create_observable_gauge(
            "ocpp.transactions.active",
            callbacks=[self._observe_active],
            unit="1",
            description="Transactions the CSMS currently believes are open.",
        )
        self._stopped = self.meter.create_counter(
            "ocpp.transactions.stopped", unit="1",
            description="Transactions the CSMS accepted a StopTransaction for, by OCPP 1.6 stop "
                        "reason. Session success rate = Local / all.",
        )
        self._started = self.meter.create_observable_gauge(
            "ocpp.transactions.total",
            callbacks=[self._observe_total],
            unit="1",
            description="Transactions the CSMS has started since it came up (process-scoped).",
        )
        LOG.info("CSMS online: blocked idTags=%s",
                 ",".join(sorted(self.state.blocked_id_tags)) or "(none)")

    def _count_stop(self, reason: str) -> None:
        # One counter, one bounded attribute. A per-transaction dimension here would mint a series
        # per session for a number that is only ever read as a ratio (D25, D36).
        self._stopped.add(1, {"ocpp.stop.reason": reason or "Other"})

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
            LOG.info("OCPP %s %s", direction, action, extra=extra)

    # -- CSMS-initiated calls (M4, #12) -------------------------------------------------
    def _next_unique(self) -> str:
        self._unique += 1
        return f"csms-{self._unique}"

    async def send_call(self, cp_id: str, action: str, payload: dict,
                        timeout_s: float = 5.0) -> dict:
        """Send a CALL to a charge point and await its CALLRESULT.

        This is the half of OCPP 1.6 that a passive test double never needs and a real CSMS cannot
        live without: profiles, reservations and remote starts are all CSMS-initiated. Frames are
        logged in both directions with `ocpp.message.direction`, so the conversation is traceable
        in the same places as everything else.
        """
        conn = self.connections.get(cp_id)
        if conn is None:
            raise RuntimeError(f"charge point {cp_id} is not connected")
        unique_id = self._next_unique()
        fut: asyncio.Future[dict] = self.loop.create_future()  # type: ignore[union-attr]
        self.pending[unique_id] = fut
        self._log_frame("send", action, unique_id, payload, cp_id, None)
        await conn.send(ocpp16.call(unique_id, action, payload))  # type: ignore[attr-defined]
        try:
            reply = await asyncio.wait_for(fut, timeout=timeout_s)
        finally:
            self.pending.pop(unique_id, None)
        self._log_frame("recv", f"CALLRESULT {action}", unique_id, reply, cp_id, None)
        return reply

    async def set_site_cap(self, cp_id: str, limit_a: float, profile_id: int = 1) -> dict:
        """Push a `ChargePointMaxProfile`: a cap on the whole charge point, which is what a site
        capacity limit is in OCPP 1.6 terms."""
        payload = {
            "connectorId": 0,
            "csChargingProfiles": {
                "chargingProfileId": profile_id,
                "stackLevel": 0,
                "chargingProfilePurpose": "ChargePointMaxProfile",
                "chargingProfileKind": "Absolute",
                "chargingSchedule": {
                    "chargingRateUnit": "A",
                    "chargingSchedulePeriod": [{"startPeriod": 0, "limit": round(limit_a, 1)}],
                },
            },
        }
        self.state.profiles[cp_id] = limit_a
        return await self.send_call(cp_id, "SetChargingProfile", payload)

    async def clear_site_cap(self, cp_id: str) -> dict:
        self.state.profiles.pop(cp_id, None)
        return await self.send_call(cp_id, "ClearChargingProfile",
                                    {"id": 1, "connectorId": 0,
                                     "chargingProfilePurpose": "ChargePointMaxProfile"})

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
        # A charge point that reconnects has no memory of a profile (ours keeps it in memory, but
        # a real one may have rebooted), and OCPP has no "get my profile". So the CSMS pushes it
        # again — after this response, because sending a CALL inside a handler would deadlock on
        # the same socket.
        if cp_id in self.state.profiles and self.loop is not None:
            self.loop.create_task(self._repush(cp_id, self.state.profiles[cp_id]))
        return {
            "status": "Accepted",
            "currentTime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "interval": _envi("CSMS_HEARTBEAT_INTERVAL_S", 60),
        }

    async def _repush(self, cp_id: str, limit_a: float) -> None:
        await asyncio.sleep(1.0)          # let the BootNotification response land first
        try:
            await self.set_site_cap(cp_id, limit_a)
            LOG.info("re-pushed the %.1f A site cap to %s after its boot", limit_a, cp_id)
        except Exception as exc:  # noqa: BLE001 - a boot that races a disconnect is normal
            LOG.warning("could not re-push the site cap to %s: %s", cp_id, exc)

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
    csms.connections[cp_id] = conn
    try:
        async for message in conn:
            try:
                mtype, unique_id, action_or_payload, payload = ocpp16.parse(message)
            except ocpp16.OCPPProtocolError as exc:
                LOG.warning("unparseable frame from %s: %s", cp_id, exc)
                continue
            if mtype in (ocpp16.CALL_RESULT, ocpp16.CALL_ERROR):
                # The reply to a CSMS-initiated CALL (SetChargingProfile, ClearChargingProfile).
                fut = csms.pending.get(unique_id)
                if fut is not None and not fut.done():
                    if mtype == ocpp16.CALL_ERROR:
                        fut.set_exception(RuntimeError(f"charge point refused: {payload}"))
                    else:
                        fut.set_result(action_or_payload or {})
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
        csms.connections.pop(cp_id, None)
        LOG.info("charge point %s disconnected", cp_id)


async def _http_control(state: "CsmsState", csms: "Csms", port: int) -> None:
    """A minimal HTTP control endpoint, in the event loop.

    It was a `ThreadingHTTPServer` first, with `asyncio.run_coroutine_threadsafe` to reach the OCPP
    connections. That deadlocked: the coroutine was submitted and never completed, so `make cap`
    timed out while the CSMS looked perfectly healthy. One loop, one thread, no bridge — at the cost
    of writing the request parsing by hand, which for five routes is not a cost.
    """

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=5)
            method, path, _ = (request_line.decode().split(" ") + ["", "", ""])[:3]
            headers: dict[str, str] = {}
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=5)
                if line in (b"\r\n", b"\n", b""):
                    break
                k, _, v = line.decode().partition(":")
                headers[k.strip().lower()] = v.strip()
            body = await asyncio.wait_for(
                reader.readexactly(int(headers.get("content-length") or 0)), timeout=5) \
                if int(headers.get("content-length") or 0) else b""

            status, payload = 404, {"error": "not found"}
            path = path.rstrip("/") or "/"
            if method == "GET" and path == "/health":
                snap = state.snapshot()
                status, payload = 200, {
                    "status": "ok", "chargePoints": len(snap["chargePoints"]),
                    "transactions": len(snap["transactions"]),
                    "open": sum(1 for t in snap["transactions"] if t["open"]),
                }
            elif method == "GET" and path in ("/transactions", "/"):
                status, payload = 200, state.snapshot()
            elif method == "GET" and path == "/profile":
                status, payload = 200, {"connected": sorted(csms.connections),
                                        "profiles": state.profiles}
            elif method == "POST" and path == "/profile":
                try:
                    limit_a = float(json.loads(body or b"{}").get("limit_a"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    status, payload = 400, {"error": "limit_a (amperes) is required"}
                else:
                    status, payload = 202, await _push_cap(csms, limit_a)
            elif method == "DELETE" and path == "/profile":
                status, payload = 202, await _push_cap(csms, None)
            out = json.dumps(payload, indent=2).encode()
            writer.write(b"HTTP/1.1 %d %s\r\nContent-Type: application/json\r\n"
                         b"Content-Length: %d\r\nConnection: close\r\n\r\n"
                         % (status, b"OK" if status < 300 else b"ERR", len(out)) + out)
            await writer.drain()
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ValueError,
                UnicodeDecodeError):
            # A probe, a half-open connection or a binary frame is not an error worth a log line on
            # a control endpoint: close and move on.
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "0.0.0.0", port)
    LOG.info("control endpoint on http://0.0.0.0:%d (GET /health, GET|POST|DELETE /profile)", port)
    async with server:
        await server.serve_forever()


async def _push_cap(csms: "Csms", limit_a: float | None) -> dict:
    """Push (or clear) a site cap on every connected charge point."""
    targets = sorted(csms.connections)
    if not targets:
        return {"accepted": False, "reason": "no charge point connected"}
    results: dict[str, object] = {}
    for cp_id in targets:
        try:
            results[cp_id] = (await csms.clear_site_cap(cp_id) if limit_a is None
                              else await csms.set_site_cap(cp_id, limit_a))
        except Exception as exc:  # noqa: BLE001 - report per CP, never crash the endpoint
            results[cp_id] = {"error": f"{type(exc).__name__}: {exc}"}
    return {"accepted": True, "limit_a": limit_a, "chargePoints": results}


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

    asyncio.create_task(_http_control(state, csms, _envi("HTTP_PORT", 9001)))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    csms.loop = loop
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
