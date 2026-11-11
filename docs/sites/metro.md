# The metro depot — twelve bays, twelve concurrent sessions

The fourth site is where the demo stops being a demo. Same images, same poller, same edge collector
config, same charge point code — the difference is scale (12 connectors instead of 2) and environment
(a 12 ms fibre backhaul instead of a 4G link):

```
   [SunSpec/Modbus meter]  <-poll--  [charge point, 12 connectors]  --OCPP 1.6J/WS-->  [CSMS double]
        device-metro              ocpp-cp-metro                                   csms-metro
                                            |
                                            +--OTLP/HTTP-->  [edge-metro] --12 ms WAN-->  hub
```

Every bay runs a session, started 15 s apart, so twelve transactions are live within three minutes of
`make up` and their ends are staggered too.

## Why twelve bays on one charge point

OCPP 1.6 is per-connector, so twelve outlets on one power cabinet is legal and is what a real depot
looks like (plan.md D37). Keeping them in one process also keeps the concurrency visible: twelve
simultaneous transactions share one WebSocket and one `uniqueId` counter, so a crossed transaction id
or a shared session context would show up in-process instead of being hidden by the network.

What it is *not*: twelve charge points, load sharing between bays, or a central CSMS. All three are
stated in [`docs/standards.md`](../standards.md) §5, together with the fidelity trade they represent.

## The two things only concurrency and cardinality can break

**One session is still one trace.** Checked while twelve were running: every trace carries exactly one
`energy.evse.id`, one `ocpp.transaction.id`, one `energy.session.id` and one connector, every
`MeterValues` child carries its own transaction, and the CSMS's connector→transaction map agrees with
Jaeger's 12-for-12. No 600 s session's spans appear in a sibling's trace.

The verification has a wrinkle worth knowing: a session root span only lands when the transaction ends
(`docs/findings.md` #29), so a mid-session trace has children and no root. The check that worked was
one `ocpp.transaction.id` per trace, corroborated by the CSMS's own connector→transaction map as an
independent authority — both agreed 12/12 before any root had arrived (#44).

**Series scale with bays, not with sessions.** Six series per bay (power, offered current, state, and
one voltage per phase) plus one `energy.session.energy` series per live transaction. Measured before
the depot (2 bays) and after (12), by PromQL count, not by memory:

| | before (3 sites, 2 bays) | after (4 sites, 12 bays) |
|---|---|---|
| `energy_evse.*` series, all sites | 13 | 87 |
| — of which the depot | — | 74 |
| `energy.session.energy` series | 2 | 15 |
| `prometheus_tsdb_head_series` | 897 | 1 367 |

Twelve bays cost 72 + 12 = 84 series. That is what a bay costs, no matter how many sessions run
through it: the idTag, vehicle id, session id and transaction id ride on spans and logs, not on the
gauges (plan.md D36). The one exception, `energy.session.energy`, carries `ocpp.transaction.id`
because a per-transaction counter is the thing you want to query. The full method and the two series
details that explain the arithmetic are in [`docs/sizing.md`](../sizing.md) §"Twelve bays".

## The depot's meter is not the sum of its bays

The depot's `M1` reads about 13 kW while its twelve bays draw ~260 kW. That is deliberate and it is a
gap, not a measurement: **there is no power-flow model in the stack** (`docs/standards.md` §6, §7,
`docs/findings.md` #42). The site meter simulates a building load of its own; the charge points
simulate what an EVSE offers. Read the charge points as the billing data and M1 as the site's supply
meter for its own load, never as a balance.

## Commands

```bash
make session-status SITE=metro          # twelve rows: state, kW, offered A, tx, session kWh
make session-status SITE=metro EVSE=EVSE07
make prom Q='count by (__name__) ({__name__=~"energy_evse.*",energy_site_id="metro-tottenham"})'
make link SITE=metro PROFILE=down       # the depot's own link: 12 ms ± 4 ms when wet
make session SITE=metro EVSE=EVSE07     # start a fresh transaction on one bay
```

## The mimic

Same drawing as the rural site with a different number of bays: the sites reported in the pipeline
decide how many cells there are, and at the depot the twelve bays are a table on an LV board rather
than twelve boxes on a busbar. See [`docs/hmi.md`](../hmi.md).
