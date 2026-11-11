# The rural site — a real device path

The rural site is the load site: a weak 4G-fed feeder with a SunSpec/Modbus meter and two EVSEs on
one OCPP 1.6J charge point. It is the site M1a was built around and the site the voltage sag is
injected into.

```
   [SunSpec/Modbus meter]  <-poll->  [poller]  --OTLP/HTTP-->  [edge collector]  --WAN-->  [hub]
     device-rural                    poller-rural               edge-rural              hub
```

Nothing here is `telemetrygen`. A Modbus TCP device holds a SunSpec-shaped register file — integers
with separate scale-factor registers, a 32-bit energy accumulator across two registers, a device
clock — and the poller reads it and forwards OTLP. The edge collector then does the part that makes
OT telemetry different, and it is the reason an edge collector exists at all.

## What the edge collector changes, and why each one exists

- **Names and units.** `modbus.register.vpha`, unit `1`, becomes `energy.meter.voltage`, unit `V`.
  The poller stays device-shaped; meaning is added once, at the edge (plan.md D20).
- **Scale.** `modbus.register.raw=2314` with `scale_factor=-1` becomes `231.4 V`. The raw value
  stays on the data point, so the transformation is auditable. One box, one scale factor, one
  decimal place — and every device in the repo does it the same way.
- **Phase naming.** The device says `A/B/C`; IEC practice says `L1/L2/L3`. Translated at the edge,
  because the device's own register names are what a maintenance electrician will read off a
  terminal strip.
- **Quality.** Every data point carries `otel.data_point.quality` — `good`, `stale`, `bad` or
  `comms-lost`. Modbus has no such concept, and neither does OTel; this is the repo's strongest
  contribution candidate (plan.md D12, D21). Stop the device (`docker compose stop device-rural`) and
  watch retained values go `comms-lost`.
- **Two timestamps.** `energy.device.timestamp` is when the device says the sample happened;
  `energy.receive.timestamp` is when the collector observed it. They differ by design — remote sites
  in this network have no reliable NTP (plan.md D12, D23).
- **Deadband.** A value that moves less than a threshold is not worth a data point. The two constant
  configuration registers on this device emit exactly one data point each, ever. The deadband lives
  in the poller (plan.md D22), which is where change-of-state reporting would live on a real device.
- **The report rate is not the scan rate.** The device is scanned at 1 Hz; values are reported every
  5 s (plan.md D26). Tying those together was a real bug: it put 1 106 metric points a minute across a
  link whose story is that it is a 4G backhaul. Measured today: ~195 points/min, with the deadband
  suppressing roughly a third of readings (34 838 suppressed of 110 850, `docs/sizing.md`).

The measured costs of all this — the poller's memory climb from 33 to 232 MiB and back to 32, the
transport change from gRPC to OTLP/HTTP — are in [`docs/sizing.md`](../sizing.md) under "Poller
memory" and "Transport".

## Make a voltage sag

```bash
make fault SITE=rural FAULT=voltage-sag
make logs      # energy.meter.voltage drops from ~230 V to ~180 V (0.78 pu)
make fault SITE=rural FAULT=clear
```

`--depth 0.78 --duration 20` are the defaults. `ARGS="--depth 0.6 --duration 30"` goes deeper and
longer.

This is step one of the project's spine: **voltage sag → charger derate → failed charging session**
(plan.md §4). M2 completes it — see [`docs/sites/metro.md`](metro.md) for the twelve-bay version and
the README for the span tree.

**Modelled, not measured:** the sag is injected into the meter simulator's register image. What is
real is everything downstream of it — the charge point reads that register over Modbus and reacts to
the number it can see.

## Watch time cross a bad link

```bash
make link SITE=rural PROFILE=down      # hard outage on the rural site
make queues                            # the edge queue starts filling; the solar site stays at 0
make link SITE=rural PROFILE=wet       # restore
make queues                            # the queue drains back to zero
```

Profiles: `wet` (clear), `degraded` (250 ms + 2% loss), `satellite` (650 ms + 5% loss + 32 kB/s),
`down` (outage). The queue is on disk (`file_storage`, 5 000 batches per site), so the outage loses
nothing — it delays. A 30 s rural outage accumulates ~15 batches and drains to 0 on restore.

Two things about the profiles that cost time: toxiproxy's config file declares proxies, not toxics,
so a good-link baseline belongs in `impairment/link.sh` (`PROFILE=wet`) and `make up` applies it
(`docs/findings.md` #41). And the depot's "good" link is 12 ms of fibre, not a clear one.

## Time: the site where the clock is wrong on purpose

The rural meter runs **7 seconds behind** — not as a decoration, as the ordinary condition of a site
with no usable NTP (D12). Three things follow, and they are three different things, which is the
point:

1. **The device clock is provenance, not a sort key.** The poller reads the meter's `UnixTime`
   register and the metric path carries `modbus_device_clock_error_seconds` — one bounded number per
   device, and the number an operator actually wants. It used to carry the raw timestamp on *every
   data point of every poll*, which leaked half a megabyte a minute in the OTel SDK before it was a
   cardinality problem in a store (D51, findings #57).
2. **The dual-timestamp story rides on spans and logs**, where it can be carried without cost: the
   charge point stamps `energy.supply.device.timestamp` on its sag and stop spans, and the poller
   logs the pair.
3. **A skewed clock is not `comms-lost`.** Push the meter 5 minutes behind and every one of its
   series reports `quality=stale` while the link is perfect and the values are seconds old. Push it
   4 minutes ahead and everything reports `good` with timestamps in the future — the dangerous
   direction, because nothing complains (D43).

```bash
make clock-skew SITE=rural SECONDS=240     # the meter claims it is 4 minutes in the future
./tools/clock-inversion.sh                 # the whole demonstration: an effect before its cause
make clock-reset SITE=rural
```

`tools/clock-inversion.sh` is worth running once: it produces a session stop that arrives **236
seconds before** the sag that caused it, when the events are ordered by device time, and then shows
the same two events in the right order by receive time. The write-up, including what a backend should
do about it, is [`docs/ot-primer.md` §5.1](../ot-primer.md#51-the-clock-problem-with-a-worked-example).

## The mimic for this site

Two EVSE cells on an LV board under the one-line diagram, live from M2 (state, kW, offered current,
session kWh). The drawing and its quality treatment are described in [`docs/hmi.md`](../hmi.md).
