"""Recorded NEM spot price feed — the one signal here that is not a device measurement.

    [this]  --OTLP-->  [edge collector]  --> WAN -->  [hub] --> stores

Why this exists
---------------
Price is what turns a kilowatt-hour into money, and it is the first thing a reader asks for after
the telemetry starts making sense. The question the issue asked (#22) was live spot price or a
static tariff; this is the third option, and the one that keeps the repo's central property: **it
runs offline, deterministically, with no external dependency and no API key.**

It replays `docs/measurements/market-prices-nsw1.csv` — 288 five-minute intervals, one day, with a
midday negative-price episode. **Those prices are not real** (the file says so in its own header):
they are a representative shape, because what is being demonstrated is that a cost is derivable
from telemetry, not that the prices are.

What it emits
-------------
* `energy.market.price` — AUD per MWh, attribute `energy.market.region` (NSW1). The unit is
  `[AUD]/MW.h` in UCUM, so Prometheus stores it as `energy_market_price_AUD_per_MW_h`.
* `energy.market.price.feed_age` — seconds between the wall clock and the timestamp of the price
  being replayed. This is the honest signal that a *recorded* feed is not a live one: it grows as
  the replay drifts from the recording, and a dashboard can say "this price is three hours old".

Deliberately not a site: there is no OT zone for a market. The feed emits to the **rural site's**
edge collector, which is the same path every other signal takes, so a market price is stamped with
the same site identity that the cost query joins on. `docs/standards.md` §5 says plainly that a
market price is not a device measurement and must not be dressed as one.
"""

from __future__ import annotations

import csv
import logging
import os
import signal
import time
from datetime import datetime, timezone

from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.metrics import Observation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource

LOG = logging.getLogger("market")

REGION = os.environ.get("MARKET_REGION", "NSW1")
PRICE_FILE = os.environ.get("MARKET_PRICE_FILE",
                            "/app/market/../../docs/measurements/market-prices-nsw1.csv")


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def load_prices(path: str) -> list[tuple[float, float]]:
    """(unix seconds, AUD/MWh) pairs, in file order. Header and '#' lines are skipped."""
    out: list[tuple[float, float]] = []
    with open(path, newline="") as f:
        for row in csv.reader(f):
            if not row or row[0].startswith("#") or row[0] == "utc":
                continue
            ts = datetime.strptime(row[0], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            out.append((ts.timestamp(), float(row[1])))
    if not out:
        raise RuntimeError(f"no prices in {path}")
    return out


class Replay:
    """Replays the recorded day, looping, at wall-clock speed.

    It maps "now" onto the recording by time-of-day, so the replay is *in phase with the sun*
    rather than with how long the container has been up: at 1pm the site sees the recording's 1pm
    price, which is the one with the negative strip in it. That makes `make up` at any hour show
    something meaningful, which was the point of choosing a recording.
    """

    def __init__(self, prices: list[tuple[float, float]]) -> None:
        self.prices = prices
        self.start = prices[0][0]
        self.length = prices[-1][0] - prices[0][0] + 300.0

    def at(self, now: float) -> tuple[float, float, float]:
        """(price, replay_timestamp, feed_age_s) for a given wall clock."""
        offset = (now - self.start) % self.length
        target = self.start + offset
        best = min(self.prices, key=lambda p: abs(p[0] - target))
        return best[1], best[0], max(0.0, now - best[0])


def main() -> None:
    logging.basicConfig(
        level=_env("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )
    prices = load_prices(PRICE_FILE)
    replay = Replay(prices)
    region = {"energy.market.region": REGION}

    base = _env("OTLP_ENDPOINT", "http://edge-rural:4318")
    if "://" not in base:
        base = "http://" + base
    endpoint = base.rstrip("/") + "/v1/metrics"

    provider = MeterProvider(
        resource=Resource.create({
            "service.name": "market-feed",
            "service.namespace": "vecn",
            "energy.market.region": REGION,
            "energy.market.source": "recorded",
            "energy.market.recording": os.path.basename(PRICE_FILE),
        }),
        metric_readers=[PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=endpoint),
            export_interval_millis=int(_env("OTEL_METRIC_EXPORT_INTERVAL_MS", "5000")),
        )],
    )
    meter = provider.get_meter("vecn.market-feed", version="0.1.0")

    def observe_price(_options):
        price, _ts, _age = replay.at(time.time())
        return [Observation(price, attributes=region)]

    def observe_age(_options):
        _price, _ts, age = replay.at(time.time())
        return [Observation(age, attributes=region)]

    meter.create_observable_gauge(
        "energy.market.price", callbacks=[observe_price], unit="[AUD]/MW.h",
        description="NEM-style spot price for the region, AUD per MWh. Recorded, replayed in "
                    "phase with the time of day - not a live feed (#22).",
    )
    meter.create_observable_gauge(
        "energy.market.price.feed_age", callbacks=[observe_age], unit="s",
        description="Age of the replayed price against the wall clock. A recorded feed is not a "
                    "live one, and this is the number that says so.",
    )

    stop = False

    def _halt(*_a):
        nonlocal stop
        stop = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _halt)

    price, ts, age = replay.at(time.time())
    LOG.info("market feed online: %s, %d intervals from %s, replaying %.2f AUD/MWh (age %.0fs) -> %s",
             REGION, len(prices), os.path.basename(PRICE_FILE), price, age, endpoint)
    while not stop:
        time.sleep(1.0)
    provider.shutdown()


if __name__ == "__main__":
    main()
