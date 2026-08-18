"""Correlation, structured logging, redaction and the telemetry ports (§30)."""

from app.observability.correlation import (
    CorrelationContext,
    background_scope,
    correlation_scope,
    current_context,
    current_metadata,
    interaction_scope,
    new_correlation_id,
    new_trace_id,
    request_scope,
    with_workflow_execution,
)
from app.observability.logging import StructuredFormatter, configure_logging
from app.observability.middleware import (
    CorrelationMiddleware,
    background_job_scope,
    consumed_message_scope,
)
from app.observability.ports import AITracingPort, MetricsPort, NoOpAITracing, NoOpMetrics
from app.observability.redaction import REDACTED, ContentPolicy, TelemetryRedactor

__all__ = [
    "REDACTED",
    "AITracingPort",
    "ContentPolicy",
    "CorrelationContext",
    "CorrelationMiddleware",
    "MetricsPort",
    "NoOpAITracing",
    "NoOpMetrics",
    "StructuredFormatter",
    "TelemetryRedactor",
    "background_job_scope",
    "background_scope",
    "configure_logging",
    "consumed_message_scope",
    "correlation_scope",
    "current_context",
    "current_metadata",
    "interaction_scope",
    "new_correlation_id",
    "new_trace_id",
    "request_scope",
    "with_workflow_execution",
]
