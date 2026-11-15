"""Prometheus metrics (process-local registry)."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

REGISTRY = CollectorRegistry(auto_describe=True)

HTTP_REQUESTS = Counter(
    "phoneintel_http_requests_total", "HTTP requests", ["method", "route", "status"], registry=REGISTRY
)
HTTP_LATENCY = Histogram(
    "phoneintel_http_request_duration_seconds", "HTTP request latency", ["method", "route"], registry=REGISTRY,
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
INVESTIGATIONS_CREATED = Counter(
    "phoneintel_investigations_created_total", "Investigations created", registry=REGISTRY
)
DISCOVERY_JOBS = Counter(
    "phoneintel_discovery_jobs_total", "Discovery job outcomes", ["status"], registry=REGISTRY
)
PROFILES_DISCOVERED = Counter(
    "phoneintel_profiles_discovered_total", "Profiles discovered", ["platform", "status"], registry=REGISTRY
)
OUTBOUND_REQUESTS = Counter(
    "phoneintel_outbound_requests_total", "Outbound HTTP requests", ["target", "outcome"], registry=REGISTRY
)
OUTBOUND_BLOCKED = Counter(
    "phoneintel_outbound_blocked_total", "Outbound requests blocked by SSRF policy", ["reason"], registry=REGISTRY
)
SEARCH_PROVIDER_ERRORS = Counter(
    "phoneintel_search_provider_errors_total", "Search provider failures", ["provider", "code"], registry=REGISTRY
)
DEAD_LETTER_JOBS = Gauge(
    "phoneintel_dead_letter_jobs", "Jobs currently in the dead-letter queue", registry=REGISTRY
)
