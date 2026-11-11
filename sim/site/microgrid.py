"""Isolated microgrid simulator - the device end of M3-2.

    [this]  <--poll--  [poller]  --OTLP-->  [edge collector]  --> WAN -->  [hub]

An islanded site: solar, wind and a battery, and **no feeder at all**. Frequency is set by
the battery inverter rather than by the NEM, which is the whole point of the site (D17): the
same voltage sag means something different here because there is no upstream network to
explain it.

What is modelled physics, and what is invented
----------------------------------------------
Modelled, in the sense that the equations are the ordinary ones an electrical engineer
would write down:

* **Swing equation.** Frequency moves because generation and load disagree:
  ``dΔf/dt = (ΔP / S_base) · f_nom / (2H)``. H is 2 s here, which is low on purpose - an
  inverter-dominated microgrid has almost no rotating inertia, so a load step moves
  frequency a hundred times more than it would on the NEM. That contrast is the site.
* **Droop control.** The grid-forming BESS does not follow a power schedule; it responds to
  frequency: ``P = (f_nom − f) / (f_nom · R) · S_base`` with R = 4 %. That is why a load step
  settles at a *droop offset* (~0.2 Hz at 10 % load) instead of returning exactly to 50.00.
* **State of charge** integrates the actual charge/discharge power with a one-way
  efficiency, and the battery's power limits are hard: at SoC floor it stops discharging
  and frequency falls until the controller sheds load (49.4 Hz).
* **Resource models.** PV is irradiance × temperature derate (the same shape as the solar
  farm), wind is a cube law between cut-in and rated (the same shape as the wind farm).
* **Over-frequency curtailment.** If the battery cannot absorb a surplus and frequency rises
  past 50.35 Hz, PV is curtailed - the controller's response, not a network instruction.

Invented outright, and stated here rather than implied:

* the protection thresholds (49.4 Hz load shed, 50.35 Hz curtailment) are chosen to be
  visible on a five-second metric cadence, not taken from any standard;
* the load cycle (a block of EV charging every four minutes) is a demo schedule, not a
  load forecast;
* there is no protection relay model, no fault current, no phase-angle model, no unbalanced
  or harmonic behaviour. A real microgrid's hardest problem - fault level with
  inverter-based sources - is not represented.

Like every other device here it owns a live register image, carries no timestamp and no
quality flag, and does not know OTLP exists.
"""

from __future__ import annotations

import math
import os
import random
import threading
import time
from dataclasses import dataclass, field

import sunspec
from clock import ClockState

# BESS status enum. Vendor, documented in docs/standards.md alongside the turbine one.
BESS_IDLE = 0
BESS_CHARGING = 1
BESS_DISCHARGING = 2
BESS_HOLD = 3

#: Why the grid former is not doing what the busbar is asking. Distinct values because the
#: remedy differs: a power limit is transient, a SoC floor is a fuel problem.
HOLD_NONE = 0
HOLD_SOC_HIGH = 1
HOLD_SOC_LOW = 2
HOLD_POWER_LIMIT = 3

#: Source block ids. The repeating model carries these; the edge collector turns them into
#: names, because names are semantics and semantics belong at the edge (D20).
SRC_SOLAR = 1
SRC_WIND = 2

#: Cut-in / rated wind speed for the modelled machine, m/s.
CUT_IN_MS = 3.0
RATED_MS = 11.5


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
class LoadStepState:
    """Thread-safe holder for the operator/demo load step.

    This is not a fault (D29 keeps exactly one of those). It is an operational event: a block
    of EV chargers closes its contactors. It exists so that "what does a load step do to an
    islanded grid" is reproducible on demand, and it is the same code path the automatic
    four-minute cycle uses.
    """

    lock: threading.Lock = field(default_factory=threading.Lock)
    kw: float = 0.0
    until: float = 0.0

    def set_step(self, kw: float, duration_s: float | None) -> dict:
        with self.lock:
            self.kw = max(0.0, kw)
            self.until = 0.0 if not duration_s else time.monotonic() + max(1.0, duration_s)
            return self.snapshot_locked()

    def clear(self) -> None:
        with self.lock:
            self.kw = 0.0
            self.until = 0.0

    def snapshot_locked(self) -> dict:
        return {
            "step_kw": round(self.kw, 1),
            "remaining_s": max(0.0, round(self.until - time.monotonic(), 1)) if self.until else None,
        }

    def snapshot(self) -> dict:
        with self.lock:
            if self.until and time.monotonic() >= self.until:
                self.kw, self.until = 0.0, 0.0
            return self.snapshot_locked()

    def active_kw(self) -> float:
        with self.lock:
            if self.until and time.monotonic() >= self.until:
                self.kw, self.until = 0.0, 0.0
            return self.kw


class MicrogridModel:
    """Solar + wind + BESS on one busbar, the BESS being the grid former."""

    def __init__(
        self,
        device_id: str,
        regmap: sunspec.RegisterMap,
        pv_kwp: float = 350.0,
        wind_rated_kw: float = 300.0,
        bess_kwh: float = 500.0,
        bess_max_charge_kw: float = 300.0,
        bess_max_discharge_kw: float = 300.0,
        bess_soc_pct: float = 62.0,
        load_base_kw: float = 150.0,
        ev_step_kw: float = 300.0,
        ev_cycle_s: float = 240.0,
        ev_on_s: float = 90.0,
        nominal_voltage: float = 230.0,
        nominal_hz: float = 50.0,
        system_base_kw: float = 1500.0,
        inertia_h: float = 2.0,
        droop_r: float = 0.02,
        soc_min_pct: float = 12.0,
        soc_max_pct: float = 98.0,
        shed_hz: float = 49.4,
        curtail_hz: float = 50.35,
        skew_s: float = -11.0,
        word_order: int = sunspec.WORD_ORDER_LOW_FIRST,
        energy_base_kwh: float = 0.0,
        seed: int = 20261112,
    ) -> None:
        self.device_id = device_id
        self.regmap = regmap
        self.pv_kwp = pv_kwp
        self.wind_rated_kw = wind_rated_kw
        self.bess_kwh = bess_kwh
        self.bess_max_charge_kw = bess_max_charge_kw
        self.bess_max_discharge_kw = bess_max_discharge_kw
        self.load_base_kw = load_base_kw
        self.ev_step_kw = ev_step_kw
        self.ev_cycle_s = ev_cycle_s
        self.ev_on_s = ev_on_s
        self.nominal_voltage = nominal_voltage
        self.nominal_hz = nominal_hz
        self.system_base_kw = system_base_kw
        self.inertia_h = max(0.2, inertia_h)
        self.droop_r = max(0.005, droop_r)
        self.soc_min_pct = soc_min_pct
        self.soc_max_pct = soc_max_pct
        self.shed_hz = shed_hz
        self.curtail_hz = curtail_hz
        self.skew_s = skew_s
        self.clock = ClockState(initial_skew_s=skew_s)
        self.word_order = word_order
        self.loadstep = LoadStepState()
        self._t0 = time.monotonic()
        self._last_t = self._t0
        self._rng = random.Random(seed)
        self._hour_override = os.environ.get("SIM_CLOCK_HOUR")

        # State that integrates over time. Nothing here is reset per sample.
        self.soc_pct = min(99.0, max(1.0, bess_soc_pct))
        self.frequency_hz = nominal_hz
        self.p_bess_kw = 0.0
        self.throughput_kwh = energy_base_kwh
        self._wind_ms = 9.0
        self._cloud = 0.85
        self._renew_curtail_pct = 0.0
        self._ev_shed_pct = 0.0
        self._hold = HOLD_NONE
        self._st = BESS_IDLE
        self._rocof = 0.0
        self._last_f = nominal_hz

    # -- helpers -----------------------------------------------------------------------
    def _noise(self, scale: float) -> float:
        return self._rng.gauss(0.0, scale)

    def _sim_hour(self) -> float:
        """Local hour of day; SIM_CLOCK_HOUR pins the start for a night-time demo."""
        if self._hour_override is not None and self._hour_override.strip() != "":
            try:
                base = float(self._hour_override)
            except ValueError:
                lt = time.localtime()
                return lt.tm_hour + lt.tm_min / 60.0
            return (base + (time.monotonic() - self._t0) / 3600.0) % 24.0
        lt = time.localtime()
        return lt.tm_hour + lt.tm_min / 60.0 + lt.tm_sec / 3600.0

    @staticmethod
    def _sun_elevation(hour: float) -> float:
        if hour < 6.0 or hour > 18.0:
            return 0.0
        return math.sin(math.pi * (hour - 6.0) / 12.0)

    def _ev_load_kw(self, t: float) -> float:
        """The EV block: operator step if one is active, otherwise the automatic cycle.

        The automatic cycle exists so that `make prom Q='energy_microgrid_frequency_hertz'`
        shows something without anyone typing a command: a microgrid whose frequency moves
        is the claim, and a claim that needs a human to schedule it is a weaker one.
        """
        step = self.loadstep.active_kw()
        if step > 0.0:
            return step
        if self.ev_cycle_s <= 0:
            return 0.0
        phase = (t + self.ev_cycle_s * 0.37) % self.ev_cycle_s
        if phase < self.ev_on_s:
            # A short ramp as cars start, so the step has a shape rather than a cliff.
            ramp = min(1.0, phase / 6.0, max(0.0, (self.ev_on_s - phase) / 6.0))
            return self.ev_step_kw * (0.6 + 0.4 * ramp)
        return self.ev_step_kw * 0.08  # a few cars always plugged in

    # -- the physical model ------------------------------------------------------------
    def sample(self) -> dict[str, float]:
        now = time.monotonic()
        dt = max(0.0, min(2.0, now - self._last_t))
        self._last_t = now
        t = now - self._t0
        hour = self._sim_hour()
        elev = self._sun_elevation(hour)

        # --- the resource -------------------------------------------------------------
        self._cloud = min(1.0, max(0.25, self._cloud + self._noise(0.02)))
        if self._rng.random() < 0.004:
            self._cloud = max(0.28, self._cloud - self._rng.uniform(0.1, 0.3))
        poa = max(0.0, 1050.0 * elev * self._cloud + self._noise(3.0)) if elev > 0.0 else 0.0
        t_amb = 15.0 + 12.0 * elev + self._noise(0.25)
        t_cell = t_amb + poa / 800.0 * 25.0
        derate = max(0.0, 1.0 + (-0.0034) * (t_cell - 25.0))
        pv_available_kw = max(0.0, self.pv_kwp * (poa / 1000.0) * derate)

        target = 10.0 + 1.5 * math.sin(2 * math.pi * (hour - 4.0) / 12.0)
        self._wind_ms += 0.06 * (target - self._wind_ms) + self._noise(0.4)
        if self._rng.random() < 0.01:
            self._wind_ms += self._rng.uniform(0.4, 2.0)
        self._wind_ms = min(23.0, max(2.0, self._wind_ms))
        v = self._wind_ms
        if v < CUT_IN_MS or v >= 25.0:
            wind_available_kw = 0.0
        elif v >= RATED_MS:
            wind_available_kw = self.wind_rated_kw
        else:
            wind_available_kw = self.wind_rated_kw * (v ** 3 - CUT_IN_MS ** 3) / (RATED_MS ** 3 - CUT_IN_MS ** 3)

        # Curtailment and load shedding are the controller's last resort and persist until
        # frequency comes back inside the band - a protection action, not a per-sample value.
        # Over-frequency curtails *every* source, not just PV: at 3am the wind is the only
        # thing making power, and a controller that can only spill solar would let an islanded
        # grid climb until something tripped.
        spill = 1.0 - self._renew_curtail_pct / 100.0
        pv_kw = pv_available_kw * spill
        wind_kw = wind_available_kw * spill
        ev_kw = self._ev_load_kw(t) * (1.0 - self._ev_shed_pct / 100.0)
        load_kw = self.load_base_kw + ev_kw

        # --- the control loop, integrated in substeps ----------------------------------
        # The swing equation is stiff: with H = 2 s and a 1.5 MW base, a 100 kW imbalance
        # moves frequency at ~0.8 Hz/s. Integrating that once per second - once per Modbus
        # scan - is numerically unstable and produces a simulation that oscillates between
        # 46 and 55 Hz, which is a property of the solver, not of a microgrid. So the device
        # runs its own control loop at 20 Hz internally, like a real inverter would, and the
        # poller samples it at whatever rate it likes. This is the one place in this repo
        # where the model's own time step matters, and it is stated rather than hidden.
        substeps = max(1, int(math.ceil(dt / 0.05)))
        h = dt / substeps if substeps else 0.0
        k_droop_kw_per_hz = self.system_base_kw / (self.nominal_hz * self.droop_r)
        eta = 0.95
        ramp_kw_s = 2500.0

        for _ in range(substeps):
            # Droop control: the grid former responds to frequency, not to a schedule.
            want = k_droop_kw_per_hz * (self.nominal_hz - self.frequency_hz)
            want = min(self.bess_max_discharge_kw, max(-self.bess_max_charge_kw, want))
            self._hold = HOLD_NONE
            if want > 0 and self.soc_pct <= self.soc_min_pct:
                want, self._hold = 0.0, HOLD_SOC_LOW
            elif want < 0 and self.soc_pct >= self.soc_max_pct:
                want, self._hold = 0.0, HOLD_SOC_HIGH
            elif abs(want) >= min(self.bess_max_discharge_kw, self.bess_max_charge_kw) - 1e-6:
                self._hold = HOLD_POWER_LIMIT

            self.p_bess_kw += max(-ramp_kw_s * h, min(ramp_kw_s * h, want - self.p_bess_kw))

            # State of charge from what the battery actually did, one-way efficiencies.
            if self.p_bess_kw >= 0:
                self.soc_pct -= (self.p_bess_kw / eta) * h / 3600.0 / self.bess_kwh * 100.0
            else:
                self.soc_pct -= (self.p_bess_kw * eta) * h / 3600.0 / self.bess_kwh * 100.0
            self.soc_pct = min(100.0, max(0.0, self.soc_pct))

            # The swing equation. Low inertia on purpose: this is an inverter-based island.
            net_kw = pv_kw + wind_kw + self.p_bess_kw - load_kw
            dfdt = (net_kw / self.system_base_kw) * (self.nominal_hz / (2.0 * self.inertia_h))
            self._last_f = self.frequency_hz
            self.frequency_hz += dfdt * h
            self.frequency_hz = min(55.0, max(45.0, self.frequency_hz))
            self._rocof = dfdt

        self.throughput_kwh += abs(self.p_bess_kw) * dt / 3600.0

        # --- protection: shed load, then curtail every source (the only two actions) -----
        # Ramped, not jumped: a protection action that moves 100 % in one sample is the same
        # numerical artefact as the one above, one layer out.
        if self.frequency_hz < self.shed_hz:
            target_shed = min(100.0, (self.shed_hz - self.frequency_hz) / 0.30 * 100.0)
            self._ev_shed_pct = min(target_shed, self._ev_shed_pct + 50.0 * dt)
        else:
            self._ev_shed_pct = max(0.0, self._ev_shed_pct - 10.0 * dt)
        if self.frequency_hz > self.curtail_hz:
            target_curt = min(100.0, (self.frequency_hz - self.curtail_hz) / 0.35 * 100.0)
            self._renew_curtail_pct = min(target_curt, self._renew_curtail_pct + 50.0 * dt)
        else:
            self._renew_curtail_pct = max(0.0, self._renew_curtail_pct - 10.0 * dt)

        # --- status ------------------------------------------------------------------
        if self._hold != HOLD_NONE:
            self._st = BESS_HOLD
        elif self.p_bess_kw > 2.0:
            self._st = BESS_DISCHARGING
        elif self.p_bess_kw < -2.0:
            self._st = BESS_CHARGING
        else:
            self._st = BESS_IDLE

        tmp = 24.0 + 0.055 * abs(self.p_bess_kw) + self._noise(0.15)
        # A grid-forming inverter holds voltage; that is its job. The busbar stays far
        # steadier than the rural site's weak feeder, which is why a *frequency* event is
        # this site's signature and a *voltage* sag is the rural site's.
        v_ph = self.nominal_voltage - 0.004 * (load_kw - self.load_base_kw) + self._noise(0.12)

        return {
            # -- the busbar
            "Hz": self.frequency_hz,
            "V": v_ph,
            "RoCoF": self._rocof,
            "GridFormingUnit": 1,          # unit 1 (the BESS) is the grid former
            # -- the grid-forming BESS, one block
            "SoC": self.soc_pct,
            "W": self.p_bess_kw * 1000.0,  # positive = discharge into the busbar
            "WH": self.throughput_kwh,
            "MaxChargeW": -self.bess_max_charge_kw * 1000.0,
            "MaxDischargeW": self.bess_max_discharge_kw * 1000.0,
            "UsableWH": self.bess_kwh,
            "St": self._st,
            "HoldReason": self._hold,
            "TmpCab": tmp,
            # -- two repeating source blocks: solar, wind
            "SrcID_1": SRC_SOLAR, "W_1": pv_kw * 1000.0,
            "AvailableW_1": pv_available_kw * 1000.0, "Avail_1": 1,
            "SrcID_2": SRC_WIND, "W_2": wind_kw * 1000.0,
            "AvailableW_2": wind_available_kw * 1000.0, "Avail_2": 1,
            # -- the load, and the imbalance before the battery covered it
            "LoadW": load_kw * 1000.0,
            "EVW": ev_kw * 1000.0,
            "BalanceW": (pv_kw + wind_kw - load_kw) * 1000.0,  # before the battery covered it
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
            "device_kind": "microgrid",
            "uptime_s": self.uptime_s(),
            "device_clock_skew_s": self.clock.snapshot()["clock_skew_s"],
            "register_count": self.regmap.total_regs,
            "frequency_hz": round(self.frequency_hz, 3),
            "rocof_hz_s": round(self._rocof, 4),
            "bess": {
                "soc_pct": round(self.soc_pct, 1),
                "power_kw": round(self.p_bess_kw, 1),
                "hold": ["none", "soc-high", "soc-low", "power-limit"][self._hold],
                "usable_kwh": self.bess_kwh,
                "max_charge_kw": self.bess_max_charge_kw,
                "max_discharge_kw": self.bess_max_discharge_kw,
            },
            "pv_kwp": self.pv_kwp,
            "wind_rated_kw": self.wind_rated_kw,
            "renewable_curtail_pct": round(self._renew_curtail_pct, 1),
            "ev_shed_pct": round(self._ev_shed_pct, 1),
            "load_step": self.loadstep.snapshot(),
        }
