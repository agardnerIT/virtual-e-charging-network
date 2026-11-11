"""Operator control for the CSMS, run *inside* the CSMS container (M4, #12).

The CSMS publishes no host port for the same reason the devices do not: the site's OT zone is not
reachable from outside. So `make cap` execs in and talks to the control endpoint over loopback.

    python ocpp/csmsctl.py set-cap --amp 145        # cap the whole charge point at 145 A
    python ocpp/csmsctl.py set-cap --kw 100         # the same, in kW (3-phase, 230 V L-N)
    python ocpp/csmsctl.py clear-cap
    python ocpp/csmsctl.py status                   # connected charge points and their caps

`set-cap` pushes an OCPP 1.6 `SetChargingProfile` with `chargingProfilePurpose:
ChargePointMaxProfile` — a limit on the whole charge point, which is what a site capacity limit is.
The charge point shares it equally between the bays that are actually charging, and the effect is
visible in `energy.evse.current.offered` (with `energy.evse.throttled_by="load_management"`) and in
`energy.site.load.power` against `energy.site.capacity.current`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

BASE = f"http://127.0.0.1:{os.environ.get('HTTP_PORT', '9001')}"

#: Line-to-neutral nominal, so "100 kW" becomes "145 A". A 3-phase site at 230 V L-N.
V_NOMINAL = 230.0
PHASES = 3


def _request(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:  # bounded: a CP that hangs must not hang us
        return json.loads(r.read() or b"{}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="csmsctl", description="CSMS operator control")
    sub = p.add_subparsers(dest="command", required=True)

    cap = sub.add_parser("set-cap", help="push a ChargePointMaxProfile (a site capacity cap)")
    cap.add_argument("--amp", type=float, help="total current for the charge point, amperes")
    cap.add_argument("--kw", type=float, help="total power for the charge point, kW")
    sub.add_parser("clear-cap", help="clear the site cap")
    sub.add_parser("status", help="connected charge points and the caps they hold")

    args = p.parse_args(argv)
    try:
        if args.command == "set-cap":
            if args.amp is None and args.kw is None:
                print("csmsctl: set-cap needs --amp or --kw", file=sys.stderr)
                return 2
            limit_a = args.amp if args.amp is not None else (args.kw * 1000.0) / (PHASES * V_NOMINAL)
            out = _request("POST", "/profile", {"limit_a": round(limit_a, 1)})
        elif args.command == "clear-cap":
            out = _request("DELETE", "/profile")
        else:
            out = _request("GET", "/profile")
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"csmsctl: cannot reach the CSMS control endpoint at {BASE}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2))
    # A refused push is a failure of the command, not just a field in the output: a `make cap` that
    # silently did nothing is the worst possible outcome for a load-management demo.
    if out.get("chargePoints"):
        refused = {k: v for k, v in out["chargePoints"].items()
                   if isinstance(v, dict) and v.get("status") not in (None, "Accepted")}
        if refused:
            return 3
    if out.get("accepted") is False:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
