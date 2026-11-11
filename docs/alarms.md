# Alarms and SLOs — and why there is no alarm engine

Two questions, two answers, one rule: **both are derived from the telemetry, not from a separate
piece of software with its own opinion.**

- The **SLOs** are four PromQL expressions, printed by `make slo`, over session-level metrics the
  charge point and CSMS already emit.
- The **alarm list** is a set of predicates over the points the HMI is already showing. There is no
  alarm store, no alarm service and no rule engine — which means the list cannot drift from the data
  it is drawn beside.

## The SLOs (`make slo`)

| SLO | Expression | Target | Why that target |
|---|---|---|---|
| Session success rate | `ocpp_transactions_stopped{reason="Local"} / all stopped` | ≥ 0.95 | `Local` is a normal end (driver, schedule); `PowerLoss` is the supply deciding. A fleet that fails 5 % of sessions to the grid is already in trouble. |
| Time to first power, p95 | `histogram_quantile(0.95, energy_session_time_to_first_power_seconds_bucket)` | ≤ 30 s | The operator-visible failure is a bay that says `Charging` and delivers nothing. 30 s is generous for OCPP 1.6 (authorize + start is milliseconds in this model); a p95 above it means something is being retried. |
| Delivered / requested energy | `energy_session_energy_delivered / .requested` | ≥ 0.90 | Requested is the *vehicle's* ask; delivered is what flowed. A derate, a sag or a cap shows up here and nowhere else. 0.90 tolerates normal taper; below that the site is systematically under-delivering. |
| Power-quality events per 100 sessions | `energy_session_power_quality_affected / sessions stopped × 100` | ≤ 5 | How often the grid interrupted charging. This is the number a network operator asks for after a bad week, and it is not derivable from any single session. |

**The targets are this repo's, not an industry standard**, and `tools/slo.sh` says so on every run.
They are set where the demo passes on a quiet day and fails under `make spine`.

**They are lifetime ratios, not windowed ones**, because the CSMS and charge point keep their
counters in memory (D34): `increase(counter[1h])` on a counter that has incremented once returns 0,
since Prometheus cannot see the step before the first sample it scraped. A windowed SLO board that
says "n/a" for a healthy stack is worse than no board. When the counters are durable, the windowed
form is one function away.

## The alarm list

ISA-18.2 is mostly about **rationalisation** — deciding what deserves an alarm and what does not —
and about **suppression**, because an alarm flood is how operators learn to ignore alarms. What this
repo implements is the shape, derived from data:

| Condition | Priority | Cause | Operator action | Suppression |
|---|---|---|---|---|
| Supply voltage below 0.94 pu | High | Feeder sag, upstream fault, or a heavy load switching on | Check whether a bay dropped; expect derate; do not restart into the sag | **One alarm per site**, with an affected-bay count. Twelve bays do not make twelve alarms. |
| Bay charging but delivering 0 kW | Medium | Vehicle pause, connector fault, or the EVSE has derated to zero | Check the bay, then the vehicle | One row per bay (it is a per-bay fault, and it is the point of the alarm) |
| Grid former at a limit (hold) | Medium | Battery at SoC floor/ceiling or at its power limit | Nothing immediately: the battery is doing its job. Watch frequency | One row per site |
| Frequency outside 49.85–50.15 Hz | Medium | Islanded site: load/generation imbalance at the limit of the grid former | Shed load or curtail; check `energy.bess.hold_reason` for *why* | One row per site |
| Device not answering (`comms-lost`) | High | Modbus/OCPP link to the device, not the WAN | Look at the device, not the network | One row per site with a point count |
| Site at its capacity cap | Medium | The site is at (within 2 % of) its OCPP `ChargePointMaxProfile`, so bays are held below what the vehicles asked for | None operationally — this is the site working as designed. Log it, and check `energy.evse.throttled_by` before blaming the grid | One row per site, with the number of bays sharing the cap |
| Generation curtailed | Low | Operator export limit, or a connection cap | Commercial, not operational | One row per site |

**First-out** is the oldest sample that satisfies the condition, taken from the store's receive time
— the only clock comparable across entities (D42). After a sag it tells you the voltage moved before
the bays did, which is the whole point of a sequence-of-events list.

**Acknowledge is per browser session** (`localStorage`) and labelled as such in the panel. A real ack
state needs a state store, and this page deliberately has none; claiming one would be the single
dishonest thing in a list whose value is that it is derived from data.

### Demonstrated deduplication

- **Rural site, one sag, one bay:** one High alarm, "1 bay(s) charging through it", even though three
  phase samples were below the threshold.
- **Depot, one sag, twelve bays:** one High alarm, "**12 bay(s) charging through it**", one first-out
  — not twelve alarms. This is the case the issue was written for, and it is the case a naive
  per-point alarm rule gets spectacularly wrong.

## What is deliberately not here

- **No notification pipeline** (Alertmanager, paging, email). The shape of the alarm is the subject;
  who gets woken up is not.
- **No alarm history.** A cleared alarm disappears. History is what a log store or a real alarm
  system is for, and the hub's logs already carry the underlying events.
- **No priority arithmetic.** Priorities are chosen in the table above and justified there, not
  computed from a formula nobody can audit.
- **No hysteresis.** A sag that hovers at 0.94 pu will flicker the alarm. Real systems add deadbands;
  this one states the omission instead of hiding it behind a smoothed number.
