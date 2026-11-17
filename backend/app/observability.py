"""OpenTelemetry tracing exported to Langfuse over OTLP/HTTP."""

import base64

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from app.config import Settings

SERVICE_NAME = "invoice-agent-api"


def langfuse_otlp_endpoint(host: str) -> str:
    return host.rstrip("/") + "/api/public/otel/v1/traces"


def langfuse_auth_header(public_key: str, secret_key: str) -> str:
    token = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
    return f"Basic {token}"


def build_tracer_provider(settings: Settings) -> TracerProvider | None:
    """Return a provider exporting to Langfuse, or None when the keys are not configured."""
    if not settings.langfuse_public_key or not settings.langfuse_secret_key:
        return None
    exporter = OTLPSpanExporter(
        endpoint=langfuse_otlp_endpoint(settings.langfuse_host),
        headers={
            "Authorization": langfuse_auth_header(
                settings.langfuse_public_key, settings.langfuse_secret_key
            )
        },
    )
    provider = TracerProvider(
        resource=Resource.create(
            {"service.name": SERVICE_NAME, "service.version": settings.app_version}
        )
    )
    provider.add_span_processor(BatchSpanProcessor(exporter))
    return provider
