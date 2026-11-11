"""OTLP wiring for the OCPP simulators - traces, metrics and logs, over OTLP/HTTP only.

Both OCPP roles are applications rather than device drivers, so unlike the Modbus poller they
are free to emit semantic names directly. What they must *not* do is invent a second way around
the pipeline: they send OTLP/HTTP to the site's edge collector and nothing else (D15, D18), and
they leave `energy.site.id` / geography to the edge collector's `resource/site` processor so the
site stamp has exactly one author.

The one convention they share with the Modbus path is quality: they set
`energy.quality` (`good` / `stale` / `bad` / `comms-lost`) on a data point, and the edge
collector turns that into `otel.data_point.quality` (D21). Applications get the same quality
vocabulary as devices, which is the point.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Callable

from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

LOG = logging.getLogger("ocpp.telemetry")


def _base() -> str | None:
    base = os.environ.get("OTLP_ENDPOINT", "").strip()
    if not base:
        return None
    if "://" not in base:
        base = "http://" + base
    return base.rstrip("/")


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass
class Telemetry:
    """The three providers, or nothing if `OTLP_ENDPOINT` is unset (a local unit-test run)."""

    resource: Resource
    tracer_provider: TracerProvider | None = None
    meter_provider: MeterProvider | None = None
    logger_provider: LoggerProvider | None = None
    _handlers: list = field(default_factory=list)

    def tracer(self, name: str):
        return self.tracer_provider.get_tracer(name) if self.tracer_provider else _NoopTracer()

    def meter(self, name: str):
        return self.meter_provider.get_meter(name) if self.meter_provider else _NoopMeter()

    def shutdown(self) -> None:
        for provider in (self.meter_provider, self.tracer_provider, self.logger_provider):
            if provider is not None:
                try:
                    provider.shutdown()
                except Exception:  # noqa: BLE001 - shutdown must never raise
                    LOG.debug("provider shutdown failed", exc_info=True)


def setup(role: str, attributes: dict[str, object],
          export_interval_ms: int = 5000) -> Telemetry:
    """Build the providers this role needs. Traces + logs always; metrics when asked for.

    `role` becomes `service.name`; `attributes` are the resource attributes only the emitter
    can know (`energy.device.kind`, `energy.evse.id`, ...). Site identity is deliberately NOT
    set here - the edge collector owns that (D20, see the module docstring).
    """
    resource = Resource.create({
        "service.name": role,
        "service.namespace": "vecn",
        **attributes,
    })
    telemetry = Telemetry(resource=resource)
    base = _base()
    if base is None:
        return telemetry

    telemetry.tracer_provider = TracerProvider(resource=resource)
    telemetry.tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{base}/v1/traces")))

    telemetry.meter_provider = MeterProvider(
        resource=resource,
        metric_readers=[PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=f"{base}/v1/metrics"),
            export_interval_millis=export_interval_ms,
        )],
    )

    telemetry.logger_provider = LoggerProvider(resource=resource)
    telemetry.logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(OTLPLogExporter(endpoint=f"{base}/v1/logs")))
    handler = LoggingHandler(level=logging.INFO, logger_provider=telemetry.logger_provider)
    logging.getLogger("ocpp").addHandler(handler)
    telemetry._handlers.append(handler)  # type: ignore[attr-defined]
    return telemetry


# --------------------------------------------------------------------------------------
# No-op fallbacks. Running `python ocpp/cp.py` on a laptop with no collector should print
# frames and stay up, not crash on a missing exporter - the same courtesy the poller extends.
# --------------------------------------------------------------------------------------
class _NoopSpan:
    def set_attribute(self, *_a, **_k): pass
    def set_attributes(self, *_a, **_k): pass
    def add_event(self, *_a, **_k): pass
    def record_exception(self, *_a, **_k): pass
    def set_status(self, *_a, **_k): pass
    def end(self, *_a, **_k): pass
    def get_span_context(self): return _INVALID


class _INVALID:  # pragma: no cover - trivial
    trace_id = 0
    span_id = 0


class _NoopTracer:
    def start_as_current_span(self, *_a, **_k):
        import contextlib
        return contextlib.nullcontext(_NoopSpan())

    def start_span(self, *_a, **_k):
        return _NoopSpan()


class _NoopMeter:
    def create_observable_gauge(self, *_a, **_k):
        return None

    def create_counter(self, *_a, **_k):
        return None
