"""Operator control for the OCPP charge point, run *inside* the container.

The charge point publishes no host port, for the same reason the Modbus devices do not: the
rural OT zone is not reachable from outside (plan.md §5.4). So `make session` / `make spine`
exec into the container and talk to the control endpoint over loopback, exactly as
`make fault` does for the device.

    python ocpp/cpctl.py status
    python ocpp/cpctl.py start  [--evse EVSE02]
    python ocpp/cpctl.py stop   [--evse EVSE02]
    python ocpp/cpctl.py watch  [--seconds 120]   # poll until the session ends
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = f"http://127.0.0.1:{os.environ.get('HTTP_PORT', '8080')}"


def _get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=5) as r:  # bounded, always
        return json.load(r)


def _post(path: str, evse: str) -> dict:
    query = urllib.parse.urlencode({"evse": evse})
    req = urllib.request.Request(f"{BASE}{path}?{query}", method="POST", data=b"{}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def _line(h: dict) -> str:
    s = h["supply"]
    bits = [f"supply {s['voltageV'][0]:.1f}/{s['voltageV'][1]:.1f}/{s['voltageV'][2]:.1f} V "
            f"({s['pu']:.2f} pu, {s['quality']})"]
    for e in h["evses"]:
        tx = e["transactionId"] if e["transactionId"] is not None else "-"
        bits.append(f"{e['evseId']} {e['state']:<13} {e['powerW']/1000:5.1f} kW "
                    f"offered {e['offeredA']:4.1f} A  tx {tx}  "
                    f"{e['sessionKWh']:.2f} kWh")
    return " | ".join(bits)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("command", choices=("status", "start", "stop", "watch"))
    ap.add_argument("--evse", default=os.environ.get("CP_AUTO_EVSE", "EVSE01"))
    ap.add_argument("--seconds", type=float, default=120.0)
    args = ap.parse_args(argv)

    try:
        if args.command == "status":
            print(_line(_get("/health")))
            return 0
        if args.command == "start":
            print(json.dumps(_post("/session/start", args.evse)))
            return 0
        if args.command == "stop":
            print(json.dumps(_post("/session/stop", args.evse)))
            return 0

        # watch: poll until the named EVSE leaves Charging, or the timeout expires.
        started = time.time()
        saw_charging = False
        while time.time() - started < args.seconds:
            h = _get("/health")
            print(_line(h), flush=True)
            evse = next(e for e in h["evses"] if e["evseId"] == args.evse)
            if evse["state"] == "Charging":
                saw_charging = True
            elif saw_charging:
                print(f"{args.evse} left Charging - session ended", flush=True)
                return 0
            time.sleep(2)
        return 0
    except urllib.error.URLError as exc:
        print(f"charge point control unreachable at {BASE}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
