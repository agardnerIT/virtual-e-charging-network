"""Modbus -> OTLP poller: the device-facing half of the M1a slice.

    [device sim]  <--poll--  [this]  --OTLP/HTTP-->  [edge collector]  --> WAN -->  [hub]

One parameterised image serves any site; sites differ by environment, not by code (D10).
The register map is shared with the device (``sunspec.py``), so the encoder and decoder
cannot disagree.

What this process does, and what it deliberately leaves to the edge collector
--------------------------------------------------------------------------------
There is no Modbus receiver in OpenTelemetry core or contrib, so something has to speak the
protocol (D19). This poller is that something, and it stays as dumb as it can:

* it reads registers and applies the device's own scale factors and word order - because
  only the device-facing code can know them, and because the plan (plan.md D20) puts them
  here;
* it emits metric names that are still the *device's* names (``modbus.register.vpha``) and
  unit ``1``, with the raw integer and the scale factor carried as attributes;
* it derives a device-level validity state from what Modbus gives it - which is nothing, so
  validity is inferred from the transport, the sentinel values and the device's own clock;
* it deadbands at the source.

Everything semantic - canonical names, units, the A/B/C -> L1/L2/L3 mapping, the OTel quality
convention, the collector's own receive timestamp - happens in the edge collector, which is
exactly the argument for why an edge collector exists (D16, plan.md 5.3).
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import threading
import time
from dataclasses import dataclass

from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.metrics import Observation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from pymodbus.client import AsyncModbusTcpClient

import sunspec

LOG = logging.getLogger("poller")

#: Deadband per quantity group. This is the OT answer to metric cardinality: a value that
#: moves less than this is not worth a data point. It lives in the poller because the
#: collector has no stateful deadband processor (contrib has filter, but nothing that
#: compares the current value with the last one). See plan.md D22 and §11.
DEADBAND = {
    "voltage": 0.3,        # V   - well below a sag, well above sensor noise
    "current": 0.05,       # A
    "frequency": 0.005,    # Hz
    "power.real": 5.0,     # W
    "power.apparent": 5.0,  # VA
    "power.reactive": 5.0,  # var
    "power_factor": 0.002,  # 1
    "energy": 0.002,       # kWh
    "temperature": 0.05,   # degC
    # Inverter-side groups (M1b). The deadband follows the device's own scale factors: the
    # inverter reports power in kW, so a DC-power threshold below 1 kW would pass on rounding
    # noise alone.
    "dc.current": 0.5,          # A
    "dc.voltage": 0.5,          # V
    "dc.power": 50.0,           # W
    "dc.energy": 0.05,          # kWh
    "voltage.line": 0.3,        # V
    "irradiance": 2.0,          # W/m2
    "irradiance.poa": 2.0,      # W/m2
    # #23: the irradiance split, the transformer temperature and the ride-through values.
    "irradiance.diffuse": 2.0,   # W/m2
    "irradiance.direct": 2.0,    # W/m2
    "temperature.transformer": 0.1,  # degC - a big mass moves slowly
    "event.vendor": 0.0,         # a bitfield: report on change only
    "ride_through.voltage": 0.0,  # a connection-agreement constant
    "ride_through.time": 0.0,     # ms, also constant
    "ride_through.state": 0.0,    # 1 or 0
    "temperature.module": 0.1,  # degC
    "temperature.ambient": 0.1,  # degC
    # Microgrid-side groups (M3-2). The battery's state of charge is the most expensive
    # number to lose and the cheapest to report: 0.5 % is well below anything an operator
    # acts on, and the four-minute load cycle moves it by several percent.
    "state_of_charge": 0.5,     # %
    "rocof": 0.002,             # Hz/s - resolution of the device's own register
    "grid_forming": 0.0,        # 1 - which unit is the grid former; it never changes
    "power.max_charge": 0.0,    # W - a nameplate limit, not a measurement
    "power.max_discharge": 0.0, # W
    "energy.usable": 0.0,       # kWh - nameplate
    "source.id": 0.0,           # 1 - an identifier
    "power.available": 5.0,     # W - what a source could make; moves with the resource
    "availability": 0.0,        # 1
    "power.ev": 5.0,            # W
    "power.balance": 5.0,       # W
    "status": 0.0,              # 1 - a state, reported on change only
    "hold_reason": 0.0,         # 1
}

#: Emit a value that has not moved, occasionally, so a consumer can tell "steady" from
#: "gone". Classic OT heartbeat on a change-of-state report.
HEARTBEAT_S = 60.0

#: A reading whose device clock is older than this is reported as stale rather than good.
STALE_AFTER_S = 30.0

#: After startup, every point is emitted on every collection for this long. This is not
#: belt-and-braces: `make up` starts the pollers and the edge collectors together, so the
#: poller's *first* export can be refused because the edge collector's OTLP receiver is not
#: listening yet - and a point that never changes (a nameplate limit, a state, the grid-former
#: id) would then be invisible until the first heartbeat, a minute later. Measured on the
#: microgrid site: fourteen of twenty series were missing for the first minute (findings).
STARTUP_ALWAYS_S = 20.0


#: Modbus function 03 allows at most 125 registers in one request (0x7D). The M1a meter
#: image is 118 registers and always fit in one read; the M1b inverter image is 142, so the
#: poller MUST chunk. This is not a tuning knob - a 142-register read is an invalid request
#: and the device answers with an exception.
MODBUS_MAX_REGS_PER_READ = 120


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _envf(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _envi(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass
class _LastEmitted:
    value: float
    quality: str
    at: float


class PollerState:
    """The most recent successful read, plus how trustworthy it is."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.data: dict[str, sunspec.Decoded] = {}
        self.quality: dict[str, str] = {}
        self.device_time: int | None = None
        self.transport_ok = False
        self.last_error: str | None = None
        self.last_good_at: float = 0.0
        self.polls = 0
        self.failures = 0
        self.emitted = 0
        self.suppressed = 0

    def update(self, decoded: dict[str, sunspec.Decoded], now: float) -> None:
        clock = decoded.get("UnixTime")
        device_time = clock.raw if clock and clock.raw is not None else None
        age = (now - device_time) if device_time is not None else None
        quality: dict[str, str] = {}
        for name, point in decoded.items():
            if point.not_implemented:
                quality[name] = "bad"          # the point exists but has no value behind it
            elif age is not None and age > STALE_AFTER_S:
                quality[name] = "stale"        # the device's own clock says this is old
            else:
                quality[name] = "good"
        with self.lock:
            self.data = decoded
            self.quality = quality
            self.device_time = device_time
            self.transport_ok = True
            self.last_error = None
            self.last_good_at = now
            self.polls += 1
            self.failures = 0

    def mark_failed(self, error: str) -> None:
        with self.lock:
            self.transport_ok = False
            self.last_error = error
            # Values are retained on purpose: in OT a value that is present but not
            # trustworthy is more useful than a gap, as long as the quality says so.
            self.quality = {name: "comms-lost" for name in self.data}

    def snapshot(self):
        with self.lock:
            return dict(self.data), dict(self.quality), self.device_time


#: The steady configuration points from the vendor model are emitted too. They never change,
#: so they produce exactly one data point each - the crispest possible deadband demo, and
#: genuine provenance (which register word order did this device actually use?).
_CONFIG_POINTS = {
    "ClockOffsetS": ("modbus.device.clock_offset", "s"),
    "WordOrder": ("modbus.device.register_word_order", "1"),
}


class Emitter:
    """Turns the latest poll into OTLP observations, with per-point deadband."""

    def __init__(self, state: PollerState) -> None:
        self.state = state
        self.last: dict[str, _LastEmitted] = {}
        self.lock = threading.Lock()
        self.started_at = time.time()

    def _attributes(self, point: sunspec.Point, decoded: sunspec.Decoded,
                    quality: str, device_time: int | None) -> dict:
        # Every attribute here must be *static for the life of a series* or bounded to a few
        # values. This is not a style preference: a metric data point attribute is part of
        # series identity in every attribute-flattening backend. Two attributes that used to
        # be here changed on every single poll:
        #
        #   modbus.register.raw          - dropped. It is exactly recoverable from the value
        #                                  and the retained scale factor (value = raw x
        #                                  10^scale_factor), so the audit trail survives
        #                                  without carrying a per-sample copy of the reading.
        #                                  Measured cost of keeping it: 4 532 series in 15
        #                                  minutes in Prometheus, from one meter.
        return attrs

    def callback(self, point: sunspec.Point, metric_name: str):
        name = point.name
        deadband = DEADBAND.get(point.group or "", 0.0)

        def observe(_options):
            data, quality_map, device_time = self.state.snapshot()
            decoded = data.get(name)
            if decoded is None or decoded.value is None:
                return []
            quality = quality_map.get(name, "comms-lost")
            now = time.time()
            with self.lock:
                previous = self.last.get(name)
                if previous is not None:
                    unchanged = abs(decoded.value - previous.value) <= deadband
                    heartbeat_due = (now - previous.at) >= HEARTBEAT_S
                    starting_up = (now - self.started_at) < STARTUP_ALWAYS_S
                    if unchanged and quality == previous.quality and not heartbeat_due \
                            and not starting_up:
                        self.state.suppressed += 1
                        return []
                self.last[name] = _LastEmitted(decoded.value, quality, now)
                self.state.emitted += 1
            return [Observation(decoded.value, attributes=self._attributes(point, decoded,
                                                                           quality, device_time))]
        observe.__name__ = f"observe_{metric_name.replace('.', '_')}_{name.lower()}"
        return observe


def _metric_name(regmap: sunspec.RegisterMap, point: sunspec.Point) -> str:
    """Device-shaped metric name: `<namespace>.<point inside its model>`.

    The namespace comes from the register map, not from a hard-coded list, so a PV inverter
    writes `modbus.inverter.w` while a feeder meter writes `modbus.register.w` and the one
    shared edge config can tell them apart (D20). Repeating-block points use the model-level
    name and carry their block index as an attribute.
    """
    base = point.base_name or point.name
    return f"{regmap.namespaces[point.model]}.{base.lower()}"


def _device_resource(regmap: sunspec.RegisterMap) -> dict:
    """Resource attributes the poller is the only thing that can know: which device it reads."""
    device_id = _env("DEVICE_ID", "meter-rural-01")
    resource = {
        "service.name": "modbus-poller",
        "service.namespace": "vecn",
        "energy.device.kind": regmap.kind,
        "energy.device.id": device_id,
        "energy.device.model": _env("DEVICE_MODEL", "VECN SIM"),
        "energy.device.protocol": "modbus-tcp",
        "modbus.device.host": _env("MODBUS_HOST", "device-rural"),
        "modbus.device.unit_id": _envi("MODBUS_UNIT_ID", 1),
    }
    if regmap.kind == "meter":
        resource.update({
            "energy.meter.id": device_id,
            "energy.meter.phases": 3,
            "energy.meter.nominal_voltage": int(_envf("NOMINAL_VOLTAGE", 230.0)),
        })
    elif regmap.kind == "microgrid":
        # The microgrid's resource attributes say what makes it different: a named grid
        # former, a system base and an inertia constant. Those two numbers are the reason its
        # frequency moves, so they belong in the telemetry rather than only in a docstring.
        resource.update({
            "energy.microgrid.grid_former": "bess",
            "energy.microgrid.system_base_kw": int(_envf("SYSTEM_BASE_KW", 1500.0)),
            "energy.microgrid.inertia_h_s": _envf("INERTIA_H", 2.0),
            "energy.microgrid.nominal_frequency_hz": _envf("NOMINAL_HZ", 50.0),
            "energy.microgrid.islanded": True,
        })
    else:
        resource.update({
            "energy.inverter.id": device_id,
            "energy.inverter.phases": 3,
            "energy.inverter.nominal_voltage": int(_envf("NOMINAL_VOLTAGE", 398.0)),
            "energy.inverter.ac_rating_kw": int(_envf("AC_RATING_KW", 4200.0)),
            "energy.inverter.dc_rating_kwp": int(_envf("DC_RATING_KWP", 5300.0)),
        })
    return resource


def _build_meter(emitter: Emitter, regmap: sunspec.RegisterMap) -> MeterProvider:
    # OTLP/HTTP (see requirements.txt and plan.md D26 for why this hop is not gRPC). The
    # exporter wants the full signal URL, so the environment carries a base URL and the
    # metrics path is appended here rather than in every compose file.
    base = _env("OTLP_ENDPOINT", "http://edge-rural:4318")
    if "://" not in base:
        base = "http://" + base
    endpoint = base.rstrip("/") + "/v1/metrics"

    # The scan interval and the report interval are NOT the same thing, and conflating them
    # was a real bug (found by measuring, not by reading): this used to be
    # `interval_ms = POLL_INTERVAL_MS`, which made the poller open a gRPC export to the edge
    # collector once per second. That is 60 exports a minute per site over a link whose whole
    # story is that it is a 4G or satellite backhaul, and it is also what the deadband exists
    # to avoid - a steady register is not worth a message. Measured on the rural site: 1 106
    # metric points a minute crossing the wire where ~28 are real.
    #
    # The scan stays at 1 Hz (the deadband compares each fresh reading against the last
    # reported one, so a sag is still caught on the next scan), while the report cadence is
    # 5 s. That is also this pipeline's time resolution on the diagram, so it is a trade-off
    # to state rather than hide: a 20 s sag is four samples, not twenty.
    export_ms = _envi("OTEL_METRIC_EXPORT_INTERVAL_MS", 5000)

    host = _env("MODBUS_HOST", "device-rural")
    meter_provider = MeterProvider(
        resource=Resource.create(_device_resource(regmap)),
        metric_readers=[PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=endpoint),
            export_interval_millis=export_ms,
        )],
    )
    meter = meter_provider.get_meter("vecn.modbus-poller", version="0.1.0")

    # One observable gauge per point the device's map declares. Observable, not sync gauge,
    # because the callback can simply decline to produce an observation - which is how
    # deadband removes a data point from the export rather than just repeating the last
    # value. Vendor config points (device clock, word order) have no group and are emitted
    # explicitly below instead.
    # Group points that share a metric name BEFORE creating instruments. A repeating SunSpec
    # model (160, MPPT) maps several points onto ONE metric; the Python SDK returns the same
    # instrument when a name is created twice and silently drops the second callback, so one
    # gauge per point loses tracker 2 entirely. One instrument, every callback it needs.
    by_metric: dict[str, list[sunspec.Point]] = {}
    for point in regmap.points.values():
        if point.group is None:
            continue
        by_metric.setdefault(_metric_name(regmap, point), []).append(point)

    for metric_name, points in by_metric.items():
        meter.create_observable_gauge(
            metric_name,
            callbacks=[emitter.callback(point, metric_name) for point in points],
            unit="1",  # units are the edge collector's job, not the driver's
            description=f"SunSpec {points[0].model} points "
                        f"{', '.join(p.base_name or p.name for p in points)}, engineering "
                        f"values with the device's own scale factors applied",
        )

    # The measured clock error: how far the device's own clock is from the collector's, at the
    # moment of the read. This is what replaces the per-sample `energy.device.timestamp`
    # attribute (D51): one bounded number per device, which is the fact an operator actually
    # wants, instead of a timestamp per data point per poll.
    def observe_clock_error(_options):
        data, _quality, device_time = state.snapshot()
        if device_time is None:
            return []
        return [Observation(time.time() - float(device_time))]

    meter.create_observable_gauge(
        "modbus.device.clock_error", callbacks=[observe_clock_error], unit="s",
        description="The device's own clock minus this collector's, at the last read. Positive "
                    "means the device is behind. A stale device clock is quality=stale, not "
                    "comms-lost (D43).",
    )

    for name, (metric_name, unit) in _CONFIG_POINTS.items():
        point = regmap.points.get(name)
        if point is None:
            continue
        meter.create_observable_gauge(
            metric_name,
            callbacks=[emitter.callback(point, metric_name)],
            unit=unit,
            description=f"device configuration register {name}",
        )
    return meter_provider


async def _poll_loop(state: PollerState, regmap: sunspec.RegisterMap, host: str, port: int,
                    unit_id: int, interval_s: float, stop: asyncio.Event) -> None:
    client = AsyncModbusTcpClient(host, port=port, timeout=_envf("MODBUS_TIMEOUT_S", 3.0))
    while not stop.is_set():
        try:
            if not client.connected:
                await client.connect()
            registers: list[int] = []
            address = sunspec.BASE
            remaining = regmap.total_regs
            while remaining > 0:
                count = min(remaining, MODBUS_MAX_REGS_PER_READ)
                result = await client.read_holding_registers(
                    address=address, count=count, device_id=unit_id
                )
                if result.isError():
                    raise RuntimeError(f"modbus exception response: {result}")
                registers.extend(result.registers)
                address += count
                remaining -= count
            state.update(regmap.decode(registers), time.time())
        except Exception as exc:  # noqa: BLE001 - any transport failure is "comms-lost"
            state.mark_failed(f"{type(exc).__name__}: {exc}")
            state.failures += 1
            # A poller that cannot read its device must SAY SO. The first M1b inverter run
            # failed every read because a 142-register image exceeds Modbus' 125-register
            # limit, and the log stayed silent: the only status line is emitted after 30
            # *successful* polls, so a total failure produced exactly one line, at startup.
            if state.failures == 1 or state.failures % 30 == 0:
                LOG.warning("modbus read failed (%d consecutive): %s",
                            state.failures, state.last_error)
            client.close()

        if state.polls and state.polls % 30 == 0:
            LOG.info("polls=%d datapoints=%d deadbanded=%d last_error=%s",
                     state.polls, state.emitted, state.suppressed, state.last_error)
        await asyncio.sleep(interval_s)


async def _amain() -> None:
    logging.basicConfig(
        level=_env("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )
    interval_s = _envi("POLL_INTERVAL_MS", 1000) / 1000.0
    regmap = sunspec.map_for(_env("DEVICE_KIND", "meter"))
    state = PollerState()
    provider = _build_meter(Emitter(state), regmap)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - not on this platform
            pass

    LOG.info(
        "poller online: %s %s:%s unit %s -> %s every %dms (deadband on, heartbeat %.0fs)",
        regmap.kind, _env("MODBUS_HOST", "device-rural"), _envi("MODBUS_PORT", 5020),
        _envi("MODBUS_UNIT_ID", 1),
        _env("OTLP_ENDPOINT", "edge-rural:4317"), _envi("POLL_INTERVAL_MS", 1000), HEARTBEAT_S,
    )
    try:
        await _poll_loop(state, regmap, _env("MODBUS_HOST", "device-rural"), _envi("MODBUS_PORT", 5020),
                         _envi("MODBUS_UNIT_ID", 1), interval_s, stop)
    finally:
        provider.shutdown()


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
