"""agents-studio tracing package (absorb implementation).

See tracing.trace_schema for the span taxonomy, validation, and
OpenTelemetry attribute rendering. Schema: szl.agents-studio.trace-schema/v1.
"""
from tracing.trace_schema import (
    TraceSchemaError,
    render_otel_attributes,
    validate_span,
    validate_trace,
)

__all__ = [
    "TraceSchemaError",
    "render_otel_attributes",
    "validate_span",
    "validate_trace",
]
