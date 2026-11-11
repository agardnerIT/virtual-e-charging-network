# The highway hub — six bays, more demand than supply

`highway-hume` is the site where the interesting question is unavoidable: **there are more bays than
power.** Six 22 kW bays on a 100 kW supply. A service centre on the Hume, and the first site here
where "what happens to the queue of vehicles" is a question with a telemetry answer.

Every other site in this repo is unconstrained. The depot has twelve bays and a connection that
happens to be enough; the rural site has two bays and a weak feeder. This one has a **capacity cap**
that binds, and a load-management decision that is visible as data.

```
    GRID ── CB1 ── TX1 ── BUSBAR 400 V ──┬── EVSE01 … EVSE06   (6 × 22 kW wanted)
                                          │
                              site cap 144.9 A (100 kW)  ← pushed by the CSMS as an OCPP profile
```

## How the cap is expressed — the protocol-real way

`SetChargingProfile` is OCPP 1.6's own mechanism for this, and `ChargePointMaxProfile` is the profile
purpose that means *a limit on the whole charge point* (as opposed to one connector):

```
CSMS  →  CP    SetChargingProfile
               connectorId: 0
               csChargingProfiles:
                 chargingProfileId: 1
                 chargingProfilePurpose: ChargePointMaxProfile
                 chargingProfileKind: Absolute
                 chargingSchedule:
                   chargingRateUnit: A
                   chargingSchedulePeriod: [{startPeriod: 0, limit: 144.9}]
CP    →  CSMS  { "status": "Accepted" }
```

The charge point shares the cap **equally** between the bays that are actually charging, and a share
is a cap, never a lift: a bay that wants less than its share keeps wanting less.

```bash
make cap KW=100      # 100 kW site-wide = 144.9 A at 230 V L-N, 3-phase
make cap AMP=90      # ... or in amperes
make cap-clear       # back to no profile
make cap-status      # who is connected, and what cap they hold
```

`make cap` execs into the CSMS container (the OT zone publishes no host ports) and pushes the profile
over the live WebSocket. If a charge point reconnects, the CSMS **re-pushes** it on `BootNotification`
— OCPP has no "read my profile" call, which is why a CSMS that forgets is a CSMS that loses caps.

## The measured before/after

Six bays charging, one cap pushed, both halves of the event recorded from the CP's own OCPP state
(`ocpp/cpctl.py status`) and from the store:

| | six bays, no cap | six bays, 100 kW cap |
|---|---|---|
| offered current per bay | 32.0 A | **24.2 A** (= 144.9 / 6) |
| delivered per bay | 21.6 kW | **16.4 kW** |
| site load | 129.6 kW | **98.3 kW** (`energy_site_load_power_watts`) |
| who is throttled, and by what | nobody | all six, `energy_evse_throttled_by="load_management"` |
| cap as a metric | absent | `energy_site_capacity_current_amperes` = 144.9 |
| after `make cap-clear` | — | back to **32.0 A / 21.6 kW** per bay |

The last column is the one that makes it a *control* rather than a setting: the cap binds, the site
sits 1.7 kW under it, and clearing it restores the bays.

## Why the "throttled by" attribute is the useful part

`energy.evse.current.offered` already existed — it falls during a voltage sag. On this site it can
also fall **because of the cap**, and those are different problems with different remedies (one is
the grid, one is the site's own policy). So the data point carries
`energy.evse.throttled_by = voltage | load_management`, a bounded two-value vocabulary, and an
operator can tell "the supply is sagging" from "we are managing the queue" without reading a runbook.
That is the same argument as `energy.bess.hold_reason` on the island: *why* a number is what it is.

## What is not implemented, and is therefore not claimed

`docs/standards.md` §6 lists this in full. The short version: `chargingRateUnit: A` with a single
schedule period; no `W` units, no multi-period schedules, no `Recurring`/`Relative` kinds, no stack
levels beyond "last one wins", no `TxProfile`/`TxDefaultProfile` per-connector limits, and no
priority between bays. An equal share is what a simple load-management layer does; a real one would
let an operator say "bays 1-2 first", and that is a scheduling problem, not a telemetry one.

There is also no **power-sharing cabinet** model: the six bays are one charge point, and the cap is
enforced by the charge point rather than by hardware that would do it in the field.
