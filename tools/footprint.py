#!/usr/bin/env python3
"""Print the running stack's footprint as the markdown table docs/sizing.md is written in.

    docker stats --no-stream --format '{{.Name}}|{{.MemUsage}}' $(docker compose ... ps -q) | tools/footprint.py

`make footprint` does exactly that. It exists because "re-run make stats and update the number"
otherwise means transcribing 30 lines by hand, and a table that was transcribed is a table that
will be wrong. This is the number D10 gates on (`docker stats`), and it is **not** VmRSS — see
docs/findings.md #35 for why both are recorded and which one each figure in sizing.md came from.
"""
import sys

rows: list[tuple[str, float]] = []
for line in sys.stdin:
    line = line.strip()
    if not line or "|" not in line:
        continue
    name, mem = line.split("|", 1)
    size = mem.split(" / ")[0].strip()
    if size.endswith("GiB"):
        mib = float(size[:-3]) * 1024
    elif size.endswith("MiB"):
        mib = float(size[:-3])
    elif size.endswith("KiB"):
        mib = float(size[:-3]) / 1024
    else:
        continue
    short = name.replace("otel-ot-", "").rsplit("-1", 1)[0]
    rows.append((short, mib))

if not rows:
    sys.exit("footprint: no containers matched — is the stack up?")

total = sum(v for _, v in rows)
for name, mib in sorted(rows):
    print(f"| `{name}` | {mib:.1f} MiB |")
print(f"| **Total** ({len(rows)} containers) | **{total:.0f} MiB ({total / 1024:.2f} GiB)** |")
