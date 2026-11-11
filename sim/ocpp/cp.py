"""An OCPP 1.6J charge point that turns a charging session into a trace.

This is the third leg of the project's spine (plan.md §4): voltage sag -> charger derate ->
failed session. It is the only process in the repo that is *both* an OCPP client and a
Modbus client, and that is the point:

    [device-rural]  <-Modbus/1 Hz--  [this]  --OCPP/WS-->  [csms]
                                       |
                                       +--OTLP/HTTP--> [edge-rural] -> WAN -> [hub]

The two southbound paths ask different questions of the same physical event:

* over Modbus it reads the site meter's phase voltages, so a sag is measured at the supply
  the charger actually sees, not asserted by a test harness;
* over OCPP it reports what it did about the sag (`Current.Offered` falls, then the
  transaction ends for `PowerLoss`).

Nothing here invents causality. `make fault SITE=rural FAULT=voltage-sag` changes a register
on `device-rural`; the derate and the failed session are this process reacting to it.

Trace shape (M2-2, M2-3, M2-7)
------------------------------
One charging session is ONE trace, owned by the charge point:

    ocpp.session                      root, CLIENT, transaction id once allocated
    ├── ocpp.authorize
    ├── ocpp.start_transaction
    ├── ocpp.meter_values             one per 10 s report
    ├── energy.voltage_sag            starts below 0.94 pu, ends on recovery or stop
    └── ocpp.stop_transaction         reason=PowerLoss when the sag won

A boot is a property of the CP's *connection*, not of a transaction, so `BootNotification`
and the per-EVSE `StatusNotification`s get their own short trace (`ocpp.boot`) - M2-3.

The CSMS emits logs only. OCPP 1.6 has nowhere to put a W3C `traceparent`, so the two ends
cannot share a trace id; the CSMS's records are joined to the session by `ocpp.transaction.id`,
which is a first-class OCPP field. That is a finding, not a workaround to hide.

Exemplars (M2-7)
----------------
Prometheus can store exemplars, and the OTel Python SDK will attach them: the async-gauge
callback returns `Observation(value, attributes, context)`, and if that context holds a
sampled span the SDK emits an exemplar carrying its trace id. So each EVSE stashes the
current session span's context with every sample, and during a sag it stashes the sag span's
context instead. A 30-second sag therefore leaves an exemplar on the power metric that jumps
straight to the sag in the session trace - if the collector chain and Prometheus' OTLP
receiver preserve it. `make exemplar` tests that; the result is written down rather than
assumed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import threading
import time
import uuid
from contextlib import nullcontext
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from opentelemetry.metrics import Observation
from opentelemetry.trace import SpanKind, set_span_in_context, get_current_span
from pymodbus.client import AsyncModbusTcpClient
from websockets.asyncio.client import connect

import ocpp16
import sunspec
from telemetry import setup

LOG = logging.getLogger("ocpp.cp")

CALL_TIMEOUT_S = 10.0


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _envi(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _envf(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


#: Modbus function 03 reads at most 125 registers; the meter image is 118, but chunk anyway
#: so this client never depends on a device image staying small (docs/findings.md #18).
MODBUS_MAX_REGS_PER_READ = 120

#: AS 60038 nominal phase-to-neutral voltage at this site.
V_NOMINAL = 230.0
#: The derate curve. At or above FULL the charger offers nameplate; at or below ZERO it
#: offers nothing; in between it is linear in per-unit voltage. 0.94 pu is the AS 60038
#: lower utilisation limit (230 x 0.94 = 216 V), 0.80 is where a contactor drops out.
DERATE_FULL_PU = 0.94
DERATE_ZERO_PU = 0.80
#: Below ZERO for longer than this and the session is terminated with PowerLoss rather than
#: left hanging at zero current. Three seconds is faster than a driver notices and slower
#: than a single bad sample.
POWER_LOSS_HOLD_S = 3.0


# --------------------------------------------------------------------------------------
# Supply: the meter on the same site, read over Modbus
# --------------------------------------------------------------------------------------
@dataclass
class Supply:
    """What the site meter says the charger terminals are seeing."""

    voltage_v: tuple[float, float, float] = (V_NOMINAL, V_NOMINAL, V_NOMINAL)
    quality: str = "comms-lost"
    updated_at: float = 0.0
    reads: int = 0
    failures: int = 0

    @property
    def min_v(self) -> float:
        return min(self.voltage_v)

    @property
    def pu(self) -> float:
        return self.min_v / V_NOMINAL

    def snapshot(self) -> tuple[tuple[float, float, float], str, float]:
        return self.voltage_v, self.quality, self.updated_at


class SupplyMonitor:
    """Polls `device-rural`'s SunSpec meter for phase voltage.

    It uses `sunspec.map_for("meter")`, i.e. the same register map the poller uses, so the
    charge point decodes exactly what the device encodes. If the Modbus read fails the last
    voltages are *retained* and the quality becomes `comms-lost` - the same rule the poller
    applies (D20/D21). Retaining beats zeroing: a charger that suddenly reports 0 V is a
    charger reporting a fault it does not have.
    """

    def __init__(self) -> None:
        self.supply = Supply()
        self.host = _env("SUPPLY_MODBUS_HOST", "device-rural")
        self.port = _envi("SUPPLY_MODBUS_PORT", 5020)
        self.unit_id = _envi("SUPPLY_MODBUS_UNIT_ID", 1)
        self.interval_s = _envi("SUPPLY_POLL_INTERVAL_MS", 1000) / 1000.0
        self.regmap = sunspec.map_for("meter")

    async def run(self, stop: asyncio.Event) -> None:
        client = AsyncModbusTcpClient(self.host, port=self.port, timeout=3.0)
        while not stop.is_set():
            try:
                if not client.connected:
                    await client.connect()
                registers: list[int] = []
                address = sunspec.BASE
                remaining = self.regmap.total_regs
                while remaining > 0:
                    count = min(remaining, MODBUS_MAX_REGS_PER_READ)
                    result = await client.read_holding_registers(
                        address=address, count=count, device_id=self.unit_id
                    )
                    if result.isError():
                        raise RuntimeError(f"modbus exception response: {result}")
                    registers.extend(result.registers)
                    address += count
                    remaining -= count

                decoded = self.regmap.decode(registers)
                phases = []
                for name in ("VphA", "VphB", "VphC"):
                    point = decoded.get(name)
                    phases.append(float(point.value) if point and point.value is not None
                                  else V_NOMINAL)
                self.supply.voltage_v = (phases[0], phases[1], phases[2])
                self.supply.quality = "good"
                self.supply.updated_at = time.time()
                self.supply.reads += 1
                self.supply.failures = 0
            except Exception as exc:  # noqa: BLE001 - any transport failure keeps last values
                self.supply.quality = "comms-lost"
                self.supply.failures += 1
                if self.supply.failures == 1 or self.supply.failures % 30 == 0:
                    LOG.warning("supply meter unreadable (%d consecutive): %s",
                                self.supply.failures, f"{type(exc).__name__}: {exc}")
                client.close()
            await asyncio.sleep(self.interval_s)


# --------------------------------------------------------------------------------------
# OCPP client: CALL/RESULT correlation over one WebSocket
# --------------------------------------------------------------------------------------
class OcppClient:
    """The wire half: one WebSocket, uniqueId -> Future, inbound CALL handled."""

    def __init__(self, cp: "ChargePoint") -> None:
        self.cp = cp
        self.url = _env("CSMS_URL", "ws://csms:9000/ocpp/EVSE-RURAL-01")
        self.cp_id = _env("CP_ID", "EVSE-RURAL-01")
        self._conn = None
        self._pending: dict[str, asyncio.Future] = {}
        self._connected = asyncio.Event()
        self._next_id = 0

    async def run(self, stop: asyncio.Event) -> None:
        backoff = 1.0
        while not stop.is_set():
            try:
                async with connect(self.url, subprotocols=[ocpp16.SUBPROTOCOL],
                                   open_timeout=10, ping_interval=20,
                                   max_queue=64) as conn:
                    self._conn = conn
                    self._connected.set()
                    backoff = 1.0
                    LOG.info("connected to CSMS %s as %s (%s)", self.url, self.cp_id,
                             conn.subprotocol)
                    # Read *while* talking. Awaiting the boot exchange before entering the
                    # receive loop deadlocks: BootNotification waits for a CALLRESULT that
                    # only this loop can deliver.
                    reader = asyncio.create_task(self._read_loop(conn))
                    try:
                        await self.cp.on_connected()
                        await reader
                    finally:
                        reader.cancel()
                        try:
                            await reader
                        except (asyncio.CancelledError, Exception):  # noqa: BLE001
                            pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a CSMS restart is normal
                LOG.warning("CSMS connection ended: %s", f"{type(exc).__name__}: {exc}")
            finally:
                self._connected.clear()
                self._conn = None
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(ConnectionError("OCPP connection lost"))
                self._pending.clear()
            if stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 15.0)

    async def _read_loop(self, conn) -> None:
        async for raw in conn:
            await self._on_message(raw)

    async def _on_message(self, raw: str) -> None:
        try:
            mtype, unique_id, action_or_payload, payload = ocpp16.parse(raw)
        except ocpp16.OCPPProtocolError as exc:
            LOG.warning("unparseable frame from CSMS: %s", exc)
            return

        if mtype in (ocpp16.CALL_RESULT, ocpp16.CALL_ERROR):
            fut = self._pending.pop(unique_id, None)
            if fut is not None and not fut.done():
                if mtype == ocpp16.CALL_ERROR:
                    fut.set_exception(RuntimeError(
                        f"OCPP CALLERROR {action_or_payload}: {payload}"))
                else:
                    fut.set_result(action_or_payload or {})
            return

        # A CALL from the CSMS. This test double receives none, but a CP that cannot answer
        # one is not a CP; refuse it in protocol rather than dropping the frame silently.
        action, call_payload = action_or_payload, payload or {}
        self._log_frame("recv", action, unique_id, call_payload)
        self._log_frame("send", f"CALLERROR {action}", unique_id, {"error": "NotSupported"})
        await self._conn.send(ocpp16.error(unique_id, "NotSupported",
                                           f"{action} not supported by this charge point"))

    def _log_frame(self, direction: str, action: str, unique_id: str,
                   payload: dict) -> None:
        extra = {
            "ocpp.message.direction": direction,
            "ocpp.action": action,
            "ocpp.unique_id": unique_id,
            "ocpp.charge_point.id": self.cp_id,
            "ocpp.frame": json.dumps(payload, separators=(",", ":")),
        }
        tx_id = payload.get("transactionId")
        if tx_id:
            extra["ocpp.transaction.id"] = tx_id
        LOG.info("OCPP %s %s", direction, action, extra=extra)

    async def call(self, action: str, payload: dict, timeout: float = CALL_TIMEOUT_S) -> dict:
        """Send a CALL and await its CALLRESULT. Logs both frames in the current span."""
        await asyncio.wait_for(self._connected.wait(), timeout=timeout)
        self._next_id += 1
        unique_id = f"{self.cp_id}-{self._next_id}"
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[unique_id] = fut
        self._log_frame("send", action, unique_id, payload)
        await self._conn.send(ocpp16.call(unique_id, action, payload))
        try:
            result = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(unique_id, None)
            raise
        self._log_frame("recv", action, unique_id, result)
        return result


# --------------------------------------------------------------------------------------
# One EVSE: state, session, measurements
# --------------------------------------------------------------------------------------
@dataclass
class EvseSample:
    """The most recent measurement for one EVSE. Read by the metric callbacks."""

    evse_id: str
    connector_id: int
    state: str = "Available"
    power_w: float = 0.0
    offered_a: float = 0.0
    session_kwh: float = 0.0
    voltages: tuple[float, float, float] = (V_NOMINAL, V_NOMINAL, V_NOMINAL)
    supply_quality: str = "comms-lost"
    transaction_id: int | None = None
    session_id: str | None = None
    context: Any = None


class Evse:
    def __init__(self, cp: "ChargePoint", evse_id: str, connector_id: int,
                 id_tag: str, vehicle_id: str) -> None:
        self.cp = cp
        self.evse_id = evse_id
        self.connector_id = connector_id
        self.id_tag = id_tag
        self.vehicle_id = vehicle_id
        self.sample = EvseSample(evse_id, connector_id)
        self.task: asyncio.Task | None = None
        self.stop_requested = asyncio.Event()
        self.want_start = False

    @property
    def busy(self) -> bool:
        return self.task is not None and not self.task.done()

    async def run_once(self) -> None:
        """One session, start to finish. Re-raises nothing: a failed session is normal."""
        await self.cp.session(self)


# --------------------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------------------
class Metrics:
    """The four numbers that make a session visible, each on an EVSE.

    Only EVSE-stable attributes ride on these gauges. `ocpp.transaction.id`, the idTag and
    the vehicle id go on spans and logs, not on metrics: Prometheus flattens a data point
    attribute into series identity, so a per-session attribute mints a series per session
    and the store would grow forever for no extra query power. `energy.session.energy` is
    the exception - a session-scoped counter is the thing you want to query - and it is one
    series per session, which is honest.
    """

    def __init__(self, meter, evses: list[Evse], monitor: "SupplyMonitor") -> None:
        self.evses = evses
        self.monitor = monitor
        self.power = meter.create_observable_gauge(
            "energy.evse.power.real", callbacks=[self._power], unit="W",
            description="Real power delivered by one EVSE. Zero when the EVSE is not charging.",
        )
        self.offered = meter.create_observable_gauge(
            "energy.evse.current.offered", callbacks=[self._offered], unit="A",
            description="Current the EVSE is offering the vehicle after the supply-voltage "
                        "derate. This is the number that falls during a sag.",
        )
        self.voltage = meter.create_observable_gauge(
            "energy.evse.voltage", callbacks=[self._voltage], unit="V",
            description="Phase-to-neutral supply voltage at the EVSE, measured by the site "
                        "meter over Modbus. One data point per phase.",
        )
        self.state = meter.create_observable_gauge(
            "energy.evse.state", callbacks=[self._state], unit="1",
            description="OCPP 1.6 charge point status: 1 means the EVSE is in that state.",
        )
        self.session_energy = meter.create_observable_gauge(
            "energy.session.energy", callbacks=[self._session_energy], unit="kW.h",
            description="Energy delivered in the current OCPP transaction. One series per "
                        "transaction; the value is cumulative within it.",
        )

    @staticmethod
    def _attrs(s: EvseSample) -> dict:
        return {
            "energy.evse.id": s.evse_id,
            "energy.connector.id": s.connector_id,
            "energy.quality": s.supply_quality,
        }

    def _power(self, _options):
        return [Observation(s.power_w, self._attrs(s), s.context) for s in self._samples()]

    def _offered(self, _options):
        return [Observation(s.offered_a, self._attrs(s), s.context) for s in self._samples()]

    def _voltage(self, _options):
        out = []
        for s in self._samples():
            for phase, value in zip(("L1", "L2", "L3"), s.voltages):
                attrs = self._attrs(s)
                attrs["energy.phase"] = phase
                out.append(Observation(value, attrs, s.context))
        return out

    def _state(self, _options):
        out = []
        for s in self._samples():
            attrs = self._attrs(s)
            attrs["energy.evse.state.text"] = s.state
            out.append(Observation(1.0, attrs, s.context))
        return out

    def _session_energy(self, _options):
        out = []
        for s in self._samples():
            if s.transaction_id is None:
                continue
            attrs = self._attrs(s)
            attrs["ocpp.transaction.id"] = s.transaction_id
            attrs["energy.session.id"] = s.session_id or ""
            out.append(Observation(s.session_kwh, attrs, s.context))
        return out

    def _samples(self):
        """Refresh supply from the meter on every collection.

        An EVSE with no transaction is still connected to a live supply; its first cut left
        `supply_quality` at its initial `comms-lost` because only the charging loop updated
        it, so an idle-but-healthy EVSE rendered as lost. The supply measurement is
        site-wide, not session-scoped: read it from the monitor for every EVSE.
        """
        voltages, quality, _ = self.monitor.supply.snapshot()
        for e in self.evses:
            e.sample.voltages = voltages
            e.sample.supply_quality = quality
        return [e.sample for e in self.evses]


# --------------------------------------------------------------------------------------
# The charge point
# --------------------------------------------------------------------------------------
class ChargePoint:
    def __init__(self) -> None:
        self.telemetry = setup("vecn-ocpp-cp", {
            "energy.device.kind": "charge_point",
            "energy.device.id": _env("CP_ID", "EVSE-RURAL-01"),
            "energy.device.model": _env("CP_MODEL", "VECN SIM-AC22"),
            "energy.device.protocol": "ocpp1.6j",
            "energy.evse.count": _envi("CP_EVSE_COUNT", 2),
        })
        self.tracer = self.telemetry.tracer("vecn.ocpp-cp")
        self.monitor = SupplyMonitor()
        self.evses = [
            Evse(self, "EVSE01", 1, _env("CP_ID_TAG_1", "RFID-EV-0001"), "VECN-EV-0001"),
            Evse(self, "EVSE02", 2, _env("CP_ID_TAG_2", "RFID-EV-0002"), "VECN-EV-0002"),
        ]
        self.metrics = Metrics(self.telemetry.meter("vecn.ocpp-cp"), self.evses, self.monitor)
        self.client = OcppClient(self)
        self.loop: asyncio.AbstractEventLoop | None = None
        self.auto_evse = _env("CP_AUTO_EVSE", "EVSE01")
        self.session_duration_s = _envf("CP_SESSION_DURATION_S", 600.0)
        self.idle_gap_s = _envf("CP_IDLE_GAP_S", 20.0)
        self.meter_interval_s = _envf("CP_METER_INTERVAL_S", 10.0)
        self.vehicle_max_a = _envf("CP_VEHICLE_MAX_A", 32.0)

    # -- connection lifecycle ----------------------------------------------------------
    async def on_connected(self) -> None:
        """Boot trace (M2-3), then advertise every connector, exactly once per connection."""
        with self.tracer.start_as_current_span("ocpp.boot", kind=SpanKind.CLIENT,
                                               attributes={"ocpp.charge_point.id":
                                                           self.client.cp_id}) as span:
            with self._span("ocpp.boot_notification", "BootNotification"):
                resp = await self.client.call("BootNotification", {
                    "chargePointVendor": "VECN",
                    "chargePointModel": _env("CP_MODEL", "VECN SIM-AC22"),
                    "chargePointSerialNumber": self.client.cp_id,
                    "firmwareVersion": "0.1.0",
                })
                span.set_attribute("ocpp.boot.status", str(resp.get("status")))
                span.set_attribute("ocpp.boot.heartbeat_interval_s",
                                   resp.get("interval") or 0)
            for evse in self.evses:
                with self._span("ocpp.status_notification",
                                f"StatusNotification {evse.evse_id}"):
                    await self.client.call("StatusNotification", {
                        "connectorId": evse.connector_id,
                        "status": "Available",
                        "errorCode": "NoError",
                        "timestamp": _iso(),
                    })

        if self.auto_evse:
            for evse in self.evses:
                if evse.evse_id == self.auto_evse:
                    asyncio.create_task(self._auto_loop(evse))

    async def _auto_loop(self, evse: Evse) -> None:
        """Keep one EVSE busy so `make up` ends in a live session, and the demo repeats.

        After a session ends it waits the idle gap and refuses to start while the supply is
        still below the derate knee. Restarting immediately into a live sag was the first
        cut, and it produced three failed transactions in twenty seconds - a charge point
        that retries into a fault is not something an operator would accept.
        """
        await asyncio.sleep(2.0)
        while True:
            if not evse.busy:
                await asyncio.sleep(self.idle_gap_s)
                while self.monitor.supply.pu < DERATE_FULL_PU:
                    await asyncio.sleep(1.0)
                if not evse.busy:
                    await self.start(evse)
            await asyncio.sleep(1.0)

    # -- operator control (HTTP on :8080) ----------------------------------------------
    async def start(self, evse: Evse) -> dict:
        if evse.busy:
            return {"evse": evse.evse_id, "status": evse.sample.state,
                    "note": "session already running"}
        evse.stop_requested.clear()
        evse.want_start = False
        evse.task = asyncio.create_task(evse.run_once())
        return {"evse": evse.evse_id, "status": "starting"}

    async def stop(self, evse: Evse) -> dict:
        if not evse.busy:
            return {"evse": evse.evse_id, "status": evse.sample.state,
                    "note": "no session running"}
        evse.stop_requested.set()
        return {"evse": evse.evse_id, "status": "stopping"}

    def evse_by_id(self, evse_id: str) -> Evse | None:
        for e in self.evses:
            if e.evse_id.lower() == evse_id.lower():
                return e
        return None

    def _span(self, name: str, action: str, **attrs):
        """A child span for one OCPP exchange. Logs inside it carry its trace id."""
        return self.tracer.start_as_current_span(
            name, kind=SpanKind.CLIENT,
            attributes={"ocpp.action": action, **attrs},
        )

    # -- the session -------------------------------------------------------------------
    async def session(self, evse: Evse) -> None:
        session_id = uuid.uuid4().hex[:12]
        started = time.time()
        span = self.tracer.start_span("ocpp.session", kind=SpanKind.CLIENT, attributes={
            "energy.evse.id": evse.evse_id,
            "energy.connector.id": evse.connector_id,
            "energy.session.id": session_id,
            "ocpp.id_tag": evse.id_tag,
            "energy.vehicle.id": evse.vehicle_id,
        })
        sample = evse.sample
        sample.session_id = session_id
        sample.state = "Preparing"
        stop_reason = "Local"
        tx_id: int | None = None
        sag_span = None
        sag_started_at: float | None = None
        severe_since: float | None = None
        energy_kwh = 0.0
        meter_start_kwh = _envf("CP_METER_BASE_KWH", 1200.0)
        session_context = set_span_in_context(span)
        sample.context = session_context

        with _use(span):
            try:
                # ---- Authorize ---------------------------------------------------
                with self._span("ocpp.authorize", "Authorize",
                                **{"ocpp.id_tag": evse.id_tag}) as auth:
                    resp = await self.client.call("Authorize", {"idTag": evse.id_tag})
                    status = (resp.get("idTagInfo") or {}).get("status", "Invalid")
                    auth.set_attribute("ocpp.id_tag.status", status)
                if status != "Accepted":
                    sample.state = "Faulted"
                    stop_reason = "Other"
                    span.set_attribute("energy.session.outcome", "authorization_rejected")
                    return

                # ---- StartTransaction -------------------------------------------
                with self._span("ocpp.start_transaction", "StartTransaction") as st:
                    resp = await self.client.call("StartTransaction", {
                        "connectorId": evse.connector_id,
                        "idTag": evse.id_tag,
                        "meterStart": int(round(meter_start_kwh * 1000)),
                        "timestamp": _iso(),
                    })
                    tx_id = resp.get("transactionId")
                    st.set_attribute("ocpp.transaction.id", tx_id or 0)
                    st.set_attribute("ocpp.id_tag.status",
                                     (resp.get("idTagInfo") or {}).get("status", "Invalid"))
                if not tx_id:
                    sample.state = "Faulted"
                    stop_reason = "Other"
                    span.set_attribute("energy.session.outcome", "start_rejected")
                    return

                span.set_attribute("ocpp.transaction.id", tx_id)
                sample.transaction_id = tx_id
                sample.state = "Charging"
                await self._status(evse, "Charging", "NoError")

                # ---- charge ------------------------------------------------------
                last_tick = time.time()
                last_meter_report = 0.0
                while True:
                    now = time.time()
                    dt = max(0.0, now - last_tick)
                    last_tick = now

                    if evse.stop_requested.is_set():
                        stop_reason = "Local"
                        break
                    if now - started >= self.session_duration_s:
                        stop_reason = "Local"
                        break

                    voltages, quality, _ = self.monitor.supply.snapshot()
                    pu = min(voltages) / V_NOMINAL
                    offered = self.vehicle_max_a * _clamp(
                        (pu - DERATE_ZERO_PU) / (DERATE_FULL_PU - DERATE_ZERO_PU))
                    offered = round(offered, 1)

                    # The sag is a span in the session, not an event: it has a start, a
                    # depth, a derate response and an end. Starting it before the drop means
                    # the exemplar captured during the drop points at it (M2-7).
                    if pu < DERATE_FULL_PU and sag_span is None:
                        sag_span = self.tracer.start_span(
                            "energy.voltage_sag", kind=SpanKind.INTERNAL,
                            attributes={
                                "energy.evse.id": evse.evse_id,
                                "energy.voltage.nominal": V_NOMINAL,
                                "energy.voltage.pu": round(pu, 3),
                                "ocpp.transaction.id": tx_id,
                            })
                        sag_span.add_event("sag.start", {
                            "energy.voltage.l1": round(voltages[0], 1),
                            "energy.voltage.l2": round(voltages[1], 1),
                            "energy.voltage.l3": round(voltages[2], 1),
                            "energy.voltage.pu": round(pu, 3),
                        })
                        sag_started_at = now
                        LOG.warning("supply sag: %.1f V (%.2f pu) - derating to %.1f A",
                                    min(voltages), pu, offered)

                    if sag_span is not None:
                        # While the sag is the thing happening, the exemplar points at it.
                        sample.context = set_span_in_context(sag_span)
                        sag_span.set_attribute("energy.derate.offered_a", offered)
                        sag_span.set_attribute("energy.voltage.min_pu", round(min(
                            sag_span.attributes.get("energy.voltage.min_pu", pu), pu), 3))

                    if pu >= DERATE_FULL_PU and sag_span is not None:
                        sag_span.set_attribute("energy.sag.duration_s",
                                               round(now - (sag_started_at or now), 1))
                        sag_span.add_event("sag.end", {"energy.voltage.pu": round(pu, 3)})
                        sag_span.end()
                        sag_span = None
                        sag_started_at = None
                        severe_since = None
                        sample.context = session_context

                    # Severe: below the contactor-drop threshold for longer than the hold.
                    if pu < DERATE_ZERO_PU:
                        severe_since = severe_since or now
                        if now - severe_since >= POWER_LOSS_HOLD_S:
                            stop_reason = "PowerLoss"
                            span.set_attribute("energy.session.outcome", "failed_power_loss")
                            break
                    else:
                        severe_since = None

                    power_w = sum(voltages) * offered * 0.98 if offered > 0 else 0.0
                    energy_kwh += power_w * dt / 3_600_000.0
                    sample.power_w = round(power_w, 1)
                    sample.offered_a = offered
                    sample.session_kwh = round(energy_kwh, 4)
                    sample.voltages = voltages
                    sample.supply_quality = quality
                    sample.state = "Charging"
                    sample.transaction_id = tx_id

                    if now - last_meter_report >= self.meter_interval_s:
                        last_meter_report = now
                        await self._meter_values(evse, tx_id, meter_start_kwh + energy_kwh,
                                                 power_w, offered, voltages)

                    await asyncio.sleep(1.0)

                # ---- StopTransaction --------------------------------------------
                with self._span("ocpp.stop_transaction", "StopTransaction",
                                **{"ocpp.transaction.id": tx_id,
                                   "ocpp.stop.reason": stop_reason}) as stop:
                    await self.client.call("StopTransaction", {
                        "transactionId": tx_id,
                        "meterStop": int(round((meter_start_kwh + energy_kwh) * 1000)),
                        "timestamp": _iso(),
                        "reason": stop_reason,
                    })
                    stop.set_attribute("energy.session.energy_kwh", round(energy_kwh, 3))
                if stop_reason == "PowerLoss":
                    span.add_event("session.failed", {
                        "energy.session.reason": "PowerLoss",
                        "energy.session.energy_kwh": round(energy_kwh, 3),
                    })
                span.set_attribute("ocpp.stop.reason", stop_reason)
                span.set_attribute("energy.session.energy_kwh", round(energy_kwh, 3))
                span.set_attribute("energy.session.duration_s", round(time.time() - started, 1))

            except Exception as exc:  # noqa: BLE001 - a dropped link must not kill the CP
                stop_reason = "Other"
                span.record_exception(exc)
                span.set_attribute("energy.session.outcome", "error")
                LOG.warning("session on %s ended with error: %s", evse.evse_id,
                            f"{type(exc).__name__}: {exc}")
            finally:
                if sag_span is not None:
                    sag_span.set_attribute("energy.sag.duration_s",
                                           round(time.time() - (sag_started_at or time.time()), 1))
                    sag_span.set_attribute("energy.sag.interrupted_session", True)
                    sag_span.end()
                if tx_id is not None:
                    evse.sample.state = "Finishing"
                    try:
                        await self._status(evse, "Finishing", "NoError")
                    except Exception:  # noqa: BLE001
                        pass
                sample = evse.sample
                sample.state = "Available"
                sample.power_w = 0.0
                sample.offered_a = 0.0
                sample.transaction_id = None
                sample.session_id = None
                sample.context = None
                span.end()

        # Advertise the connector free again, outside the session span: this is the start
        # of the next session, not part of this one.
        try:
            await self._status(evse, "Available", "NoError")
        except Exception:  # noqa: BLE001
            pass
        LOG.info("session on %s finished: reason=%s energy=%.3f kWh", evse.evse_id,
                 stop_reason, energy_kwh)
        if not evse.stop_requested.is_set() and evse.evse_id != self.auto_evse:
            pass
        # An auto EVSE restarts after the idle gap; _auto_loop owns that timing.

    async def _meter_values(self, evse: Evse, tx_id: int, cumulative_kwh: float,
                            power_w: float, offered_a: float,
                            voltages: tuple[float, float, float]) -> None:
        with self._span("ocpp.meter_values", "MeterValues",
                        **{"ocpp.transaction.id": tx_id}) as span:
            await self.client.call("MeterValues", {
                "connectorId": evse.connector_id,
                "transactionId": tx_id,
                "meterValue": [{
                    "timestamp": _iso(),
                    "sampledValue": [
                        {"value": f"{cumulative_kwh * 1000:.1f}",
                         "measurand": "Energy.Active.Import.Register",
                         "unit": "Wh", "context": "Sample.Periodic"},
                        {"value": f"{power_w:.0f}", "measurand": "Power.Active.Import",
                         "unit": "W", "context": "Sample.Periodic"},
                        {"value": f"{offered_a:.1f}", "measurand": "Current.Offered",
                         "unit": "A", "context": "Sample.Periodic"},
                        {"value": f"{min(voltages):.1f}", "measurand": "Voltage",
                         "unit": "V", "phase": "L1", "context": "Sample.Periodic"},
                    ],
                }],
            })
            span.set_attribute("energy.session.energy_kwh", round(cumulative_kwh, 3))
            span.set_attribute("energy.evse.current.offered_a", offered_a)

    async def _status(self, evse: Evse, status: str, error_code: str) -> None:
        with self._span("ocpp.status_notification", f"StatusNotification {evse.evse_id}"):
            await self.client.call("StatusNotification", {
                "connectorId": evse.connector_id,
                "status": status,
                "errorCode": error_code,
                "timestamp": _iso(),
            })

    def health(self) -> dict:
        v, quality, updated = self.monitor.supply.snapshot()
        return {
            "status": "ok",
            "chargePointId": self.client.cp_id,
            "connected": self.client._connected.is_set(),
            "supply": {
                "voltageV": [round(x, 1) for x in v],
                "pu": round(min(v) / V_NOMINAL, 3),
                "quality": quality,
                "ageS": round(time.time() - updated, 1) if updated else None,
                "modbusReads": self.monitor.supply.reads,
                "modbusFailures": self.monitor.supply.failures,
            },
            "evses": [{
                "evseId": e.sample.evse_id,
                "connectorId": e.sample.connector_id,
                "state": e.sample.state,
                "powerW": e.sample.power_w,
                "offeredA": e.sample.offered_a,
                "sessionKWh": e.sample.session_kwh,
                "transactionId": e.sample.transaction_id,
                "sessionId": e.sample.session_id,
            } for e in self.evses],
        }


# --------------------------------------------------------------------------------------
# HTTP control, same shape as the device's faultctl surface: exec'd into, never published
# --------------------------------------------------------------------------------------
class _ControlHandler(BaseHTTPRequestHandler):
    server_version = "vecn-ocpp-cp/0.1"

    def log_message(self, fmt, *args):
        LOG.debug("http %s", fmt % args)

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _target(self) -> tuple[Any, str]:
        cp: ChargePoint = self.server.cp  # type: ignore[attr-defined]
        query = parse_qs(urlparse(self.path).query)
        evse = cp.evse_by_id(query.get("evse", [_env("CP_AUTO_EVSE", "EVSE01")])[0])
        return cp, evse

    def do_GET(self):  # noqa: N802
        cp: ChargePoint = self.server.cp  # type: ignore[attr-defined]
        path = urlparse(self.path).path
        if path == "/health":
            self._send(200, cp.health())
        elif path == "/":
            self._send(200, cp.health())
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        cp, evse = self._target()
        path = urlparse(self.path).path
        if evse is None:
            self._send(404, {"error": "unknown evse"})
            return
        if path not in ("/session/start", "/session/stop"):
            self._send(404, {"error": "not found"})
            return
        coro = cp.start(evse) if path.endswith("start") else cp.stop(evse)
        try:
            result = asyncio.run_coroutine_threadsafe(coro, cp.loop).result(timeout=10)
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})
            return
        self._send(202, result)


# --------------------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------------------
def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def _iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _use(span):
    """`use_span` without ending the span - the session span is ended in `finally`."""
    from opentelemetry import trace
    return trace.use_span(span, end_on_exit=False)


async def _amain() -> None:
    logging.basicConfig(
        level=_env("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )
    cp = ChargePoint()
    cp.loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            cp.loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover
            pass

    httpd = ThreadingHTTPServer(("0.0.0.0", _envi("HTTP_PORT", 8080)), _ControlHandler)
    httpd.cp = cp  # type: ignore[attr-defined]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    LOG.info("charge point %s online: OCPP -> %s, supply <- %s:%d, control http :%d",
             cp.client.cp_id, cp.client.url, cp.monitor.host, cp.monitor.port,
             _envi("HTTP_PORT", 8080))
    tasks = [
        asyncio.create_task(cp.monitor.run(stop)),
        asyncio.create_task(cp.client.run(stop)),
    ]
    await stop.wait()
    for task in tasks:
        task.cancel()
    cp.telemetry.shutdown()


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
