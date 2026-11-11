"""Wind farm simulator - the device end of the M1b third site.

    [this]  <--poll--  [poller]  --OTLP-->  [edge collector]  --> WAN -->  [hub]

Same image, same poller, same register-file contract as the rural meter and the solar
inverter (D10). Only the electrical model and the model chain differ.

Why the wind farm is not "solar with a different curve"
-------------------------------------------------------
The plan asks for two contrasting generation regimes (D17), and the contrast is measurable
rather than decorative:

* **High per-turbine variance, farm-level smoothing.** Six machines see different wind at the
  same instant. Each turbine's power swings hard; the farm total does not, because the
  swings are largely independent. That is why the mimic shows six turbines and a farm
  aggregate, and why a single "farm wind speed" number would be a lie of convenience.
* **A cube law and hard limits.** Power goes as the cube of wind speed between cut-in and
  rated, then is flat-rated, then drops to zero at cut-out. The curve has a knee, not a
  smooth arc, and a gust can take one machine to rated while its neighbour is still idling.
* **Wind does not care what time it is.** There is a weak diurnal component, but the farm
  makes energy at 2am - which is why `make up` at night still shows a live generation site
  even though the solar farm is asleep.
"""

from __future__ import annotations

import math
import os
import random
import time
from collections import deque
from dataclasses import dataclass

import sunspec
from clock import ClockState
from inverter import CurtailState  # the same export-limit control, shared

# Turbine operating states. A vendor enum: SunSpec has no shipped turbine model, so this is
# documented in docs/standards.md rather than implied to be standard.
WTG_IDLE = 0        # below cut-in, or above cut-out
WTG_STARTING = 1
WTG_PRODUCING = 2
WTG_CURTAILED = 3
WTG_FAULT = 7

#: Aerodynamic limits of the modelled machine (a 2.5 MW onshore class turbine).
CUT_IN_MS = 3.0
RATED_MS = 12.0
CUT_OUT_MS = 25.0


def _envf(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


class WindFarmModel:
    def __init__(
        self,
        device_id: str,
        regmap: sunspec.RegisterMap,
        turbine_count: int,
        rated_kw_each: float,
        export_limit_kw: float,
        nominal_voltage: float,
        skew_s: float,
        word_order: int,
        energy_base_kwh: float,
        seed: int = 20261016,
    ) -> None:
        self.device_id = device_id
        self.regmap = regmap
        self.turbine_count = turbine_count
        self.rated_kw_each = rated_kw_each
        self.export_limit_kw = export_limit_kw
        self.nominal_voltage = nominal_voltage
        self.skew_s = skew_s
        self.clock = ClockState(initial_skew_s=skew_s)
        self.word_order = word_order
        self.curtail = CurtailState()
        self._t0 = time.monotonic()
        self._last_t = self._t0
        self.energy_kwh = energy_base_kwh
        self._rng = random.Random(seed)
        self._wind = 9.0
        self._dir = 214.0
        self._nacelle = [214.0] * turbine_count
        # Independent per-turbine phase and period: the whole point is that they disagree.
        self._phase = [self._rng.uniform(0, 2 * math.pi) for _ in range(turbine_count)]
        self._period = [self._rng.uniform(22.0, 70.0) for _ in range(turbine_count)]
        self._bias = [self._rng.uniform(-0.06, 0.06) for _ in range(turbine_count)]
        # A shared gust field passed down the row: real gusts are correlated, and modelling
        # them as pure independent noise would make the farm average too smooth.
        self._gust: deque[float] = deque([0.0] * 12, maxlen=12)

    def _noise(self, scale: float) -> float:
        return self._rng.gauss(0.0, scale)

    def _sun_ish(self) -> float:
        """A weak diurnal factor. Wind is not solar: this only shifts the mean a little."""
        lt = time.localtime()
        hour = lt.tm_hour + lt.tm_min / 60.0
        override = os.environ.get("SIM_CLOCK_HOUR")
        if override is not None and override.strip() != "":
            try:
                hour = (float(override) + (time.monotonic() - self._t0) / 3600.0) % 24.0
            except ValueError:
                pass
        # Slightly windier overnight and in the afternoon than at dawn.
        return 0.8 * math.sin(2 * math.pi * (hour - 4.0) / 12.0)

    def _power_kw(self, v: float) -> float:
        """The power curve: zero, cubic ramp, flat-rated, zero. A knee, not an arc."""
        if v < CUT_IN_MS or v >= CUT_OUT_MS:
            return 0.0
        if v >= RATED_MS:
            return self.rated_kw_each
        return self.rated_kw_each * (v ** 3 - CUT_IN_MS ** 3) / (RATED_MS ** 3 - CUT_IN_MS ** 3)

    def sample(self) -> dict[str, float]:
        now = time.monotonic()
        dt = max(0.0, now - self._last_t)
        self._last_t = now
        t = now - self._t0

        # Free-stream wind: mean-reverting, not a free random walk. A free walk eventually
        # parks itself on the clamp and the whole farm idles at 0 kW, which is a bug that
        # only shows up after a few minutes of running. Pull toward a site mean instead.
        target = 10.5 + 1.5 * self._sun_ish()
        self._wind += 0.06 * (target - self._wind) + self._noise(0.35)
        if self._rng.random() < 0.01:
            self._wind += self._rng.uniform(0.5, 2.5)  # a front passes
        self._wind = min(23.0, max(3.5, self._wind))
        self._dir = (self._dir + self._noise(0.35)) % 360.0
        self._gust.append(self._wind + self._noise(1.1))

        ambient = 14.0 + 7.0 * self._sun_ish() + self._noise(0.3)

        turbines = []
        total_kw = 0.0
        for i in range(self.turbine_count):
            # The gust field arrives at turbine i slightly later than at its neighbour.
            gust = self._gust[-1 - (i % len(self._gust))]
            v = max(0.0, self._wind + 1.0 * math.sin(t / self._period[i] + self._phase[i])
                    + 0.35 * (gust - self._wind) + self._bias[i] * self._wind + self._noise(0.35))
            p_kw = self._power_kw(v)
            # Nacelle yaw lags the wind; a machine is never perfectly aligned.
            self._nacelle[i] = (self._nacelle[i] + 0.08 * ((self._dir - self._nacelle[i] + 540.0) % 360.0 - 180.0)) % 360.0
            load = p_kw / self.rated_kw_each if self.rated_kw_each else 0.0
            rpm = 0.0 if v < CUT_IN_MS else min(16.2, 1.35 * v)
            pitch = 0.0 if v <= RATED_MS else min(22.0, (v - RATED_MS) * 1.8) + self._noise(0.05)
            turbines.append({
                "WindSpeed": v, "WindDir": self._nacelle[i], "RotorRpm": rpm, "PitchAngle": pitch,
                "W": p_kw, "WH": self.energy_kwh, "load": load,
                "TmpNac": ambient + 3.0 + 9.0 * load, "TmpGear": ambient + 6.0 + 42.0 * load,
                "st": WTG_PRODUCING if p_kw > 0.02 * self.rated_kw_each else WTG_IDLE,
            })
            total_kw += p_kw

        # Farm curtailment: the operator's export limit applies to the point of connection and
        # is spread across the machines, exactly as a farm controller would.
        limit_pct = self.curtail.active_pct()
        cap_kw = self.rated_kw_each * self.turbine_count * limit_pct / 100.0
        export_limit_kw = min(self.export_limit_kw, cap_kw)
        scale = 1.0
        if total_kw > export_limit_kw > 0:
            scale = export_limit_kw / total_kw
        if limit_pct < 100.0 and total_kw > cap_kw + 0.5:
            curtail_state = 1
        elif total_kw > self.export_limit_kw + 0.5:
            curtail_state = 2  # held down by the connection cap, not by the operator
        else:
            curtail_state = 0

        self.energy_kwh += (total_kw * scale) * dt / 3600.0

        out: dict[str, float] = {}
        for i, turb in enumerate(turbines, start=1):
            delivered = turb["W"] * scale
            st = WTG_CURTAILED if (scale < 0.999 and turb["W"] > 0) else turb["st"]
            p_w = delivered * 1000.0
            pf = min(0.999, max(0.985, 0.993 + self._noise(0.001)))
            v_ph = self.nominal_voltage + self._noise(0.2)
            a = p_w / (3.0 * v_ph * pf) if v_ph else 0.0
            var = p_w * math.tan(math.acos(pf))
            out[f"WTG_ID_{i}"] = i
            out[f"WindSpeed_{i}"] = turb["WindSpeed"]
            out[f"WindDir_{i}"] = turb["WindDir"]
            out[f"RotorRpm_{i}"] = turb["RotorRpm"]
            out[f"PitchAngle_{i}"] = turb["PitchAngle"]
            out[f"W_{i}"] = p_w
            out[f"VAr_{i}"] = var
            out[f"WH_{i}"] = self.energy_kwh
            out[f"A_{i}"] = a
            out[f"V_{i}"] = v_ph
            out[f"TmpNac_{i}"] = turb["TmpNac"]
            out[f"TmpGear_{i}"] = turb["TmpGear"]
            out[f"St_{i}"] = st
            out[f"Avail_{i}"] = 0 if st == WTG_FAULT else 1

        farm_w = sum(t["W"] * scale for t in turbines)
        farm_var = farm_w * math.tan(math.acos(0.993))
        speeds = [t["WindSpeed"] for t in turbines]
        out.update({
            "FarmW": farm_w * 1000.0 * 0.99,   # collector + transformer losses
            "FarmVAr": farm_var * 1000.0,
            "FarmWH": self.energy_kwh * 0.99,
            "Hz": min(50.12, max(49.88, 50.0 + 0.03 * math.sin(t / 41.0) + self._noise(0.004))),
            "TmpAmb": ambient,
            "WindSpeedAvg": sum(speeds) / len(speeds),
            "WindDirAvg": self._dir,
            "FarmSt": WTG_PRODUCING if farm_w > 0.02 * self.rated_kw_each else WTG_IDLE,
            "FarmAvail": sum(1 for t in turbines if t["st"] != WTG_FAULT),
            "WMaxLimPct": limit_pct,
            "FaultCode": 0,
            "CurtailState": curtail_state,
        })
        return out

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
            "device_kind": "windfarm",
            "uptime_s": self.uptime_s(),
            "device_clock_skew_s": self.clock.snapshot()["clock_skew_s"],
            "register_count": self.regmap.total_regs,
            "turbines": self.turbine_count,
            "rated_kw_each": self.rated_kw_each,
            "farm_rating_kw": self.rated_kw_each * self.turbine_count,
            "export_limit": self.curtail.snapshot(),
        }
