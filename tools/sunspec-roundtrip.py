#!/usr/bin/env python3
"""Encode -> decode round trip for every register map, asserting the byte layout (#23).

    tools/sunspec-roundtrip.py

The device writes with `sunspec.RegisterMap.encode` and the poller reads with `.decode`. Every
fidelity claim in docs/standards.md rests on those two agreeing, so this asserts it per map, per
point: a value with a distinctive scale goes in, the same value (within one count of the device's
own scale factor) comes out.

It also asserts the things that are easy to get silently wrong:

  * the word-order switch - a 32-bit accumulator is read as `(reg0<<16)|reg1`, so a decoder that
    ignores the vendor's order gets a plausible number that is wrong by 65536x;
  * the not-implemented sentinel - a point marked absent must decode as absent, not as 32768;
  * the model chain - every model the map declares must appear in the header, so a new model
    (this is how model 129 was added) cannot be silently misplaced.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sim"))

import sunspec  # noqa: E402

FAILED = 0


def check(desc: str, ok: bool, detail: str = "") -> None:
    global FAILED
    if ok:
        print(f"  ok   {desc}")
    else:
        FAILED += 1
        print(f"  FAIL {desc} {detail}")


def roundtrip(kind: str) -> None:
    m = sunspec.map_for(kind)
    print(f"\n{kind}: {m.total_regs} registers, models "
          f"{', '.join(str(x.id) for x in m.models)}")

    # One distinctive engineering value per point, well inside the type's range.
    values: dict[str, float] = {}
    for name, p in m.points.items():
        if p.group is None or p.sf is None:
            continue
        sf = m.scale_factors[p.sf]
        target = 1234 * (10.0 ** sf) if p.kind != "acc32" else 12345.6 * (10.0 ** sf)
        values[name] = target

    img = m.encode(values, device_time=1_700_000_000, word_order=sunspec.WORD_ORDER_LOW_FIRST)
    dec = m.decode(img)

    worst = 0.0
    bad: list[str] = []
    for name, want in values.items():
        got = dec[name].value
        if got is None:
            bad.append(f"{name} decoded as absent")
            continue
        step = 10.0 ** m.scale_factors[m.points[name].sf]
        err = abs(got - want) / max(step, 1e-9)
        worst = max(worst, err)
        if err > 1.01:            # one count of the device's own resolution
            bad.append(f"{name}: {want} -> {got} ({err:.1f} counts)")
    check(f"every point survives encode/decode ({len(values)} points, worst {worst:.2f} counts)",
          not bad, "; ".join(bad[:4]))

    # ... and with the other word order, which must give the same values.
    img2 = m.encode(values, device_time=1_700_000_000, word_order=sunspec.WORD_ORDER_HIGH_FIRST)
    dec2 = m.decode(img2)
    mismatch = [n for n, w in values.items()
                if dec2[n].value is None or abs(dec2[n].value - w) > 1.01 * 10.0 ** m.scale_factors[m.points[n].sf]]
    check("the same values survive the vendor's other 32-bit word order", not mismatch,
          ", ".join(mismatch[:3]))

    # A 32-bit accumulator must not be off by the classic 65536x.
    acc = next((n for n, p in m.points.items() if p.kind == "acc32" and p.group), None)
    if acc:
        got = dec[acc].value
        want = values[acc]
        check(f"accumulator {acc} is not word-order-swapped ({want:.0f} -> {got:.0f})",
              abs(got - want) <= 10.0 ** m.scale_factors[m.points[acc].sf])

    # The sentinel: a not-implemented point decodes as absent, never as a number.
    armed = list(img)
    victim = next(n for n, p in m.points.items()
                  if p.kind == "int16" and p.group and p.regs == 1)
    armed[m.points[victim].offset] = sunspec.NOT_IMPLEMENTED_I16
    check(f"the not-implemented sentinel decodes as absent ({victim})",
          m.decode(armed)[victim].not_implemented)

    # The model chain is where it says it is.
    header_ok = all(img[m.header_offsets[model.name]] == model.id for model in m.models)
    check("every declared model has its header in the image", header_ok)


for kind in ("meter", "inverter", "windfarm", "microgrid"):
    roundtrip(kind)

print()
if FAILED:
    print(f"FAILED: {FAILED} check(s)")
    sys.exit(1)
print("OK: every map round-trips, both word orders, and the sentinels behave.")
