# Contributing and conventions

This repository is a teaching artefact: it is meant to be **run**, read and argued with. These are
the rules that keep it coherent. They are short on purpose.

## Hard constraints (do not violate)

| Constraint | Decision | Meaning |
|---|---|---|
| **OTel-native, no exceptions** | D15 | No ActiveGate, no OneAgent, no vendor agent. The collectors *are* the agents. Every backend is reached over OTLP only. |
| **Two collector layers, and only two** | D16 | One edge collector per site, one hub. Do not add a tier. |
| **arm64 only** | D10 | Must run on Apple Silicon. Verify any new image actually runs arm64. |
| **≤3 GB RAM total** | D10 | Currently ~830 MiB. Measure, then update `docs/sizing.md`. |
| **Docker Compose only** | D13 | No Kubernetes. |
| **Clean energy only** | D17 | No diesel, no thermal, no nuclear generation. |
| **One southbound protocol** | D4 | Modbus (SunSpec-shaped). Do not add DNP3, IEC 104, 61850, DLMS or OPC UA. |

`plan.md` is the source of truth for decisions and is a living document: superseded decisions are
struck through with the replacement noted, never deleted. Add new ones as `D37`, `D38`, …

## The split of responsibility

- **The poller reads and scales; the edge collector gives meaning** (D20). Device-shaped names and
  unit `1` leave the poller; names, units, sign, A/B/C → L1/L2/L3, quality and dual timestamps are
  the edge collector's job. Do not move semantics into the poller.
- **Quality is `otel.data_point.quality`** (D21), authored by the edge collector. Applications set
  `energy.quality` and get the same conversion (D35).
- **Deadband lives in the poller** (D22); heartbeat 60 s.
- **One fault only: voltage sag** (D29). Resist building a fault framework.
- **One image, many roles** (`sim/Dockerfile`): the shared modules (`sunspec.py`, `ocpp16.py`) are
  what stop an encoder and a decoder from drifting.

## Code conventions

- Keep the simulators as small as they can be while staying recognisable to an electrical engineer.
  Depth buys credibility; breadth is opt-in (D4).
- Prefer `make` targets over raw `docker compose` commands for anything a reader will want to repeat.
- Anything in the OT zone publishes **no host port**. Control is by `docker compose exec` into the
  container (`make fault`, `make curtail`, `make session`), which also keeps the zone unreachable.
- Every attribute that rides on a metric must be **stable for the life of a series** or bounded to a
  few values. Payload belongs on spans and logs (D25, D36).

## Before you call something done

1. `make up` ends in a picture at <http://127.0.0.1:8091>.
2. `make queues` → three edges at 0.
3. The M1/M2 regressions still pass: `make fault SITE=rural FAULT=voltage-sag`, the deadband still
   suppresses steady registers, stopping `device-rural` turns quality to `comms-lost`, and
   `make spine` still ends in a `PowerLoss` transaction.
4. `make stats` → update `docs/sizing.md` if the footprint moved, and say which measurement it is
   (`docker stats` vs `VmRSS`).
5. `docs/standards.md` states fidelity per device and per protocol. Update it if you added either.
6. If you learned something the hard way, add it to `docs/findings.md`.

## Where things live

```
plan.md              decisions log + roadmap (source of truth)
NEXT.md              the current milestone, and where the work items are tracked
docs/findings.md     the hard-won findings, by area
docs/standards.md    which spec each device/protocol claims, and how faithfully
docs/sizing.md       measured RAM/disk, image audit, visual-stack bake-off
docs/m2-build-log.md the M2 engineering log: what was built, what broke, what was measured
```
