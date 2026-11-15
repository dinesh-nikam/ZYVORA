"""Optional OpenTelemetry instrumentation (``pip install phoneintel-backend[otel]``).

Enabled with ``OTEL_ENABLED=true``; exporter endpoint/headers come from the
standard ``OTEL_EXPORTER_OTLP_*`` environment variables.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def setup_tracing(app: Any, engine: Any, service_name: str) -> None:
    try:
        from opentelemetry import trace  # noqa: PLC0415
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter  # noqa: PLC0415
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor  # noqa: PLC0415
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor  # noqa: PLC0415
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor  # noqa: PLC0415
        from opentelemetry.sdk.resources import Resource  # noqa: PLC0415
        from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415
        from opentelemetry.sdk.trace.export import BatchSpanProcessor  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError("OTEL_ENABLED=true but the 'otel' extra is not installed") from exc

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app, excluded_urls="health/live,health/ready,metrics")
    HTTPXClientInstrumentor().instrument()
    SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine)
    logger.info("OpenTelemetry tracing enabled")
