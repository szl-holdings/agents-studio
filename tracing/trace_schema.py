"""Agents-Studio trace schema (absorb implementation).

Absorbs, at schema level only (source card: docs/LEADERS-STUDY.md,
leader-study PR agents-studio#59): the OpenAI Agents SDK tracing span
taxonomy (https://openai.github.io/openai-agents-python/tracing/) and
the Agents API observability guides -- span kinds for generations,
function/tool calls, handoffs and guardrails -- mapped onto
OpenTelemetry-friendly attribute names.

HONEST SCOPE: this module validates the SHAPE of trace records a
caller supplies. It performs no tracing, no network calls, no LLM
invocation, and no redaction (payload content never enters the record
by design: spans carry payload digests, not payloads).
"""
from __future__ import annotations

import re
from typing import Any

SCHEMA = "szl.agents-studio.trace-schema/v1"

SPAN_KINDS = ("generation", "function_call", "handoff", "guardrail", "session")

REQUIRED_KEYS = ("span_id", "kind", "started_at", "payload_sha256")

# OTel-friendly attribute mapping for observability backends.
OTEL_MAPPING = {
    "span_id": "trace.span_id",
    "kind": "otel span kind source: agents span kind",
    "started_at": "event start time",
    "payload_sha256": "attribute with digest of the raw payload",
}

ISO_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class TraceSchemaError(ValueError):
    """Raised when a trace span fails schema validation."""


def validate_span(span: dict[str, Any]) -> None:
    """Fail-closed validation of one trace span record."""
    if span.get("schema") not in (None, SCHEMA):
        raise TraceSchemaError(f"schema must be {SCHEMA} or absent")
    for key in REQUIRED_KEYS:
        if not span.get(key):
            raise TraceSchemaError(f"span missing required key: {key}")
    kind = span.get("kind")
    if kind not in SPAN_KINDS:
        raise TraceSchemaError(
            f"span.kind must be one of {SPAN_KINDS}, got {kind!r}"
        )
    if not ISO_TS_RE.match(str(span.get("started_at") or "")):
        raise TraceSchemaError(f"started_at must be an ISO-8601 timestamp: {span.get('started_at')!r}")
    if not SHA256_RE.match(str(span.get("payload_sha256") or "")):
        raise TraceSchemaError("payload_sha256 must be a 64-hex sha256 of the raw payload")
    if "payload" in span:
        raise TraceSchemaError(
            "spans carry payload_sha256, never payload content (redaction-by-design)"
        )
    parent = span.get("parent_span_id")
    if parent is not None and not (isinstance(parent, str) and parent):
        raise TraceSchemaError("parent_span_id must be a non-empty string or absent")


def validate_trace(trace: list[dict[str, Any]]) -> None:
    """Fail-closed validation of a trace: each span + parent link integrity."""
    seen: set[str] = set()
    for span in trace:
        validate_span(span)
        sid = str(span.get("span_id"))
        if sid in seen:
            raise TraceSchemaError(f"duplicate span_id: {sid}")
        seen.add(sid)
    for span in trace:
        parent = span.get("parent_span_id")
        if parent is not None and str(parent) not in seen:
            raise TraceSchemaError(f"parent_span_id {parent!r} has no matching span")


def render_otel_attributes(span: dict[str, Any]) -> dict[str, str]:
    """Render OTel-friendly attributes for one validated span."""
    validate_span(span)
    return {
        "agents.span_id": str(span["span_id"]),
        "agents.kind": str(span["kind"]),
        "agents.started_at": str(span["started_at"]),
        "agents.payload_sha256": str(span["payload_sha256"]),
    }
