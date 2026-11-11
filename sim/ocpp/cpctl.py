"""Operator control for the OCPP charge point, run *inside* the container.

The charge point publishes no host port, for the same reason the Modbus devices do not: the
rural OT zone is not reachable from outside (plan.md §5.4). So `make session` / `make spine`
exec into the container and talk to the control endpoint over loopback, exactly as
`make fault` does for the device.

    python ocpp/cpctl.py status [--evse EVSE07]
    python ocpp/cpctl.py start  [--evse EVSE02]
    python ocpp/cpctl.py stop   [--evse EVSE02]
    python ocpp/cpctl.py watch  [--evse EVSE02] [--seconds 120]   # poll until the session ends

`status` prints one row per bay, which is what makes a twelve-bay depot readable; pass
`--evse` to look at one. Exit code 3 means "no such bay here", so a caller sweeping several
charge points can tell "wrong site" from "something is broken".
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


def _cap_line(health: dict) -> str:
    """One line about load management, printed above the bays (M4, #12)."""
    prof = (health or {}).get("chargingProfile") or {}
    cap = prof.get("site_cap_a")
    if cap is None:
        return "cap     none (no ChargePointMaxProfile; bays offer their own maximum)"
    return (f"cap     {cap:.1f} A site-wide ({prof.get('site_cap_kw')} kW), shared equally between "
            f"bay(s) that are charging")


def _default_evse() -> str:
    """The first bay this charge point starts on its own (CP_AUTO_EVSE may name several)."""
    return os.environ.get("CP_AUTO_EVSE", "EVSE01").split(",")[0].strip() or "EVSE01"


def _get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=5) as r:  # bounded, always
        return json.load(r)


def _post(path: str, evse: str) -> dict:
    query = urllib.parse.urlencode({"evse": evse})
    req = urllib.request.Request(f"{BASE}{path}?{query}", method="POST", data=b"{}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def _supply(h: dict) -> str:
    s = h["supply"]
    return (f"supply {s['voltageV'][0]:.1f}/{s['voltageV'][1]:.1f}/{s['voltageV'][2]:.1f} V "
            f"({s['pu']:.2f} pu, {s['quality']})")


def _row(e: dict) -> str:
    tx = e["transactionId"] if e["transactionId"] is not None else "-"
    return (f"{e['evseId']:<8}{e['state']:<13}{e['powerW'] / 1000:>6.1f} "
            f"{e['offeredA']:>10.1f} {str(tx):>5} {e['sessionKWh']:>13.3f}")


def _table(h: dict, only: str | None = None) -> str:
    """One line per bay, because twelve bays on one line is not a status, it is a wall."""
    lines = [_supply(h)]
    evses = h["evses"]
    if only:
        evses = [e for e in evses if e["evseId"].lower() == only.lower()]
        if not evses:
            return f"no {only} at {h['chargePointId']}"
    lines.append(f"{'bay':<8}{'state':<13}{'kW':>6} {'offered A':>10} {'tx':>5} "
                 f"{'session kWh':>13}")
    lines.extend(_row(e) for e in evses)
    return "\n".join(lines)


def _line(h: dict, evse_id: str) -> str:
    evse = next(e for e in h["evses"] if e["evseId"] == evse_id)
    return f"{_supply(h)} | {_row(evse)}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("command", choices=("status", "start", "stop", "watch"))
    ap.add_argument("--evse", default=None)
    ap.add_argument("--seconds", type=float, default=120.0)
    args = ap.parse_args(argv)

    try:
        if args.command == "status":
            h = _get("/health")
            # Exit 3 means "this charge point has no such bay", which is a normal answer when a
            # caller sweeps every site and its bays are named differently. `make session-status`
            # relies on that distinction.
            if args.evse and not any(e["evseId"].lower() == args.evse.lower()
                                     for e in h["evses"]):
                print(f"no {args.evse} at {h['chargePointId']}", file=sys.stderr)
                return 3
            print(_cap_line(h))
            print(_table(h, args.evse))
            return 0
        if args.command == "start":
            print(json.dumps(_post("/session/start", args.evse or _default_evse())))
            return 0
        if args.command == "stop":
            print(json.dumps(_post("/session/stop", args.evse or _default_evse())))
            return 0

        # watch: poll until the named EVSE leaves Charging, or the timeout expires.
        evse_id = args.evse or _default_evse()
        started = time.time()
        saw_charging = False
        while time.time() - started < args.seconds:
            h = _get("/health")
            print(_line(h, evse_id), flush=True)
            evse = next((e for e in h["evses"] if e["evseId"] == evse_id), None)
            if evse is None:
                print(f"no {evse_id} at {h['chargePointId']}", file=sys.stderr)
                return 3
            if evse["state"] == "Charging":
                saw_charging = True
            elif saw_charging:
                print(f"{evse_id} left Charging - session ended", flush=True)
                return 0
            time.sleep(2)
        return 0
    except urllib.error.URLError as exc:
        print(f"charge point control unreachable at {BASE}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
