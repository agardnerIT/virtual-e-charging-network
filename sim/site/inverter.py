"""Utility-scale solar inverter simulator - the device end of the M1b solar site.

    [this]  <--poll--  [poller]  --OTLP-->  [edge collector]  --> WAN -->  [hub]

Same image, same poller, same register-file contract as the M1a rural meter (D10): the
device owns a live SunSpec register image, exposes no timestamp and no quality, and does not
know that OTLP exists. Only the electrical model and the model chain differ - sites differ
by environment, not by code.

What makes this believable to an electrical engineer
----------------------------------------------------
The numbers are not a smooth sine with noise bolted on. They come from the four things a PV
engineer would check first:

* **Irradiance and cell temperature.** POA irradiance drives DC power; cell temperature
  derates it. A hot clear day produces *less* than a cool clear day, which is the first
  thing anyone who has commissioned a farm looks for.
* **DC/AC ratio ~1.26.** The array is deliberately oversized against the inverter so it
  **clips** around solar noon on a clear day. The flat top of the AC curve is the visible
  signature of that design choice, not a bug.
* **Two MPPTs that disagree.** A cloud edge crosses one tracker before the other, so the two
  MPPT blocks diverge for tens of seconds. Per-block DC is reported separately, which is
  what model 160 (MPPT) is for.
* **An export limit and curtailment.** The connection point has a cap. When DC would push
  AC past the inverter's own rating the inverter clips; when the network operator lowers the
  cap the farm is curtailed. Those are different facts, so they are different states.

Energy is accumulated from the modelled power, so WH and DCWH are consistent with what the
AC and DC curves actually did rather than being a monotonic counter with a plausible slope.
"""

from __future__ import annotations

import math
import os
import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field

import sunspec
from clock import ClockState

LOG = None  # set by the entry point; this module stays importable without logging configured


def _envf(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


# SunSpec model 103 St (operating state) values we use. The full enum is larger; these are
# the states a PV inverter is actually in most of the time.
ST_OFF = 0
ST_SLEEPING = 3
ST_MPPT = 4
ST_FAULT = 7

# SunSpec model 103 StVnd is ours to define; 0 is "nothing to say".
STVND_NONE = 0

#: Which of the two facts is holding the output down. Different values on purpose: an
#: engineer reading the mimic must be able to tell "the inverter is full" from "the network
#: told it to stop", because the remedies are different.
CURTAIL_NONE = 0
CURTAIL_EXPORT_LIMITED = 1
CURTAIL_INVERTER_CLIPPED = 2


@dataclass
class CurtailState:
    """Thread-safe holder for the operator export limit."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    pct: float = 100.0
    until: float = 0.0

    def set_limit(self, pct: float, duration_s: float | None) -> dict:
        with self.lock:
            self.pct = max(0.0, min(100.0, pct))
            self.until = 0.0 if not duration_s else time.monotonic() + max(1.0, duration_s)
            return self.snapshot_locked()

    def clear(self) -> None:
        with self.lock:
            self.pct = 100.0
            self.until = 0.0

    def snapshot_locked(self) -> dict:
        return {
            "export_limit_pct": round(self.pct, 2),
            "remaining_s": max(0.0, round(self.until - time.monotonic(), 1)) if self.until else None,
        }

    def snapshot(self) -> dict:
        with self.lock:
            if self.until and time.monotonic() >= self.until:
                self.pct, self.until = 100.0, 0.0
            return self.snapshot_locked()

    def active_pct(self) -> float:
        with self.lock:
            if self.until and time.monotonic() >= self.until:
                self.pct, self.until = 100.0, 0.0
            return self.pct


class InverterModel:
    """A three-phase central PV inverter with two MPPTs and a weather station.

    All powers are kW internally; the register map converts to W and then to raw counts.
    """

    def __init__(
        self,
        device_id: str,
        regmap: sunspec.RegisterMap,
        ac_kw: float,
        dc_kwp: float,
        export_limit_kw: float,
        nominal_voltage: float,
        skew_s: float,
        word_order: int,
        energy_base_kwh: float,
        seed: int = 20261015,
    ) -> None:
        self.device_id = device_id
        self.regmap = regmap
        self.ac_kw = ac_kw
        self.dc_kwp = dc_kwp
        self.export_limit_kw = export_limit_kw
        self.nominal_voltage = nominal_voltage
        self.skew_s = skew_s
        self.clock = ClockState(initial_skew_s=skew_s)
        self.word_order = word_order
        self.curtail = CurtailState()
        self._t0 = time.monotonic()
        self._last_t = self._t0
        self.energy_ac_kwh = energy_base_kwh
        self.energy_dc_kwh = energy_base_kwh * 1.06  # DC is always a little ahead of AC
        self._rng = random.Random(seed)
        self._cloud = 0.92
        # The second tracker sees the same sky ~20 scans later. One buffer gives the second
        # MPPT a genuine, physically-motivated reason to disagree with the first.
        self._cloud_history: deque[float] = deque([0.92] * 25, maxlen=25)
        self._hour_override = os.environ.get("SIM_CLOCK_HOUR")
        self._mppt_count = max(1, sum(1 for p in regmap.points.values() if p.group == "mppt.id"))

    # -- helpers -----------------------------------------------------------------------
    def _noise(self, scale: float) -> float:
        return self._rng.gauss(0.0, scale)

    def _sim_hour(self) -> float:
        """Local hour of day, 0..24.

        Wall clock by default - the irradiance curve is a real one, not a compressed
        animation. ``SIM_CLOCK_HOUR`` pins the starting hour for a demo that has to run at
        night; the clock then advances at real time from there.
        """
        if self._hour_override is not None and self._hour_override.strip() != "":
            try:
                base = float(self._hour_override)
            except ValueError:
                return time.localtime().tm_hour + time.localtime().tm_min / 60.0
            return (base + (time.monotonic() - self._t0) / 3600.0) % 24.0
        lt = time.localtime()
        return lt.tm_hour + lt.tm_min / 60.0 + lt.tm_sec / 3600.0

    def _sun_elevation(self, hour: float) -> float:
        """A smooth 6am-6pm solar day. 0 below the horizon, 1 at solar noon."""
        if hour < 6.0 or hour > 18.0:
            return 0.0
        return math.sin(math.pi * (hour - 6.0) / 12.0)

    # -- the physical model ------------------------------------------------------------
    def sample(self) -> dict[str, float]:
        now = time.monotonic()
        dt = max(0.0, now - self._last_t)
        self._last_t = now
        t = now - self._t0

        # Clouds: a bounded random walk, with the occasional edge. Bounded so the plant does
        # not spend the afternoon at 5% and look broken; edges so the DC curve is not smooth.
        self._cloud = min(1.0, max(0.28, self._cloud + self._noise(0.015)))
        if self._rng.random() < 0.006:
            self._cloud = max(0.30, self._cloud - self._rng.uniform(0.15, 0.35))
        self._cloud_history.append(self._cloud)
        cloud_tracker1 = self._cloud
        cloud_tracker2 = self._cloud_history[0] if len(self._cloud_history) == self._cloud_history.maxlen else self._cloud

        hour = self._sim_hour()
        elev = self._sun_elevation(hour)

        # Plane-of-array irradiance. 1050 W/m2 clear-sky peak is normal for a tilted array
        # (it can exceed the 1000 W/m2 STC reference at high altitude / low temperature).
        g_clear = 1050.0 * elev
        if g_clear > 1.0:
            poa1 = max(0.0, g_clear * cloud_tracker1 + self._noise(4.0))
            poa2 = max(0.0, g_clear * cloud_tracker2 + self._noise(4.0))
        else:
            # No sun, no irradiance. Without this the gaussian sensor noise on the POA
            # reading leaks through the DC model and the farm quietly generates a few kW at
            # 2am, which is exactly the sort of plausible-looking nonsense this repo exists
            # not to produce.
            poa1 = poa2 = 0.0
        poa = (poa1 + poa2) / 2.0
        # Global horizontal is always below POA for a tilted array; keep the relationship
        # physical rather than making GH a second independent random number.
        gh = poa * (0.80 + 0.03 * math.sin(t / 600.0))
        # Diffuse and direct components (#23). The split follows the cloud cover rather than the
        # clock: an overcast sky is nearly all diffuse, a clear one nearly all direct. That is
        # the physically meaningful part - direct irradiance is what a tracking surface can
        # chase and diffuse is what a flat one cannot escape.
        diffuse_frac = min(1.0, max(0.10, 0.10 + 0.85 * (1.0 - self._cloud)))
        dfi = max(0.0, gh * diffuse_frac)
        dni = max(0.0, (gh - dfi) / 0.85)   # DNI = (GH - DFI) / cos(zenith), ~0.85 at this latitude

        t_amb = 16.0 + 12.0 * elev + self._noise(0.25)
        # NOCT-style cell rise: +25 degC over ambient at 800 W/m2.
        t_cell = t_amb + poa / 800.0 * 25.0 + self._noise(0.2)

        gamma = -0.0034  # /degC, module temperature coefficient of Pmax
        derate = max(0.0, 1.0 + gamma * (t_cell - 25.0))
        kwp_per_mppt = self.dc_kwp / self._mppt_count

        # Per-tracker DC. A small persistent bias keeps them from being mirror images even
        # when the sky is uniform (soiling, string mismatch, tracker alignment).
        bias = 0.012 * math.sin(t / 220.0)
        p_dc1 = kwp_per_mppt * (poa1 / 1000.0) * derate * (1.0 + bias + self._noise(0.002))
        p_dc2 = kwp_per_mppt * (poa2 / 1000.0) * derate * (1.0 - bias + self._noise(0.002))
        p_dc1, p_dc2 = max(0.0, p_dc1), max(0.0, p_dc2)
        p_dc = p_dc1 + p_dc2

        load = p_dc / self.ac_kw if self.ac_kw else 0.0
        eta = 0.0 if load <= 0.002 else 0.970 + 0.015 * min(1.0, load / 0.4)
        p_ac_raw = p_dc * eta

        limit_pct = self.curtail.active_pct()
        cap = min(self.ac_kw, self.ac_kw * limit_pct / 100.0)
        p_ac = min(p_ac_raw, cap)

        if limit_pct < 100.0 and p_ac_raw > cap + 0.5:
            curtail_state = CURTAIL_EXPORT_LIMITED   # the network told it to stop
        elif p_ac_raw > self.ac_kw + 0.5:
            curtail_state = CURTAIL_INVERTER_CLIPPED  # the inverter is simply full
        else:
            curtail_state = CURTAIL_NONE

        # Accumulate energy from the power that was actually delivered. kWh = kW * h.
        self.energy_ac_kwh += p_ac * dt / 3600.0
        self.energy_dc_kwh += p_dc * dt / 3600.0

        # AC electricals. Balanced on average, slightly untidy per phase like any real farm.
        pf = min(0.999, max(0.985, 0.996 + self._noise(0.001)))
        v_ph = self.nominal_voltage + 1.2 * math.sin(t / 31.0) + self._noise(0.15)
        v_ll = v_ph * math.sqrt(3.0)
        i_ph = (p_ac * 1000.0) / (3.0 * v_ph * pf) if v_ph else 0.0
        i_a = max(0.0, i_ph * (1.0 + 0.012) + self._noise(0.3))
        i_b = max(0.0, i_ph * (1.0 - 0.004) + self._noise(0.3))
        i_c = max(0.0, i_ph * (1.0 - 0.008) + self._noise(0.3))
        p_ac_w = p_ac * 1000.0
        va = p_ac_w / pf if pf else 0.0
        var = math.sqrt(max(va * va - p_ac_w * p_ac_w, 0.0))

        # DC bus, aggregate and per MPPT. Voltage rises as the array unloads toward Voc.
        dcv1 = 868.0 + 55.0 * max(0.0, 1.0 - poa1 / 1000.0) + self._noise(0.4)
        dcv2 = 868.0 + 55.0 * max(0.0, 1.0 - poa2 / 1000.0) + self._noise(0.4)
        dca1 = p_dc1 * 1000.0 / dcv1 if dcv1 else 0.0
        dca2 = p_dc2 * 1000.0 / dcv2 if dcv2 else 0.0

        # Cabinet/heatsink temperature follows ambient plus load. A hot inverter on a hot day
        # is a real derate and derate-trip story; the values have to move with load to tell it.
        tmp_cab = t_amb + 4.0 + 22.0 * load + self._noise(0.15)
        tmp_snk = t_amb + 3.0 + 26.0 * load + self._noise(0.15)

        st = ST_MPPT if p_dc > 0.01 * self.dc_kwp else ST_SLEEPING
        hz = min(50.10, max(49.90, 50.0 + 0.02 * math.sin(t / 43.0) + self._noise(0.003)))

        return {
            # -- model 103, AC side
            "A": (i_a + i_b + i_c) / 3.0,
            "AphA": i_a, "AphB": i_b, "AphC": i_c,
            "PPVphAB": v_ll + self._noise(0.2),
            "PPVphBC": v_ll - 0.6 + self._noise(0.2),
            "PPVphCA": v_ll + 0.4 + self._noise(0.2),
            "PhVphA": v_ph, "PhVphB": v_ph - 0.3, "PhVphC": v_ph + 0.2,
            "W": p_ac_w, "Hz": hz, "VA": va, "VAr": var, "PF": pf,
            "WH": self.energy_ac_kwh,
            # -- model 103, DC aggregate
            "DCA": dca1 + dca2,
            "DCV": (dcv1 + dcv2) / 2.0,
            "DCW": p_dc * 1000.0,
            "DCWH": self.energy_dc_kwh,
            "TmpCab": tmp_cab, "TmpSnk": tmp_snk,
            # The step-up transformer lags the inverter: bigger mass, slower to heat, slower to
            # cool. It is the thing that derates a farm on a hot still afternoon.
            "TmpTrns": t_amb + 6.0 + 30.0 * load + self._noise(0.2),
            "St": st, "StVnd": STVND_NONE, "Evt1": 0, "EvtVnd": 0,
            # -- model 160, one entry per MPPT block
            "MPPT_ID_1": 1, "MPPT_ID_2": 2,
            "MPPT_DCA_1": dca1, "MPPT_DCV_1": dcv1, "MPPT_DCW_1": p_dc1 * 1000.0, "MPPT_Tmp_1": t_cell - 4.0,
            "MPPT_DCA_2": dca2, "MPPT_DCV_2": dcv2, "MPPT_DCW_2": p_dc2 * 1000.0, "MPPT_Tmp_2": t_cell - 3.0,
            # -- models 302 / 303, the weather pair
            "GH": gh, "POA": poa, "DFI": dfi, "DNI": dni,
            "TmpBOM": t_cell, "TmpAmb": t_amb,
            # -- model 123 + model 129 (ride-through values a connection agreement would set)
            "WMaxLimPct": limit_pct,
            "VMinPct": 90.0,      # stay connected down to 90 % of nominal
            "VMinTms": 1000,      # ... for at least a second
            "VrtSt": 1,           # 1 = riding through, 0 = tripped. Never trips in this model.
            "FaultCode": 0,
            "CurtailState": curtail_state,
        }

    # -- device surface used by the Modbus server and the control endpoint --------------
    def uptime_s(self) -> float:
        return round(time.monotonic() - self._t0, 1)

    def device_time(self) -> int:
        return self.clock.now()

    def image(self) -> list[int]:
        return self.regmap.encode(
            self.sample(),
            device_time=self.device_time(),
            word_order=self.word_order,
            clock_offset_s=int(self.clock.snapshot()["clock_skew_s"]),
        )

    def health(self) -> dict:
        return {
            "status": "ok",
            "device_id": self.device_id,
            "device_kind": "inverter",
            "uptime_s": self.uptime_s(),
            "device_clock_skew_s": self.clock.snapshot()["clock_skew_s"],
            "register_count": self.regmap.total_regs,
            "ac_rating_kw": self.ac_kw,
            "dc_rating_kwp": self.dc_kwp,
            "dc_ac_ratio": round(self.dc_kwp / self.ac_kw, 3),
            "export_limit": self.curtail.snapshot(),
        }
