"""Site/feeder meter simulator - a SunSpec-style Modbus TCP device.

This is the "device" end of the M1a vertical slice:

    [this]  <--poll--  [poller]  --OTLP-->  [edge collector]  --> WAN -->  [hub]

It holds a real register file (see ``sunspec.py``): integers with separate scale-factor
registers, a 32-bit energy accumulator split across two registers, and a device clock - but
no timestamps, no units and no quality flags anywhere, because Modbus has none. Everything
an operator needs beyond the integer is invented downstream by the poller and the collector.
That absence is the teaching point.

Values are computed from a small electrical model (below) and refreshed on every read, so
the register file is live rather than a fixture. The only app-level fault is a voltage sag,
driven over a tiny HTTP control endpoint, because the plan's narrative spine is
"voltage sag -> charger derate -> failed session" and M1a needs the first step of it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pymodbus.server import StartAsyncTcpServer
from pymodbus.simulator import DataType, SimData, SimDevice

import sunspec

LOG = logging.getLogger("site")


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


# --------------------------------------------------------------------------------------
# The fault: one voltage sag. Deliberately not a framework (NEXT.md 4.4).
# --------------------------------------------------------------------------------------
@dataclass
class FaultState:
    """Thread-safe holder for the one fault this device supports."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    kind: str | None = None
    depth: float = 1.0        # multiplier applied to per-phase voltage (1.0 = healthy)
    phase_bias: float = 1.02  # L3 dips slightly less than L1/L2 - real sags are untidy
    started: float = 0.0      # monotonic
    until: float = 0.0

    def start_sag(self, depth: float, duration_s: float, phase_bias: float) -> dict:
        now = time.monotonic()
        with self.lock:
            self.kind = "voltage-sag"
            self.depth = max(0.05, min(0.99, depth))
            self.phase_bias = phase_bias
            self.started = now
            self.until = now + max(1.0, duration_s)
            return self.snapshot_locked(now)

    def clear(self) -> None:
        with self.lock:
            self.kind = None

    def active(self, now: float) -> bool:
        with self.lock:
            if self.kind is None:
                return False
            if now >= self.until:
                self.kind = None
                return False
            return True

    def snapshot_locked(self, now: float) -> dict:
        return {
            "kind": self.kind,
            "depth": self.depth,
            "phase_bias": self.phase_bias,
            "remaining_s": max(0.0, round(self.until - now, 3)) if self.kind else 0.0,
        }

    def snapshot(self) -> dict:
        with self.lock:
            now = time.monotonic()
            if self.kind is not None and now >= self.until:
                self.kind = None
            return self.snapshot_locked(now)


# --------------------------------------------------------------------------------------
# The electrical model.
#
# A small rural charging site on a long, weak feeder:
#   * 230 V phase-neutral nominal, 400 V line-line, 50 Hz (AS 60038)
#   * three phases, deliberately unbalanced - single-phase AC posts do that
#   * PF 0.95-0.99 lagging, so real power < apparent power and reactive power is positive
#   * a small load cycle so the numbers move without the sag
#
# Sign conventions, stated explicitly because getting them wrong silently inverts a
# dashboard:
#   * P (W)   > 0 means power flowing INTO the site from the grid  (import)
#   * Q (var) > 0 means inductive / lagging current (the usual case for a load)
#   * PF      is reported unsigned 0..1, with Q's sign carrying lead/lag
# --------------------------------------------------------------------------------------
class MeterModel:
    def __init__(self, device_id: str, regmap: sunspec.RegisterMap, fault: FaultState,
                 nominal_voltage: float, skew_s: float,
                 word_order: int, energy_base_kwh: float) -> None:
        self.device_id = device_id
        self.regmap = regmap
        self.fault = fault
        self.nominal_voltage = nominal_voltage
        self.skew_s = skew_s
        self.word_order = word_order
        self._t0 = time.monotonic()
        self._last_t = self._t0
        self.energy_import_kwh = energy_base_kwh
        self.energy_export_kwh = 12.4  # this site is a load; export is near zero and static
        self._rng = random.Random(20261014)

    def _noise(self, scale: float) -> float:
        return self._rng.gauss(0.0, scale)

    def sample(self) -> dict[str, float]:
        """One snapshot of engineering values, keyed by SunSpec point name."""
        now = time.monotonic()
        dt = max(0.0, now - self._last_t)
        self._last_t = now
        t = now - self._t0

        # Load cycle: two slow sines so the currents wander on a ~1-2 minute scale.
        load = 0.55 + 0.28 * math.sin(t / 12.0) + 0.06 * math.sin(t / 3.1 + 1.3)
        load = max(0.05, min(1.0, load))

        # Per-phase voltage: healthy values sit within AS 60038's ~216-253 V window.
        v_a = self.nominal_voltage + 1.6 * math.sin(t / 9.0) + self._noise(0.25)
        v_b = self.nominal_voltage - 0.9 * math.sin(t / 11.0) + self._noise(0.25)
        v_c = self.nominal_voltage + 2.0 * math.sin(t / 13.0 + 0.7) + self._noise(0.25)

        # Per-phase current: unbalanced on purpose (L1 pushed hardest).
        i_a = (18.0 + 11.0 * load) + self._noise(0.25)
        i_b = (13.0 + 9.0 * load) + self._noise(0.25)
        i_c = (9.0 + 7.0 * load) + self._noise(0.25)
        i_a, i_b, i_c = (max(0.0, x) for x in (i_a, i_b, i_c))

        # Power factor: 0.95-0.99, slightly different per phase.
        pf_a = min(0.99, max(0.95, 0.972 + 0.012 * math.sin(t / 15.0) + self._noise(0.002)))
        pf_b = min(0.99, max(0.95, 0.966 + 0.012 * math.sin(t / 17.0 + 0.5) + self._noise(0.002)))
        pf_c = min(0.99, max(0.95, 0.960 + 0.012 * math.sin(t / 19.0 + 1.0) + self._noise(0.002)))

        if self.fault.active(now):
            f = self.fault.snapshot()
            v_a *= f["depth"]
            v_b *= f["depth"]
            v_c *= min(1.0, f["depth"] * f["phase_bias"])

        # Per-phase P/Q/S. S = V x I; P = S x PF; Q = sqrt(S^2 - P^2).
        # For a balanced set this reduces to the familiar P = sqrt(3) x V_LL x I x PF.
        va, vb, vc = v_a * i_a, v_b * i_b, v_c * i_c
        pa, pb, pc = va * pf_a, vb * pf_b, vc * pf_c
        qa = math.sqrt(max(va * va - pa * pa, 0.0))
        qb = math.sqrt(max(vb * vb - pb * pb, 0.0))
        qc = math.sqrt(max(vc * vc - pc * pc, 0.0))

        p_total = pa + pb + pc
        s_total = va + vb + vc
        q_total = qa + qb + qc

        # Energy accumulates from real power. The register is in Wh behind TotWh_SF=-3.
        self.energy_import_kwh += (p_total * dt) / 3_600_000.0

        hz = min(50.05, max(49.95, 50.0 + 0.03 * math.sin(t / 40.0) + self._noise(0.004)))
        temp = 27.4 + 1.8 * math.sin(t / 47.0) + self._noise(0.05)

        return {
            "A": (i_a + i_b + i_c) / 3.0,
            "AphA": i_a, "AphB": i_b, "AphC": i_c,
            "V": (v_a + v_b + v_c) / 3.0,
            "VphA": v_a, "VphB": v_b, "VphC": v_c,
            "Hz": hz,
            "W": p_total, "WphA": pa, "WphB": pb, "WphC": pc,
            "VA": s_total, "VAphA": va, "VAphB": vb, "VAphC": vc,
            "VAR": q_total, "VARphA": qa, "VARphB": qb, "VARphC": qc,
            "PF": p_total / s_total if s_total else 0.0,
            "PFphA": pf_a, "PFphB": pf_b, "PFphC": pf_c,
            "TotWhImp": self.energy_import_kwh,
            "TotWhExp": self.energy_export_kwh,
            "TmpCab": temp,
        }

    def uptime_s(self) -> float:
        return round(time.monotonic() - self._t0, 1)

    def device_time(self) -> int:
        """The device's own claimed clock. Sites like this have no reliable NTP, so it is
        deliberately skewed (D12, plan.md 5.5)."""
        return int(time.time() + self.skew_s)

    def image(self) -> list[int]:
        return self.regmap.encode(
            self.sample(),
            device_time=self.device_time(),
            word_order=self.word_order,
            clock_offset_s=int(self.skew_s),
        )

    def health(self) -> dict:
        return {
            "status": "ok",
            "device_id": self.device_id,
            "device_kind": "meter",
            "uptime_s": self.uptime_s(),
            "device_clock_skew_s": self.skew_s,
            "register_count": self.regmap.total_regs,
            "fault": self.fault.snapshot(),
        }


# --------------------------------------------------------------------------------------
# HTTP control endpoint. Small on purpose.
# --------------------------------------------------------------------------------------
class _ControlHandler(BaseHTTPRequestHandler):
    server_version = "vecn-site-sim/0.1"

    def log_message(self, fmt, *args):  # quieter, and consistent with the rest of the logs
        LOG.debug("http %s", fmt % args)

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            return {}

    def do_GET(self):  # noqa: N802
        model = self.server.model  # type: ignore[attr-defined]
        if self.path == "/health":
            self._send(200, model.health())
        elif self.path == "/fault" and hasattr(model, "fault"):
            self._send(200, model.fault.snapshot())
        elif self.path == "/curtail" and hasattr(model, "curtail"):
            self._send(200, model.curtail.snapshot())
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        model = self.server.model  # type: ignore[attr-defined]
        path = self.path.rstrip("/")
        body = self._read_body()
        if path == "/fault/voltage-sag" and hasattr(model, "fault"):
            state = model.fault.start_sag(
                depth=float(body.get("depth", 0.78)),
                duration_s=float(body.get("duration_s", 20.0)),
                phase_bias=float(body.get("phase_bias", 1.02)),
            )
            LOG.warning("VOLTAGE SAG injected: depth=%.2f pu for %.0fs", state["depth"], state["remaining_s"])
            self._send(202, {"accepted": True, "fault": state})
        elif path == "/curtail" and hasattr(model, "curtail"):
            state = model.curtail.set_limit(
                pct=float(body.get("pct", 100.0)),
                duration_s=body.get("duration_s"),
            )
            LOG.warning("EXPORT LIMIT set to %.1f%% of nameplate", state["export_limit_pct"])
            self._send(202, {"accepted": True, "curtail": state})
        else:
            self._send(404, {"error": "not found"})

    def do_DELETE(self):  # noqa: N802
        model = self.server.model  # type: ignore[attr-defined]
        path = self.path.rstrip("/")
        if path == "/fault/voltage-sag" and hasattr(model, "fault"):
            model.fault.clear()
            self._send(200, {"cleared": True})
        elif path == "/curtail" and hasattr(model, "curtail"):
            model.curtail.clear()
            self._send(200, {"cleared": True})
        else:
            self._send(404, {"error": "not found"})


# --------------------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------------------
async def _serve_modbus(model, host: str, port: int, unit_id: int) -> None:
    total_regs = model.regmap.total_regs

    async def refresh(_fc, start_address, _address, _count, current_registers, _set_values):
        # Called by pymodbus on every read: rebuild the live image and hand back the slice
        # the client asked for. This is the seam where a real device's scan task would be.
        image = model.image()
        first = start_address - sunspec.BASE
        for i in range(len(current_registers)):
            j = first + i
            current_registers[i] = image[j] if 0 <= j < len(image) else 0
        return None

    device = SimDevice(
        id=unit_id,
        simdata=[SimData(address=sunspec.BASE, count=total_regs, values=0,
                         datatype=DataType.REGISTERS)],
        action=refresh,
    )
    await StartAsyncTcpServer(context=device, address=(host, port))


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )

    device_id = _env("DEVICE_ID", "meter-rural-01")
    kind = _env("DEVICE_KIND", "meter")
    regmap = sunspec.map_for(kind)
    word_order = sunspec.WORD_ORDER_LOW_FIRST if _env("WORD_ORDER", "low").lower().startswith("l") \
        else sunspec.WORD_ORDER_HIGH_FIRST

    if kind == "inverter":
        # Imported here so the meter path never pays for the inverter module and a typo in
        # the solar model cannot stop the rural site starting.
        from inverter import InverterModel

        model = InverterModel(
            device_id=device_id,
            regmap=regmap,
            ac_kw=_envf("AC_RATING_KW", 4200.0),
            dc_kwp=_envf("DC_RATING_KWP", 5300.0),
            export_limit_kw=_envf("EXPORT_LIMIT_KW", 4200.0),
            nominal_voltage=_envf("NOMINAL_VOLTAGE", 398.0),
            skew_s=_envf("DEVICE_CLOCK_SKEW_S", 4.0),
            word_order=word_order,
            energy_base_kwh=_envf("ENERGY_BASE_KWH", 41_800_000.0),
        )
    elif kind == "windfarm":
        from wind import WindFarmModel

        model = WindFarmModel(
            device_id=device_id,
            regmap=regmap,
            turbine_count=_envi("TURBINE_COUNT", 6),
            rated_kw_each=_envf("RATED_KW_EACH", 2500.0),
            export_limit_kw=_envf("EXPORT_LIMIT_KW", 15_000.0),
            nominal_voltage=_envf("NOMINAL_VOLTAGE", 398.0),
            # This site has the best clock of the three: a short fibre backhaul means the
            # operators actually run NTP, so the skew is small and negative.
            skew_s=_envf("DEVICE_CLOCK_SKEW_S", -2.0),
            word_order=word_order,
            energy_base_kwh=_envf("ENERGY_BASE_KWH", 8_050_000.0),
        )
    else:
        model = MeterModel(
            device_id=device_id,
            regmap=regmap,
            fault=FaultState(),
            nominal_voltage=_envf("NOMINAL_VOLTAGE", 230.0),
            skew_s=_envf("DEVICE_CLOCK_SKEW_S", -7.0),
            word_order=word_order,
            energy_base_kwh=_envf("ENERGY_BASE_KWH", 42917.3),
        )

    httpd = ThreadingHTTPServer(
        (_env("HTTP_HOST", "0.0.0.0"), _envi("HTTP_PORT", 8080)), _ControlHandler
    )
    httpd.model = model  # type: ignore[attr-defined]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    LOG.info(
        "device %s (%s) online: modbus tcp %s:%s unit %s, control http %s:%s, %s registers "
        "at %s, word order=%s, device clock skew=%+.1fs",
        device_id, kind,
        _env("MODBUS_HOST", "0.0.0.0"), _envi("MODBUS_PORT", 5020), _envi("MODBUS_UNIT_ID", 1),
        _env("HTTP_HOST", "0.0.0.0"), _envi("HTTP_PORT", 8080),
        regmap.total_regs, sunspec.BASE,
        ["high-word-first", "low-word-first"][word_order], model.skew_s,
    )
    asyncio.run(_serve_modbus(model, _env("MODBUS_HOST", "0.0.0.0"),
                              _envi("MODBUS_PORT", 5020), _envi("MODBUS_UNIT_ID", 1)))


if __name__ == "__main__":
    main()
