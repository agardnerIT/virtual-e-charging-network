#!/usr/bin/env python3
"""Two-way check between the telemetry this stack emits and the published registry (issue #9).

    tools/registry-coverage.py

A registry that drifts from the code is worse than no registry: it is a document that looks
authoritative and is wrong. So this checks **both** directions:

  * a metric the edge collector or an application emits and the registry does not describe
    → the registry is incomplete (the case the issue explicitly asks to fail the build on);
  * a metric the registry describes and nothing emits → the registry is claiming something
    imaginary, which this repo is not allowed to do (D4).

Sources of truth for "what we emit":
  collector/edge/site.yaml     the edge collector's OTLP rename statements (the semantic names)
  sim/ocpp/cp.py, csms.py      the application instruments (they emit semantic names directly)
  sim/market/feed.py           the market feed, same rule
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EDGE = ROOT / "collector/edge/site.yaml"
REGISTRY = ROOT / "schema/registry/energy.yaml"
APP_FILES = [ROOT / "sim/ocpp/cp.py", ROOT / "sim/ocpp/csms.py", ROOT / "sim/market/feed.py"]

# Names that are collected but deliberately outside this registry's scope, with the reason. An
# entry here is a decision, not a suppression: the point of the list is that adding to it is a
# visible edit in a diff.
OUT_OF_SCOPE = {
    # Modbus-side device shapes, before the edge renames them. They are the *input* to the
    # semantics the registry describes, and the poller's naming is device-specific by design
    # (D20): a registry of `modbus.register.vpha` would be a registry of one vendor's register
    # map, which is the opposite of what a semantic convention is.
    "modbus.",
}


def emitted_from_edge() -> set[str]:
    text = EDGE.read_text()
    return set(re.findall(r'- set\(metric\.name, "([a-z][a-z0-9_.]+)"\)', text))


def emitted_from_apps() -> set[str]:
    names: set[str] = set()
    for f in APP_FILES:
        text = f.read_text()
        # Meter/instrument creation: the first string argument to a create_* call.
        names |= set(re.findall(
            r'create_(?:observable_gauge|counter|histogram|up_down_counter)\(\s*"([a-z][a-z0-9_.]+)"',
            text))
    return names


def registered() -> set[str]:
    text = REGISTRY.read_text()
    return set(re.findall(r"^\s+metric_name: ([a-z][a-z0-9_.]+)$", text, re.M))


def main() -> int:
    emitted = emitted_from_edge() | emitted_from_apps()
    emitted = {n for n in emitted if not any(n.startswith(p) for p in OUT_OF_SCOPE)}
    reg = registered()

    missing = sorted(emitted - reg)
    imaginary = sorted(reg - emitted)

    print(f"emitted: {len(emitted)}   registered: {len(reg)}   "
          f"out of scope: {', '.join(OUT_OF_SCOPE)}")

    if missing:
        print(f"\nFAIL: {len(missing)} metric(s) emitted but not in the registry:")
        for n in missing:
            print(f"  - {n}")
        print("\nAdd them to schema/registry/energy.yaml, or the registry is a lie by omission.")
    if imaginary:
        print(f"\nFAIL: {len(imaginary)} metric(s) in the registry that nothing emits:")
        for n in imaginary:
            print(f"  - {n}")
        print("\nRemove them, or make them real. A registry must not describe an imaginary device (D4).")

    if missing or imaginary:
        return 1
    print("\nOK: the registry and the emitters agree, in both directions.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
