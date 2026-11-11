# Findings — the things that cost an hour

Every entry here was learned by running the stack, not by reading about it. They are kept in the
repository (rather than in an issue tracker) because they are reference material: the next person
hits them while debugging, not while planning.

Grouped by area. Each entry says **what happened** and **what to do**, and where the detail lives in
code or config.

---

## OpenTelemetry Collector

1. **`otel/opentelemetry-collector-contrib:0.116.0` is broken on arm64.** The image is published
   empty and fails with `exec /otelcol-contrib: no such file or directory`. **0.130.0 is pinned and
   works.** Verify any new pin with `docker manifest inspect` *and* by running it.

2. **The collector images are distroless — there is no shell.** Do not plan to `exec` in. Read a
   collector's own telemetry from the `ops` container over HTTP instead; that is what `make queues`
   and `make prom` do, and it is the OTel-native way anyway.

3. **`file_storage` needs `create_directory: true`,** and the edge collector services run
   `user: "0:0"` because the disk-backed queue is a root-owned volume.

4. **`set(time_unix_nano, Now())` is a silent no-op in 0.130.0.** The transform processor accepts it
   and ignores it. Hence the receive time rides as a datapoint attribute (`energy.receive.timestamp`)
   — plan.md D23.

5. **`groupbyattrs` merges metrics that share name + resource + scope.** Datapoint attributes
   survive; that is what turns per-phase points into one metric with N data points.

6. **A plain YAML scalar breaks on `: ` inside an OTTL string.** Quote it or reword the string.

7. **`otelcol_exporter_queue_size` counts batches, not items,** and 0.130 normalises metric names
   with double underscores. Match on prefix.

8. **`collector/edge/site.yaml` is shared by every site.** Anything site-specific must degrade
   cleanly when the env var or the device is absent — a statement that does not match simply does
   nothing, which is the design (plan.md D16).

---

## Prometheus and the visual stack

9. **Prometheus flattens a datapoint attribute into series identity.** An attribute whose value
   changes every poll does not describe a series — it mints a new one. Measured: 4 532 `energy_*`
   series in 15 minutes from one meter. Payload attributes are shed at the Prometheus boundary only
   (plan.md D25, `collector/hub/hub.yaml`).

10. **Resource attributes do not land on Prometheus data.** They go to the synthetic `target_info`
    series. The five bounded site attributes are copied resource → datapoint at the Prometheus
    boundary so the map works without a join (plan.md D25).

11. **Prometheus mangles metric names with their units.** `energy.meter.voltage` + `V` →
    `energy_meter_voltage_volts`; `m/s` → `meters_per_second`; `%` → `percent`; `1` → `ratio`;
    `kW.h` → `kW_h`. The mapping lives in one place: `POINTS` in `hmi/app.js`.

12. **A restart mints a new `service.instance.id`,** hence a new series set, with the old one
    lingering for the 5-minute staleness window. Never compare `prometheus_tsdb_head_series` across
    a restart, and make any consumer pick a winner explicitly (the HMI keeps the newer sample).

13. **Prometheus' OTLP receiver does not ingest OTLP exemplars.** Verified on 3.1.0 and 3.8.1 with
    the collectors bypassed: the metric is stored, `/api/v1/query_exemplars` returns `[]`. The SDK
    does emit them and the OTLP body does carry them — decode the request before blaming the SDK.
    plan.md D33. The working join is `ocpp.transaction.id`.

14. **`jaeger:2.19.0` ships no config file.** Mount one (`--config /etc/jaeger/config.yaml`).

15. **Do not mount a whole directory over `/etc/perses`.** Mount the config file and the
    provisioning folder only — the image keeps its CUE schemas there.

16. **Do not screenshot the HMI with Chrome's `--virtual-time-budget`.** It virtualises `Date.now()`
    and every site renders as stale. Use Playwright with a real clock and `domcontentloaded`, not
    `networkidle` (the page polls every 2 s, so the network is never idle).

17. **Playwright's bundled Chromium may not match the installed version.** Point `CHROME_PATH` at
    the cached `~/Library/Caches/ms-playwright/chromium_headless_shell-<build>/chrome-headless-shell-mac-arm64/chrome-headless-shell`.

---

## Modbus and the devices

18. **Modbus function 03 reads at most 125 registers (0x7D).** The meter image (118) fit in one read;
    the inverter (142) and wind farm (187) do not. A too-large read is an *invalid request* and the
    device answers with an exception, so the poller chunks at ≤120. The original failure was silent
    because the poller only logged after 30 *successful* polls; it now logs the first failure and
    every 30th.

19. **`value_int` is 0 for a double-valued gauge.** OTTL's `value_int` only means anything for int
    datapoints; these gauges are doubles. Use `value_double`. (`resource.attributes[...]` *is*
    available in the metric context.)

20. **Modbus carries no time.** The device exposes a vendor `UnixTime` register; that is the only
    way a Modbus device can claim when a sample happened.

21. **A free random walk will park on its clamp.** The wind model's first cut wandered to the
    2 m/s clamp and the farm idled for minutes. Use a mean-reverting (Ornstein–Uhlenbeck) walk.

22. **Sensor noise must be gated by the physical driver.** Gaussian noise on irradiance leaked
    through the DC model and the solar farm generated a few kW at 2am. If the sun is down,
    irradiance is exactly zero.

23. **pymodbus 3.16 replaced the classic datastore API.** Use `SimData`/`SimDevice`; the
    `SimDevice(action=…)` hook runs on every read. The client parameter is `device_id`, not
    `slave`/`unit`.

---

## The Python OTel SDK

24. **Repeating a metric name in `create_observable_gauge` drops later callbacks.** The SDK returns
    the same instrument for a repeated name and silently ignores the second callback set. Group
    points by metric name first, then pass every callback to one instrument. Symptom: MPPT tracker 2
    vanished.

25. **`Observation(value, attributes, context)` accepts a context, and that is how exemplars are
    attached.** With the default `TraceBasedExemplarFilter`, set the context to a sampled span.
    Verified in `sim/ocpp/cp.py` (the sag span context is stashed on every sample).

26. **Log records carry the current span's `trace_id`/`span_id` automatically**, via
    `LoggingHandler`. Verified on the exported OTLP log record. This is how OCPP frame logs are
    correlated to the session trace.

---

## OCPP (M2)

27. **Awaiting a request/response handshake before entering the socket read loop deadlocks.** The
    only reader is behind the await. Start the reader as a task first, then hand-shake —
    `OcppClient.run()` in `sim/ocpp/cp.py`.

28. **`extra=` is not a positional argument.** `LOG.info("... %s %s %s", a, b, extra=e)` raises at
    *log* time (not import time), so it survived startup and killed the connection. It was in both
    `csms.py` and `cp.py`.

29. **A session root span lands last.** Mid-session, Jaeger shows children whose parent is not in
    the trace yet, because the root only ends when the transaction does. That is correct batching.
    Do not "fix" it by ending the root early; demo on a completed session.

30. **An idle EVSE is not `comms-lost`.** A site-wide measurement must be refreshed for every entity
    on every collection, not only for the one with a transaction.

31. **Do not restart a session into a live sag.** Wait the idle gap *and* supply recovery, or a 20 s
    sag produces three failed transactions and three alarms.

---

## This host

32. **An unrelated `ssh` port-forward holds `4317`, `8889`, `9090`, `16686` and `8090`.** A Docker
    port mapping that cannot bind fails *quietly*. Prometheus publishes no host port; the UIs are on
    overridable free ports (HMI `8091`, Jaeger `16687`).

33. **Mounting scratch config files from `/tmp` fails on this host** (`/tmp` is a symlink). Put them
    under the repo.

34. **`docker compose exec` into a container that is being recreated can hang.** Bound the wait;
    do not loop forever.

35. **Read `/proc/1/status` (`VmRSS`) as well as `docker stats`.** They disagree by ~20%, and
    `docker stats` is the number plan.md D10 gates on. See `docs/sizing.md`.

---

## Process findings

36. **toxiproxy restore is not `POST toxics -d '[]'`.** Delete each toxic and re-enable the proxy —
    `impairment/link.sh` does it correctly.

37. **Always bound a host-side network client** (`curl --max-time`, `urlopen(..., timeout=)`).

38. **`make prom` must URL-encode on the host.** The `ops` box is alpine and has no `python3`; the
    target once depended on a package nobody installed.

39. **Verify an image tag exists before you trust a pin:** `docker manifest inspect <image>`, then
    run it. A published tag is not a working arm64 image (see #1).

40. **An unused build dependency is still a broken pin.** `x-generator` is kept but profiled; if
    `telemetrygen` is ever used again, remember its image has no shell and every metric is `gen`.

41. **toxiproxy's config file declares proxies, not toxics.** Adding a `toxics` array to a proxy in
    `impairment/toxiproxy.json` is accepted and **silently ignored** by 2.11.0 — the proxy comes up
    with `"toxics": []` and the link you thought was 12 ms is a clean one. A site's good-link
    baseline therefore belongs in `impairment/link.sh` (`PROFILE=wet`), and `make up` applies it. Check
    with `make profiles`, which prints the toxics array, not with the compose file.

42. **Twelve bays next to a 13 kW meter is how a gap becomes visible.** The site meter and the charge
    points are independent models (no power-flow model exists), which was tolerable with one 22 kW
    bay and is glaring with twelve at 260 kW. Nothing was faked to close it: `docs/standards.md` §6
    says the meter is its own load and §6 adds power flow to the not-claimed list. The lesson is the
    general one — **a second instance of an asset is an audit of the first**, and M3's whole job is
    to add instances.

43. **A slot with two directions needs the direction to bind it.** `energy.meter.energy` arrives twice
    (import and export registers) under one slot name, so the mimic's instrument panel picked
    whichever series Prometheus returned first — the depot's 128 MWh read as the static 12 kWh export
    register. `bound()` now takes a `direction`, and the lesson is that the point key already had
    it: `keyOf()` includes direction for exactly this reason, and the *drawing* code was the part
    that had forgotten.

44. **A session trace is incomplete until the session ends, so verify concurrency from the spans that
    exist.** The root span (and therefore `energy.evse.id`, which only rides on roots and sag spans)
    lands at the end of a 600 s transaction (see #29). While twelve sessions were live, the check that
    worked was: one `ocpp.transaction.id` per trace, and the CSMS's own connector→transaction map as
    the independent authority. Both agreed (12/12) before any root had arrived.

45. **Disabling a toxiproxy proxy does not kill an established gRPC stream.** `enabled:false` refuses
    *new* connections, but an edge that is already connected keeps delivering through the existing
    HTTP/2 stream — measured: wind samples kept arriving at Prometheus for over a minute after
    `make link SITE=wind PROFILE=down`, while `make queues` climbed. Two signals disagreeing, and the
    queue is the honest one. To measure a hard outage, reset the stream too:
    `docker compose -f compose/docker-compose.yml restart edge-wind`. Restoring the link then drains
    the queue, and the parked batches are written back under their **original** timestamps — a 100 s
    outage left no gap in Prometheus (`count_over_time` = 40 samples over the following 4 minutes).
    The multi-hour case is issue #3. `docs/telemetry.md` §8 has the measured profile table.

46. **A WAN outage and a device outage look nothing alike, and both get called "stale".** A broken
    link stops *delivery*: `otel.data_point.quality` stays `good`, the edge queue grows, and the store
    sees nothing new. A stopped device sets `comms-lost` and keeps sending at full rate. An alarm that
    keys on "no recent samples" fires for the first and misses the second; key on quality for the
    device and on queue depth for the link.

47. **A self-ignoring PromQL is the worst kind of wrong.** A poller restart mints a new
    `service.instance.id`, which is a new Prometheus series (#12), so an instant query returns
    **several** series for one logical signal. Taking `result[0]` gives the value of whichever process
    Prometheus happens to sort first — measured: frequency read 49.99 Hz while
    `energy_bess_power_real_watts` read 296 kW, which is not the droop relationship and cannot both be
    true. **Take the freshest sample** (`max` by sample timestamp, what `tools/*-measure.sh` does) or
    aggregate with `without (instance)`, and remember the HMI already does newest-wins. The number
    looked plausible, which is exactly why it took an arithmetic cross-check to catch (D41).

48. **A point that never changes can be invisible for a minute after `make up`.** `make up` starts
    the pollers and the edge collectors together, so a poller's *first* export can be refused because
    the edge's OTLP receiver is not listening yet. Every series then waits for its first change or its
    first heartbeat (`HEARTBEAT_S = 60`), so a deadbanded constant — a nameplate limit, a state, the
    grid-former id — simply is not there. Measured on the microgrid: **14 of 20 series missing for the
    first minute**, which looks exactly like a broken transform. Fixed by
    `STARTUP_ALWAYS_S = 20` in `sim/poller/poller.py`: every point is emitted on every collection for
    the first 20 s, so a lost first export is recovered at the next 5 s cycle instead of the next
    minute. This affects every site, not just the new one.

49. **A series total is not an audit, because a site in an outage still counts.** Adding the fifth
    site left `count({__name__=~"energy_.*"})` at exactly 310 — the same number the README had quoted
    for four sites — because the wind site's 90 series were inside the store's 5-minute lookback
    while its link was down, and the metro depot had fewer live sessions than when the figure was
    taken. Nothing was wrong; the total simply is not what it looks like. Quote **per site**, with the
    query: `count by (energy_site_name) ({__name__=~"energy_.*"})`.

50. **A stiff differential equation integrated once per poll is a solver bug, not a grid that
    oscillates.** The first microgrid model updated frequency once per Modbus scan (1 Hz) with H = 2 s.
    It swung between 46 and 55 Hz: with a closed-loop time constant of ~0.16 s, one Euler step per
    second is unstable by a factor of six. Fixed by running the device's own control loop at 20 Hz
    internally, which is also what real equipment does. If a modelled quantity has a time constant
    shorter than the poll interval, the model must not be stepped at the poll interval.

51. **A constant clock offset cannot invert anything; two clocks can.** The first version of the
    clock-skew demo copied the supply meter's clock onto *both* the sag span and the stop span, and
    the two orderings came out identical — the offset cancels. The inversion only appears when each
    timestamp is attributed to the thing that measured it: the sag comes from the meter (its clock,
    measured 239.8 s out), the stop comes from the charge point (its own clock). That is also what a
    real estate looks like: metric timestamps from devices, span timestamps from software. Written up
    in `docs/ot-primer.md` §5.1 with the measured output; the demo is `tools/clock-inversion.sh`.

52. **A meter clock 5 minutes *behind* turns every point `stale` while the link is perfect.**
    `poller.py` derives quality from the device's own clock (`STALE_AFTER_S = 30`), so a healthy device
    with a wrong clock reports `stale` on everything: measured 28 rural series carrying `stale`, the edge
    queue stayed at 0, and the charge point kept charging at 0.99 pu. It is *not* `comms-lost`, and the
    four words are not synonyms (D21). The opposite direction is worse: a clock *ahead* reports `good`
    on everything and puts the timestamps in the future, so nothing complains at all.

53. **`cmd | python3 - <<'PY'` does not feed the pipe to stdin.** The heredoc wins, `sys.stdin` is
    already at EOF, and `json.load` fails with "Expecting value: line 1 column 1 (char 0)" — which
    looks exactly like an empty API response. Write the payload to a `mktemp` file and pass the path
    as `argv[1]`. Also: Jaeger's `/api/traces` reports `startTime` in **microseconds**; dividing twice
    prints a timestamp nineteen days in the future, which is how this one was caught.

54. **`make spine` assumes EVSE01's expo session is already running.** The charge point starts one
    on its own so the demo repeats, and `cpctl.py start` is a no-op if it is already there — which
    is what you want — but a `spine` run against a *freshly restarted* CP that has not yet reached
    its first session will sag into an idle bay and produce no `PowerLoss`. Wait for
    `make session-status` to show `Charging` before judging the run.

55. **"Local time" in the simulators is the container's clock, and the containers run UTC.** The
    solar site's irradiance curve uses `time.localtime()`, and inside the container that is UTC,
    not the site's timezone — Mildura is at 142°E, so true solar noon is ~09:30 UTC, and the
    modelled curve is about 1.5 h off solar time and 11 h off the host's local clock. It has been
    like that since M1b and nobody noticed because the site looked plausible either way. Not fixed
    here: `SOLAR_CLOCK_HOUR` pins the curve for a demo, and the honest fix is a `TZ`/longitude-aware
    solar position, which is a modelling change, not a bug fix. Recorded so the next person does
    not "discover" it and think the pipeline is lying.

56. **`docker compose -f base.yml -f overlay.yml down` takes down the base stack too.** An overlay
    adds services; `down` on the merged project stops *everything*, and it removes the shared
    networks while it is at it — which killed an in-flight two-hour outage measurement mid-run and
    restarted every container in the base stack. To remove only what an overlay added:
    `docker compose -f ... -f overlay.yml rm -sf <the overlay's services>`
    (`--remove-orphans` is the other trap: with an overlay it will happily remove the *base*
    project's orphaned containers).

57. **A per-sample data point attribute is a memory leak in the OTel Python SDK, before it is a
    cardinality problem in a store.** `sim/poller/poller.py` carried `energy.device.timestamp` on
    every data point of every poll, deliberately (D12/D23), and it cost **+0.41 MiB/min** on the
    rural poller and **+0.63 MiB/min** on the solar one over a 55-minute soak (`tools/poller-soak.sh`),
    while the charge point and CSMS — whose attributes are bounded (`energy.evse.id`,
    `energy.quality`) — stayed flat at ±0.07 MiB/min. The SDK keys its aggregation state by attribute
    set and never evicts, so a new timestamp per poll is a new aggregation entry per poll, forever.
    At solar's rate that is ~900 MiB/day, which is why `poller-metro` measured 103.6 MiB beside a
    fresh poller's 31.8 MiB (#18). Fixed by D51: the clock rides on spans and logs, and the metric
    path carries one bounded `modbus.device.clock_error` per device. The rule generalises D25 —
    payload is not a dimension, and the SDK applies that rule *before* the data reaches the store.

58. **A soak has to outlive the warm-up to say anything.** The same poller measured 31.8 MiB fresh,
    45 MiB at ten minutes and 65 MiB at fifty-five. Every one of those is a true measurement and only
    one of them is a *footprint*. `tools/poller-soak.sh` exists so the number is quoted with its age,
    and `docs/sizing.md` now says "a *new* poller is 32 MiB" rather than "a poller is 32 MiB".

59. **`docker system df -v` over-reports a volume's size, by about 2x here.** During #3's outage the
    wind edge's queue volume measured **32 841 KiB** from `docker system df -v` and **16.8 MiB** from
    `du -sk` inside a throwaway container mounting the same volume. Same volume, same moment: `df -v`
    counts allocated blocks including filesystem overhead, `du` counts the files. `tools/outage-measure.sh`
    samples the former (it is one cheap call per minute), so its `disk_kib` column is a *relative*
    series and its absolute values are not the disk usage. The number to quote is the `du` one:
    `docker run --rm -v <volume>:/q:ro alpine du -sk /q`. Both are recorded for the outage run, and
    the write-up quotes the `du` figure with the method.

60. **A drained queue's history is silently rejected by Prometheus' OTLP receiver.** `otelcol`'s
    export queue replays batches under their **original** timestamps, which is the right thing to do
    (the data belongs to when it happened). Prometheus, however, only accepts samples inside its
    out-of-order window, so a drained backlog older than the series' head is dropped — and the
    collector never knows: the hub's OTLP receiver answers `200`, the edge's queue empties, and the
    evidence is a single line in Prometheus' log, `msg="Out of order sample from remote write"`.
    Measured on a 78 s outage: the microgrid's replayed series were all rejected, the edge's queue
    went to 0, and the store showed a gap. **This is the answer to "what does the backend see after
    an outage" that #3 asks for, and it is not the answer this repo had assumed**: the pipeline loses
    nothing, and the store still may. The two claims have to be separated — "nothing was dropped by
    the pipeline" is measurable at the edge (`enqueue_failed`, queue depth), and "the store kept it"
    is a *different* measurement, done with `count_over_time` across the outage window.
