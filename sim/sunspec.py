"""SunSpec-shaped Modbus register maps, shared by the device simulators and the poller.

Why SunSpec, and why this file is shared
----------------------------------------
SunSpec is a standardised *interpretation* of a raw Modbus register file. Its defining
characteristics are exactly the things that make OT integration hard, so we reproduce all
of them:

* Registers hold **integers**. The real value is ``raw x 10^scale_factor`` and the scale
  factor lives in its own register next to the value.
* 32-bit quantities (the energy accumulators) span **two 16-bit registers**, and word
  order is vendor-dependent. ``(reg0 << 16) | reg1`` is a classic 3am bug.
* There is no timestamp and no quality flag anywhere in Modbus. Both have to be invented
  by whoever reads the registers.
* A "not implemented" sentinel (int16 ``0x8000`` / acc32 ``0xFFFFFFFF``) means "this point
  exists in the map but there is no value behind it".

The devices write with this module and the poller reads with it, so the encoder and the
decoder cannot drift apart. It is deliberately a *subset* of the real models: enough to be
recognisable to an electrical engineer, not a second implementation of the spec (D4).

Two device maps, one layout engine
----------------------------------
The chain of SunSpec models a device exposes is a property of the *device*, not of the
protocol, so there are two maps here and one layout engine:

* ``meter``    - common, model 203 (three-phase wye meter), vendor 64001. This is the M1a
                 rural-site feeder meter, unchanged.
* ``inverter`` - common, model 103 (three-phase PV inverter), model 160 (MPPT, two
                 repeating blocks), model 302 (irradiance), model 303 (back-of-module and
                 ambient temperature), model 123 (immediate controls / export limit) and
                 vendor 64001. This is the M1b utility-scale solar farm.

Both are laid out by :func:`_build_map`; offsets are computed, never hand-written. A
repeating SunSpec model (160) is expressed with ``repeats``, and its points are keyed by
block instance (``MPPT_DCA_1``) so the poller can put the block number on the wire as an
attribute instead of minting a metric name per block.

Register image
--------------
The image is a flat list of 16-bit registers, index 0 == Modbus address ``BASE``. Offsets
are computed below rather than hand-written.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# SunSpec data starts at "register 40000". We keep the whole image 0-based from here, so
# offset 0 in the image is address 40000 on the wire. The poller uses the same base.
BASE = 40000
MARKER = (0x5375, 0x6E53)  # "Su", "nS" - the SunSpec start marker
END_MARKER = (0xFFFF, 0x0000)

#: int16 sentinel for "point is in the map, but not implemented on this device"
NOT_IMPLEMENTED_I16 = 0x8000
#: accumulator sentinel for the same thing
NOT_IMPLEMENTED_ACC32 = 0xFFFFFFFF

WORD_ORDER_HIGH_FIRST = 0  # most significant word first: (reg0 << 16) | reg1
WORD_ORDER_LOW_FIRST = 1   # vendor's reversed order:      (reg1 << 16) | reg0

#: The scale factors the *meter* publishes. One table, used by the device to encode and by
#: the poller to decode - they cannot disagree about what 2314 means.
METER_SCALE_FACTORS: dict[str, int] = {
    "A_SF": -1,      # 0.1 A
    "V_SF": -1,      # 0.1 V
    "Hz_SF": -2,     # 0.01 Hz
    "W_SF": 0,       # 1 W
    "VA_SF": 0,      # 1 VA
    "VAR_SF": 0,     # 1 var
    "PF_SF": -3,     # 0.001
    "TotWh_SF": -3,  # Wh -> kWh
    "Tmp_SF": -1,    # 0.1 degC
}

#: The scale factors the *inverter* publishes. Deliberately not the meter's table: a 4 MW
#: inverter cannot fit its output in an int16 at 1 W resolution, so its power scale factor
#: is +3 (kW) while the meter's is 0 (W). Sharing one table would have silently forced one
#: device to lie about its resolution.
INVERTER_SCALE_FACTORS: dict[str, int] = {
    "A_SF": 0,              # 1 A. This is not laziness: a 4.2 MVA central inverter at
                             # 690 V AC carries ~3.5 kA, and at 0.1 A resolution that is
                             # 35 000 counts - past int16. The meter's A_SF=-1 is right for
                             # a 40 A feeder and wrong for this device, which is exactly why
                             # the two maps do not share one scale-factor table.
    "V_SF": -1,             # 0.1 V -> 398 V L-N and 690 V L-L both fit comfortably
    "Hz_SF": -2,            # 0.01 Hz
    "W_SF": 3,              # raw is kW, so 4 200 kW fits
    "VA_SF": 3,             # raw is kVA
    "VAR_SF": 3,            # raw is kvar
    "PF_SF": -3,            # 0.001
    "WH_SF": -1,            # 0.1 kWh -> 429 GWh before the acc32 rolls over
    "Tmp_SF": -1,           # 0.1 degC
    "WMaxLimPct_SF": -2,    # 0.01 %
    "Irradiance_SF": 0,     # 1 W/m2
}

_REG_SIZE = {"int16": 1, "uint16": 1, "acc32": 2, "uint32": 2}


@dataclass(frozen=True)
class Point:
    """One named register (or pair of registers) in the image."""

    name: str          # unique key in the map; block points carry their instance suffix
    kind: str          # int16 | uint16 | acc32 | uint32
    sf: str | None     # name of the scale-factor point, or None
    group: str | None  # canonical quantity group; None for config/vendor points
    phase: str | None  # the device's own phase naming: "A" | "B" | "C" | None
    unit: str | None   # UCUM engineering unit
    direction: str | None  # "import" | "export" for energy accumulators
    offset: int        # register offset from BASE
    regs: int          # number of 16-bit registers
    model: str         # "common" | "meter" | "inverter" | "mppt" | ...
    base_name: str = ""    # the point's name inside its model, without the instance suffix
    instance: int = 0      # 1-based repeating-block index; 0 when the model does not repeat

    @property
    def address(self) -> int:
        """Absolute Modbus address of the first register."""
        return BASE + self.offset


@dataclass
class Decoded:
    name: str
    raw: int | None          # the integer that was on the wire, before scaling
    value: float | None      # raw x 10^sf
    scale_factor: int | None
    not_implemented: bool


@dataclass(frozen=True)
class Model:
    """One SunSpec model block in a device's chain."""

    id: int
    name: str
    #: (name, kind, sf, group, phase, unit, direction) - the same tuple shape as M1a used.
    entries: tuple = ()
    repeats: int = 1
    #: Fixed register count for models this file does not populate point-by-point (model 1).
    length: int | None = None


# --------------------------------------------------------------------------------------
# Model 1 (common). The strings live outside the point table because none of them is a
# numeric reading; the layout engine only needs to know the model's length.
# --------------------------------------------------------------------------------------
COMMON_LEN = 65


# --------------------------------------------------------------------------------------
# Model 203 (three-phase wye meter). Field order follows model 203 for the subset we
# implement; the length field reflects the subset, which is honest about being a subset
# rather than pretending to be a full 203. This is the M1a rural meter, unchanged.
# --------------------------------------------------------------------------------------
METER_ENTRIES: list[tuple] = [
    # name,        kind,     sf,         group,             phase, unit,  direction
    ("A",          "int16",  "A_SF",     "current",         None,  "A",    None),
    ("AphA",       "int16",  "A_SF",     "current",         "A",   "A",    None),
    ("AphB",       "int16",  "A_SF",     "current",         "B",   "A",    None),
    ("AphC",       "int16",  "A_SF",     "current",         "C",   "A",    None),
    ("A_SF",       "int16",  None,       None,              None,  None,   None),
    ("V",          "int16",  "V_SF",     "voltage",         None,  "V",    None),
    ("VphA",       "int16",  "V_SF",     "voltage",         "A",   "V",    None),
    ("VphB",       "int16",  "V_SF",     "voltage",         "B",   "V",    None),
    ("VphC",       "int16",  "V_SF",     "voltage",         "C",   "V",    None),
    ("V_SF",       "int16",  None,       None,              None,  None,   None),
    ("Hz",         "int16",  "Hz_SF",    "frequency",       None,  "Hz",   None),
    ("Hz_SF",      "int16",  None,       None,              None,  None,   None),
    ("W",          "int16",  "W_SF",     "power.real",      None,  "W",    None),
    ("WphA",       "int16",  "W_SF",     "power.real",      "A",   "W",    None),
    ("WphB",       "int16",  "W_SF",     "power.real",      "B",   "W",    None),
    ("WphC",       "int16",  "W_SF",     "power.real",      "C",   "W",    None),
    ("W_SF",       "int16",  None,       None,              None,  None,   None),
    ("VA",         "int16",  "VA_SF",    "power.apparent",  None,  "VA",   None),
    ("VAphA",      "int16",  "VA_SF",    "power.apparent",  "A",   "VA",   None),
    ("VAphB",      "int16",  "VA_SF",    "power.apparent",  "B",   "VA",   None),
    ("VAphC",      "int16",  "VA_SF",    "power.apparent",  "C",   "VA",   None),
    ("VA_SF",      "int16",  None,       None,              None,  None,   None),
    ("VAR",        "int16",  "VAR_SF",   "power.reactive",  None,  "var",  None),
    ("VARphA",     "int16",  "VAR_SF",   "power.reactive",  "A",   "var",  None),
    ("VARphB",     "int16",  "VAR_SF",   "power.reactive",  "B",   "var",  None),
    ("VARphC",     "int16",  "VAR_SF",   "power.reactive",  "C",   "var",  None),
    ("VAR_SF",     "int16",  None,       None,              None,  None,   None),
    ("PF",         "int16",  "PF_SF",    "power_factor",    None,  "1",    None),
    ("PFphA",      "int16",  "PF_SF",    "power_factor",    "A",   "1",    None),
    ("PFphB",      "int16",  "PF_SF",    "power_factor",    "B",   "1",    None),
    ("PFphC",      "int16",  "PF_SF",    "power_factor",    "C",   "1",    None),
    ("PF_SF",      "int16",  None,       None,              None,  None,   None),
    ("TotWhExp",   "acc32",  "TotWh_SF", "energy",          None,  "kW.h", "export"),
    ("TotWhImp",   "acc32",  "TotWh_SF", "energy",          None,  "kW.h", "import"),
    ("TotWh_SF",   "int16",  None,       None,              None,  None,   None),
    ("TmpCab",     "int16",  "Tmp_SF",   "temperature",     None,  "Cel",  None),
    ("Tmp_SF",     "int16",  None,       None,              None,  None,   None),
]

# --------------------------------------------------------------------------------------
# Model 103 (three-phase inverter). AC side, DC aggregate, energy, temperature and status.
#
# Two differences from the meter that matter to a domain reader:
#   * energy has one direction only. A PV inverter exports; it does not have an import
#     register, and pretending it does invites a sign bug.
#   * `WH` is AC lifetime energy and `DCWH` is DC lifetime energy. Their ratio, the
#     conversion efficiency, is the number that tells you the inverter is healthy.
# St is the SunSpec operating-state enum (4 = MPPT, 3 = sleeping, 7 = fault, ...); Evt1 is
# a bitfield. They are carried as points and given meaning by the edge collector, not here.
# --------------------------------------------------------------------------------------
INVERTER_ENTRIES: list[tuple] = [
    # name,       kind,     sf,          group,             phase, unit,  direction
    ("A",         "int16",  "A_SF",      "current",         None,  "A",    None),
    ("AphA",      "int16",  "A_SF",      "current",         "A",   "A",    None),
    ("AphB",      "int16",  "A_SF",      "current",         "B",   "A",    None),
    ("AphC",      "int16",  "A_SF",      "current",         "C",   "A",    None),
    ("A_SF",      "int16",  None,        None,              None,  None,   None),
    ("PPVphAB",   "int16",  "V_SF",      "voltage.line",    None,  "V",    None),
    ("PPVphBC",   "int16",  "V_SF",      "voltage.line",    None,  "V",    None),
    ("PPVphCA",   "int16",  "V_SF",      "voltage.line",    None,  "V",    None),
    ("PhVphA",    "int16",  "V_SF",      "voltage",         "A",   "V",    None),
    ("PhVphB",    "int16",  "V_SF",      "voltage",         "B",   "V",    None),
    ("PhVphC",    "int16",  "V_SF",      "voltage",         "C",   "V",    None),
    ("V_SF",      "int16",  None,        None,              None,  None,   None),
    ("W",         "int16",  "W_SF",      "power.real",      None,  "W",    None),
    ("W_SF",      "int16",  None,        None,              None,  None,   None),
    ("Hz",        "int16",  "Hz_SF",     "frequency",       None,  "Hz",   None),
    ("Hz_SF",     "int16",  None,        None,              None,  None,   None),
    ("VA",        "int16",  "VA_SF",     "power.apparent",  None,  "VA",   None),
    ("VA_SF",     "int16",  None,        None,              None,  None,   None),
    ("VAr",       "int16",  "VAR_SF",    "power.reactive",  None,  "var",  None),
    ("VAR_SF",    "int16",  None,        None,              None,  None,   None),
    ("PF",        "int16",  "PF_SF",     "power_factor",    None,  "1",    None),
    ("PF_SF",     "int16",  None,        None,              None,  None,   None),
    ("WH",        "acc32",  "WH_SF",     "energy",          None,  "kW.h", "export"),
    ("WH_SF",     "int16",  None,        None,              None,  None,   None),
    ("DCA",       "int16",  "A_SF",      "dc.current",      None,  "A",    None),
    ("DCV",       "int16",  "V_SF",      "dc.voltage",      None,  "V",    None),
    ("DCW",       "int16",  "W_SF",      "dc.power",        None,  "W",    None),
    ("DCWH",      "acc32",  "WH_SF",     "dc.energy",       None,  "kW.h", "export"),
    ("TmpCab",    "int16",  "Tmp_SF",    "temperature",     None,  "Cel",  None),
    ("TmpSnk",    "int16",  "Tmp_SF",    "temperature",     None,  "Cel",  None),
    ("Tmp_SF",    "int16",  None,        None,              None,  None,   None),
    ("St",        "uint16", None,        "status",          None,  "1",    None),
    ("StVnd",     "uint16", None,        "status.vendor",   None,  "1",    None),
    ("Evt1",      "uint32", None,        "event",           None,  "1",    None),
]

# --------------------------------------------------------------------------------------
# Model 160 (MPPT), two repeating blocks. Per-block DC voltage, current, power and module
# temperature. This is where "per-MPPT variance rather than one smooth number" comes from:
# a cloud edge crosses one tracker before the other and the two blocks diverge.
#
# The scale-factor registers are not repeated here; the points reference the inverter
# model's A_SF/V_SF/W_SF/Tmp_SF, which is what the subset buys. A full model 160 carries its
# own SF registers per block.
# --------------------------------------------------------------------------------------
MPPT_ENTRIES: list[tuple] = [
    # name,        kind,     sf,        group,          phase, unit,  direction
    ("MPPT_ID",    "uint16", None,      "mppt.id",      None,  "1",    None),
    ("MPPT_DCA",   "uint16", "A_SF",    "dc.current",   None,  "A",    None),
    ("MPPT_DCV",   "uint16", "V_SF",    "dc.voltage",   None,  "V",    None),
    ("MPPT_DCW",   "uint16", "W_SF",    "dc.power",     None,  "W",    None),
    ("MPPT_Tmp",   "int16",  "Tmp_SF",  "temperature",  None,  "Cel",  None),
]

# --------------------------------------------------------------------------------------
# Model 302 (irradiance) and model 303 (temperature). These are the pair the domain reader
# checks first: without them the AC power curve is just a shape, not a measurement of the
# resource. GH is global horizontal; POA is plane-of-array, which is the one the DC model
# actually uses.
# --------------------------------------------------------------------------------------
IRRADIANCE_ENTRIES: list[tuple] = [
    ("GH",  "uint16", "Irradiance_SF", "irradiance",      None, "W/m2", None),
    ("POA", "uint16", "Irradiance_SF", "irradiance.poa",  None, "W/m2", None),
]

TEMPERATURE_ENTRIES: list[tuple] = [
    ("TmpBOM", "int16", "Tmp_SF", "temperature.module",   None, "Cel", None),
    ("TmpAmb", "int16", "Tmp_SF", "temperature.ambient",  None, "Cel", None),
]

# --------------------------------------------------------------------------------------
# Model 123 (immediate controls) subset. The export limit is a *control*, written by the
# network operator and read back by the inverter; WMaxLimPct is a percentage of nameplate
# AC. It is in the register file because curtailment is a fact about the device, not a
# calculation a dashboard should invent.
# --------------------------------------------------------------------------------------
CONTROLS_ENTRIES: list[tuple] = [
    ("WMaxLimPct",    "uint16", "WMaxLimPct_SF", "limit", None, "%", None),
    ("WMaxLimPct_SF", "int16",  None,            None,    None, None, None),
]

# Vendor model 64001. SunSpec reserves 64000+ for vendor extensions; a device clock, a
# register word-order switch, a fault code and a curtailment state are the things this
# device has that the standard models do not. UnixTime is how a Modbus device can ever
# claim *when* a reading was taken - Modbus itself carries no time at all.
_VENDOR_ENTRIES: list[tuple] = [
    # name,         kind,     sf,   group,  phase, unit, direction
    ("UnixTime",    "uint32", None, None,   None,  "s",   None),
    ("ClockOffsetS", "int16", None, None,   None,  "s",   None),
    ("WordOrder",   "int16",  None, None,   None,  "1",   None),
]

INVERTER_VENDOR_ENTRIES: list[tuple] = _VENDOR_ENTRIES + [
    ("FaultCode",   "uint16", None, "fault",       None, "1", None),
    ("CurtailState", "uint16", None, "curtailment", None, "1", None),
]

# --------------------------------------------------------------------------------------
# Wind farm. SunSpec has no widely-deployed turbine model in the classic 100-series (the
# inverter/meter/storage models are the ones the industry actually shipped), so the turbine
# points are a VENDOR model that follows SunSpec's register conventions rather than a
# fabricated "model 1xx". docs/standards.md says so, because inventing a model number and
# implying it is standard would be the one thing a domain reader could not forgive.
#
# The turbine model REPEATS, one block per turbine, exactly like model 160 (MPPT): the farm
# is six machines sharing one Modbus device, and `repeats` + the instance attribute is how
# six sets of points stay one metric each. Per-turbine variance is then a property of the
# data, not of six metric names.
# --------------------------------------------------------------------------------------
WIND_TURBINE_ENTRIES: list[tuple] = [
    # name,          kind,     sf,          group,                 phase, unit,    direction
    ("WTG_ID",      "uint16", None,        "turbine.id",          None,  "1",     None),
    ("WindSpeed",   "int16",  "Wind_SF",   "wind.speed",          None,  "m/s",   None),
    ("WindDir",     "uint16", None,        "wind.direction",      None,  "deg",   None),
    ("RotorRpm",    "int16",  "Rpm_SF",    "rotor.speed",         None,  "/min",  None),
    ("PitchAngle",  "int16",  "Pitch_SF",  "rotor.pitch",         None,  "deg",   None),
    ("W",           "int16",  "W_SF",      "power.real",          None,  "W",     None),
    ("VAr",         "int16",  "VAR_SF",    "power.reactive",      None,  "var",   None),
    ("WH",          "acc32",  "WH_SF",     "energy",              None,  "kW.h",  "export"),
    ("A",           "int16",  "A_SF",      "current",             None,  "A",     None),
    ("V",           "int16",  "V_SF",      "voltage",             None,  "V",     None),
    ("TmpNac",      "int16",  "Tmp_SF",    "temperature",         None,  "Cel",   None),
    ("TmpGear",     "int16",  "Tmp_SF",    "temperature.gearbox", None,  "Cel",   None),
    ("St",          "uint16", None,        "status",              None,  "1",     None),
    ("Avail",       "uint16", None,        "availability",        None,  "1",     None),
]

# The point of connection, summed by the farm controller. A real wind farm's SCADA publishes
# both the machines and the connection point; the two disagree by the collector network and
# transformer losses, and an engineer expects to see both.
WIND_FARM_ENTRIES: list[tuple] = [
    ("FarmW",        "int16",  "W_SF",     "power.real",          None, "W",     None),
    ("FarmVAr",      "int16",  "VAR_SF",   "power.reactive",      None, "var",   None),
    ("FarmWH",       "acc32",  "WH_SF",    "energy",              None, "kW.h",  "export"),
    ("Hz",           "int16",  "Hz_SF",    "frequency",           None, "Hz",    None),
    ("TmpAmb",       "int16",  "Tmp_SF",   "temperature.ambient", None, "Cel",   None),
    ("WindSpeedAvg", "int16",  "Wind_SF",  "wind.speed",          None, "m/s",   None),
    ("WindDirAvg",   "uint16", None,        "wind.direction",      None, "deg",   None),
    ("FarmSt",       "uint16", None,        "status",              None, "1",     None),
    ("FarmAvail",    "uint16", None,        "availability",        None, "1",     None),
]

# --------------------------------------------------------------------------------------
# Microgrid. An islanded site: no feeder, no NEM, its own grid former. The register chain
# follows the same convention as everything else here - a vendor model per device class,
# because SunSpec's shipped 100-series models are grid-tied device models and a
# grid-forming BESS with a frequency reference is not one of them. docs/standards.md says
# which parts of this are modelled physics and which are invented outright.
#
# The shape that matters: the busbar (`grid`) carries frequency and voltage, the BESS is ONE
# unit that is also the grid former, the two renewable sources repeat (model 160/64022
# pattern: one metric, one data point per source), and the load is its own block so that
# "the chargers started" is a fact in the data rather than a footnote in a demo script.
# --------------------------------------------------------------------------------------
MICROGRID_GRID_ENTRIES: list[tuple] = [
    # name,             kind,     sf,             group,           phase, unit,  direction
    ("Hz",             "int16",  "Hz_SF",        "frequency",     None,  "Hz",   None),
    ("V",              "int16",  "V_SF",         "voltage",       None,  "V",    None),
    ("RoCoF",          "int16",  "RoCoF_SF",     "rocof",         None,  "Hz/s", None),
    ("GridFormingUnit", "uint16", None,          "grid_forming",  None,  "1",    None),
]

MICROGRID_BESS_ENTRIES: list[tuple] = [
    ("SoC",            "uint16", "SoC_SF",       "state_of_charge", None, "%",   None),
    ("W",              "int16",  "W_SF",         "power.real",      None, "W",    None),
    ("WH",             "acc32",  "WH_SF",        "energy",          None, "kW.h", None),
    ("MaxChargeW",     "int16",  "W_SF",         "power.max_charge", None, "W",   None),
    ("MaxDischargeW",  "int16",  "W_SF",         "power.max_discharge", None, "W", None),
    ("UsableWH",       "int16",  "WH_SF",        "energy.usable",   None, "kW.h", None),
    ("St",             "uint16", None,            "status",          None, "1",    None),
    ("HoldReason",     "uint16", None,            "hold_reason",     None, "1",    None),
    ("TmpCab",         "int16",  "Tmp_SF",       "temperature",     None, "Cel",  None),
]

MICROGRID_SOURCE_ENTRIES: list[tuple] = [
    ("SrcID",          "uint16", None,            "source.id",       None, "1",    None),
    ("W",              "int16",  "W_SF",         "power.real",      None, "W",    None),
    ("AvailableW",     "int16",  "W_SF",         "power.available", None, "W",    None),
    ("Avail",          "uint16", None,            "availability",    None, "1",    None),
]

MICROGRID_LOAD_ENTRIES: list[tuple] = [
    ("LoadW",          "int16",  "W_SF",         "power.real",      None, "W",    None),
    ("EVW",            "int16",  "W_SF",         "power.ev",        None, "W",    None),
    ("BalanceW",       "int16",  "W_SF",         "power.balance",   None, "W",    None),
]

MICROGRID_SCALE_FACTORS: dict[str, int] = {
    "V_SF": -1,             # 0.1 V
    "Hz_SF": -2,            # 0.01 Hz - the resolution a protection relay actually sees
    "RoCoF_SF": -3,         # 0.001 Hz/s
    "W_SF": 3,              # kW: a 250 kW battery and a 400 kW load both fit int16
    "WH_SF": -1,            # 0.1 kWh
    "Tmp_SF": -1,           # 0.1 degC
    "SoC_SF": -1,           # 0.1 %
}


WIND_SCALE_FACTORS: dict[str, int] = {
    "A_SF": 0,              # 1 A: a 2.5 MW turbine at 690 V carries ~2 kA
    "V_SF": -1,             # 0.1 V
    "Hz_SF": -2,            # 0.01 Hz
    "W_SF": 3,              # kW: 15 MW farm aggregate fits int16 with room
    "VAR_SF": 3,            # kvar
    "WH_SF": -1,            # 0.1 kWh -> 429 GWh before the acc32 rolls over
    "Tmp_SF": -1,           # 0.1 degC
    "Wind_SF": -1,          # 0.1 m/s: the resolution a met mast actually reports
    "Rpm_SF": -1,           # 0.1 rpm
    "Pitch_SF": -1,         # 0.1 deg
    "WMaxLimPct_SF": -2,    # 0.01 %
}


@dataclass(frozen=True)
class RegisterMap:
    """A fully laid-out device image: the model chain plus every point's offset."""

    kind: str
    models: tuple[Model, ...]
    points: dict[str, Point]
    scale_factors: dict[str, int]
    #: model name -> OTLP metric namespace, e.g. "inverter" -> "modbus.inverter". The poller
    #: composes `<namespace>.<point>`; it is what keeps a PV inverter's `W` from colliding
    #: with a feeder meter's `W` in one shared edge config (D20).
    namespaces: dict[str, str]
    total_regs: int
    end_at: int
    common_data: int = 0
    header_offsets: dict[str, int] = field(default_factory=dict)
    data_offsets: dict[str, int] = field(default_factory=dict)
    lengths: dict[str, int] = field(default_factory=dict)

    # -- construction ------------------------------------------------------------------
    def base_image(self) -> list[int]:
        """The parts of the register image that never change: markers, headers, strings, SFs."""
        img = [0] * self.total_regs
        img[0:2] = list(MARKER)
        for model in self.models:
            img[self.header_offsets[model.name]] = model.id
            img[self.header_offsets[model.name] + 1] = self.lengths[model.name]
        # Scale-factor registers never move, so they are written once into the base image.
        for name, value in self.scale_factors.items():
            point = self.points.get(name)
            if point is not None and point.regs == 1:
                img[point.offset] = value & 0xFFFF
        # Model 1 strings. Same layout as M1a: Mn, Md, Opt, Vr, SN, DA.
        d = self.common_data
        img[d + 0 : d + 16] = _string_regs("VECN", 16)
        img[d + 16 : d + 32] = _string_regs(self._model_string(), 16)
        img[d + 32 : d + 40] = _string_regs("subset", 8)
        img[d + 40 : d + 48] = _string_regs("1.0.0", 8)
        img[d + 48 : d + 64] = _string_regs(self._serial(), 16)
        img[d + 64] = 1
        img[self.end_at] = END_MARKER[0]
        img[self.end_at + 1] = END_MARKER[1]
        return img

    def _model_string(self) -> str:
        return {
            "meter": "SIM-METER-3P-WYE",
            "inverter": "SIM-INVERTER-3P",
            "microgrid": "SIM-MICROGRID-1",
        }.get(self.kind, "SIM-INVERTER-3P")

    def _serial(self) -> str:
        return {
            "meter": "SIM0000000000001",
            "inverter": "SIMINV0000000001",
            "microgrid": "SIMMG0000000001",
        }.get(self.kind, "SIMINV0000000001")

    # -- codec -------------------------------------------------------------------------
    def encode(
        self,
        values: dict[str, float],
        device_time: int,
        word_order: int = WORD_ORDER_LOW_FIRST,
        clock_offset_s: int = 0,
        image: list[int] | None = None,
    ) -> list[int]:
        """Engineering values -> raw register image.

        ``values`` is keyed by SunSpec point name (e.g. ``"VphA": 231.4``). Anything not in
        the map is ignored. The caller supplies the device's own clock reading; the device
        does not let the poller decide what time it is.
        """
        img = list(image) if image is not None else self.base_image()
        for name, value in values.items():
            point = self.points.get(name)
            if point is None or point.group is None:
                continue
            sf = self.scale_factors[point.sf] if point.sf else 0
            raw = round(value / (10.0 ** sf))
            _write_raw(img, point, raw, word_order)
        _write_raw(img, self.points["UnixTime"], device_time, word_order)
        _write_raw(img, self.points["ClockOffsetS"], clock_offset_s, word_order)
        _write_raw(img, self.points["WordOrder"], word_order, word_order)
        return img

    def decode(self, img: list[int]) -> dict[str, Decoded]:
        """Raw register image -> decoded values.

        Two passes, because the 32-bit accumulators cannot be read until the device's own
        ``WordOrder`` register has been read. Getting that order wrong is exactly the vendor
        word-order bug, so we do it explicitly rather than assuming big-endian.
        """
        out: dict[str, Decoded] = {}
        for point in self.points.values():
            if point.regs != 1:
                continue
            raw, missing = _decode_16(point, img)
            sf = self.scale_factors[point.sf] if point.sf else 0
            value = None if missing else raw * (10.0 ** sf)
            out[point.name] = Decoded(point.name, raw, value, sf, missing)

        word_order_point = out.get("WordOrder")
        word_order = (
            word_order_point.raw
            if word_order_point and word_order_point.raw in (WORD_ORDER_HIGH_FIRST, WORD_ORDER_LOW_FIRST)
            else WORD_ORDER_HIGH_FIRST
        )
        for point in self.points.values():
            if point.regs == 1:
                continue
            raw, missing = _decode_32(point, img, word_order)
            sf = self.scale_factors[point.sf] if point.sf else 0
            value = None if missing else raw * (10.0 ** sf)
            out[point.name] = Decoded(point.name, raw, value, sf, missing)
        return out


def _build_map(kind: str, models: list[Model], scale_factors: dict[str, int],
               namespaces: dict[str, str]) -> RegisterMap:
    """Lay out a model chain into a flat register image and an offset-indexed point table."""
    points: dict[str, Point] = {}
    header_offsets: dict[str, int] = {}
    data_offsets: dict[str, int] = {}
    lengths: dict[str, int] = {}
    common_data = 0

    cursor = 2  # the start marker occupies the first two registers
    for model in models:
        header_offsets[model.name] = cursor
        cursor += 2
        data_offsets[model.name] = cursor
        if model.name == "common":
            common_data = cursor
        start = cursor
        if model.entries:
            for block in range(1, model.repeats + 1):
                for name, pkind, sf, group, phase, unit, direction in model.entries:
                    regs = _REG_SIZE[pkind]
                    key = f"{name}_{block}" if model.repeats > 1 else name
                    points[key] = Point(
                        key, pkind, sf, group, phase, unit, direction, cursor, regs, model.name,
                        base_name=name, instance=block if model.repeats > 1 else 0,
                    )
                    cursor += regs
            lengths[model.name] = cursor - start
        elif model.length is not None:
            cursor = start + model.length
            lengths[model.name] = model.length
        else:
            lengths[model.name] = 0

    end_at = cursor
    cursor += 2  # end marker
    return RegisterMap(
        kind=kind,
        models=tuple(models),
        points=points,
        scale_factors=dict(scale_factors),
        namespaces=dict(namespaces),
        total_regs=cursor,
        end_at=end_at,
        common_data=common_data,
        header_offsets=header_offsets,
        data_offsets=data_offsets,
        lengths=lengths,
    )


#: The M1a rural meter. Byte-for-byte the chain this file built before M1b: model 1, model
#: 203, vendor 64001.
METER_MAP = _build_map(
    "meter",
    [Model(1, "common", (), length=COMMON_LEN),
     Model(203, "meter", tuple(METER_ENTRIES)),
     Model(64001, "vendor", tuple(_VENDOR_ENTRIES))],
    METER_SCALE_FACTORS,
    {"meter": "modbus.register", "vendor": "modbus.vendor"},
)

#: The M1b utility-scale solar inverter: model 1, model 103, model 160 (two blocks), model
#: 302, model 303, model 123, vendor 64001.
INVERTER_MAP = _build_map(
    "inverter",
    [Model(1, "common", (), length=COMMON_LEN),
     Model(103, "inverter", tuple(INVERTER_ENTRIES)),
     Model(160, "mppt", tuple(MPPT_ENTRIES), repeats=2),
     Model(302, "irradiance", tuple(IRRADIANCE_ENTRIES)),
     Model(303, "temperature", tuple(TEMPERATURE_ENTRIES)),
     Model(123, "controls", tuple(CONTROLS_ENTRIES)),
     Model(64001, "vendor", tuple(INVERTER_VENDOR_ENTRIES))],
    INVERTER_SCALE_FACTORS,
    {"inverter": "modbus.inverter", "mppt": "modbus.mppt", "irradiance": "modbus.irradiance",
     "temperature": "modbus.temperature", "controls": "modbus.controls", "vendor": "modbus.vendor"},
)

MAPS: dict[str, RegisterMap] = {"meter": METER_MAP, "inverter": INVERTER_MAP}

#: The M1b wind farm: model 1, the repeating turbine vendor model (six machines), the farm
#: aggregate, model 123 (export limit) and the shared vendor model.
WIND_MAP = _build_map(
    "windfarm",
    [Model(1, "common", (), length=COMMON_LEN),
     Model(64010, "turbine", tuple(WIND_TURBINE_ENTRIES), repeats=6),
     Model(64011, "farm", tuple(WIND_FARM_ENTRIES)),
     Model(123, "controls", tuple(CONTROLS_ENTRIES)),
     Model(64001, "vendor", tuple(INVERTER_VENDOR_ENTRIES))],
    WIND_SCALE_FACTORS,
    {"turbine": "modbus.turbine", "farm": "modbus.farm", "controls": "modbus.controls",
     "vendor": "modbus.vendor"},
)
MAPS["windfarm"] = WIND_MAP

#: The M3-2 isolated microgrid: model 1, the busbar, the grid-forming BESS, two repeating
#: sources (solar, wind) and the load. No model 123 and no export limit: an islanded site
#: has nothing to export to, so a curtailment control would be a control with no reader.
MICROGRID_MAP = _build_map(
    "microgrid",
    [Model(1, "common", (), length=COMMON_LEN),
     Model(64020, "grid", tuple(MICROGRID_GRID_ENTRIES)),
     Model(64021, "bess", tuple(MICROGRID_BESS_ENTRIES)),
     Model(64022, "source", tuple(MICROGRID_SOURCE_ENTRIES), repeats=2),
     Model(64023, "load", tuple(MICROGRID_LOAD_ENTRIES)),
     Model(64001, "vendor", tuple(_VENDOR_ENTRIES))],
    MICROGRID_SCALE_FACTORS,
    {"grid": "modbus.microgrid", "bess": "modbus.bess", "source": "modbus.source",
     "load": "modbus.microgridload", "vendor": "modbus.vendor"},
)
MAPS["microgrid"] = MICROGRID_MAP


def map_for(kind: str) -> RegisterMap:
    """The register map for a device kind. Fail loudly rather than silently defaulting."""
    try:
        return MAPS[kind]
    except KeyError:
        raise ValueError(f"unknown device kind {kind!r}; known: {', '.join(sorted(MAPS))}") from None


# --------------------------------------------------------------------------------------
# M1a-compatible module surface. The poller and the rural device were written against these
# names, and the meter path must not change under them (D20).
# --------------------------------------------------------------------------------------
POINTS: dict[str, Point] = METER_MAP.points
METER_POINTS: dict[str, Point] = {n: p for n, p in METER_MAP.points.items() if p.model == "meter"}
METER_POINT_OFFSETS = {name: p.offset for name, p in METER_POINTS.items()}
TOTAL_REGS = METER_MAP.total_regs
SCALE_FACTORS = METER_MAP.scale_factors  # kept for the M1a docstring/comment references


def base_image() -> list[int]:
    return METER_MAP.base_image()


def encode(
    values: dict[str, float],
    device_time: int,
    word_order: int = WORD_ORDER_LOW_FIRST,
    clock_offset_s: int = 0,
    image: list[int] | None = None,
) -> list[int]:
    return METER_MAP.encode(values, device_time, word_order, clock_offset_s, image)


def decode(img: list[int]) -> dict[str, Decoded]:
    return METER_MAP.decode(img)


# --------------------------------------------------------------------------------------
# Primitives, shared by every map.
# --------------------------------------------------------------------------------------
def _string_regs(value: str, nregs: int) -> list[int]:
    raw = value.encode("ascii")[: nregs * 2].ljust(nregs * 2, b"\x00")
    return [(raw[2 * i] << 8) | raw[2 * i + 1] for i in range(nregs)]


def _write_raw(img: list[int], point: Point, raw: int, word_order: int) -> None:
    if point.regs == 1:
        img[point.offset] = raw & 0xFFFF
        return
    raw &= 0xFFFFFFFF
    hi, lo = (raw >> 16) & 0xFFFF, raw & 0xFFFF
    if word_order == WORD_ORDER_LOW_FIRST:
        img[point.offset], img[point.offset + 1] = lo, hi
    else:
        img[point.offset], img[point.offset + 1] = hi, lo


def _decode_16(point: Point, img: list[int]) -> tuple[int | None, bool]:
    u = img[point.offset]
    if point.kind == "int16":
        if u == NOT_IMPLEMENTED_I16:
            return None, True
        return (u - 0x10000 if u & 0x8000 else u), False
    if u == 0xFFFF:
        return None, True
    return u, False


def _decode_32(point: Point, img: list[int], word_order: int) -> tuple[int | None, bool]:
    first, second = img[point.offset], img[point.offset + 1]
    hi, lo = (second, first) if word_order == WORD_ORDER_LOW_FIRST else (first, second)
    raw = (hi << 16) | lo
    if point.kind == "acc32" and raw == NOT_IMPLEMENTED_ACC32:
        return None, True
    if point.kind == "uint32" and raw == NOT_IMPLEMENTED_ACC32:
        return None, True
    return raw, False
