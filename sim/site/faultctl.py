"""Tiny client for the site simulator's fault control endpoint.

Run *inside* the device container (``make fault`` does this with ``docker compose exec``), so
the OT zone stays unreachable from the host and nothing new is published:

    python faultctl.py voltage-sag
    python faultctl.py voltage-sag --depth 0.6 --duration 30
    python faultctl.py loadstep --kw 300 --duration 60     # microgrid only
    python faultctl.py clock-skew --seconds 240           # push the device clock out (M3-4)
    python faultctl.py clear
    python faultctl.py status
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def _base() -> str:
    host = os.environ.get("FAULTCTL_HOST", "127.0.0.1")
    port = os.environ.get("HTTP_PORT", "8080")
    return f"http://{host}:{port}"


def _request(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        _base() + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read() or b"{}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="faultctl", description="site simulator fault control")
    sub = parser.add_subparsers(dest="command", required=True)

    sag = sub.add_parser("voltage-sag", help="depress per-phase voltage for a bounded period")
    sag.add_argument("--depth", type=float, default=0.78,
                     help="per-unit multiplier, e.g. 0.78 = a 78%% (0.78 pu) sag")
    sag.add_argument("--duration", type=float, default=20.0, help="seconds")
    sub.add_parser("clear", help="clear any active fault")
    sub.add_parser("status", help="show device health and any active fault or export limit")

    curt = sub.add_parser("curtail", help="hold a generation site's export below nameplate")
    curt.add_argument("--pct", type=float, default=60.0, help="export limit as %% of nameplate")
    curt.add_argument("--duration", type=float, default=None,
                      help="seconds (default: until cleared)")
    sub.add_parser("uncurtail", help="clear the export limit")

    step = sub.add_parser("loadstep",
                          help="microgrid only: switch a block of EV load on (not a fault)")
    step.add_argument("--kw", type=float, default=300.0, help="load step in kW")
    step.add_argument("--duration", type=float, default=None,
                      help="seconds (default: until cleared)")
    sub.add_parser("loadstep-clear", help="remove the load step")

    clk = sub.add_parser("clock-skew",
                         help="push the device's own clock out by N seconds (negative = behind)")
    clk.add_argument("--seconds", type=float, default=240.0)
    sub.add_parser("clock-reset", help="return the device clock to its configured skew")

    args = parser.parse_args(argv)
    try:
        if args.command == "voltage-sag":
            out = _request("POST", "/fault/voltage-sag",
                           {"depth": args.depth, "duration_s": args.duration})
        elif args.command == "curtail":
            out = _request("POST", "/curtail", {"pct": args.pct, "duration_s": args.duration})
        elif args.command == "loadstep":
            out = _request("POST", "/load/step", {"kw": args.kw, "duration_s": args.duration})
        elif args.command == "loadstep-clear":
            out = _request("DELETE", "/load/step")
        elif args.command == "clock-skew":
            out = _request("POST", "/clock/skew", {"seconds": args.seconds})
        elif args.command == "clock-reset":
            out = _request("DELETE", "/clock/skew")
        elif args.command == "uncurtail":
            out = _request("DELETE", "/curtail")
        elif args.command == "clear":
            out = _request("DELETE", "/fault/voltage-sag")
        else:
            # /health is the one endpoint every device has, and it already carries the
            # device's own fault or export-limit state.
            out = _request("GET", "/health")
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"faultctl: cannot reach device control endpoint at {_base()}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
