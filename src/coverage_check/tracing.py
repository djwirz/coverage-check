"""One trace per check, from submit through each claim and payer call (ADR 0013).

Off unless OTEL_EXPORTER_OTLP_ENDPOINT is set. Spans are exported from a background thread,
so a collector that is slow or away never holds up a check. Spans carry ids and outcomes,
never member details.
"""

from __future__ import annotations

import os

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

# A full batch is thousands of submit spans at once; the default queue would drop most.
QUEUE_SIZE = 16_384
EXPORT_TIMEOUT_SECONDS = 5

_propagator = TraceContextTextMapPropagator()
_provider: trace.TracerProvider = trace.NoOpTracerProvider()


def configure(service: str) -> None:
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return
    provider = TracerProvider(resource=Resource.create({SERVICE_NAME: f"coverage-check-{service}"}))
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(timeout=EXPORT_TIMEOUT_SECONDS), max_queue_size=QUEUE_SIZE
        )
    )
    use(provider)


def use(provider: trace.TracerProvider) -> None:
    global _provider
    _provider = provider


def shutdown() -> None:
    """Send what is still queued."""
    if isinstance(_provider, TracerProvider):
        _provider.shutdown()


def tracer() -> trace.Tracer:
    return _provider.get_tracer("coverage_check")


def carry(span: trace.Span) -> str | None:
    """The span as a traceparent, to keep with its check. None when tracing is off."""
    carrier: dict[str, str] = {}
    _propagator.inject(carrier, trace.set_span_in_context(span, Context()))
    return carrier.get("traceparent")


def resume(traceparent: str | None) -> Context:
    """The context a check's later spans belong to. Empty: they start a trace of their own."""
    carrier = {"traceparent": traceparent} if traceparent else {}
    return _propagator.extract(carrier, Context())
