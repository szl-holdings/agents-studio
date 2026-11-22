"""Tests for tracing.trace_schema (Agents SDK tracing absorb). Stdlib only."""
from __future__ import annotations

from tracing.trace_schema import (
    TraceSchemaError,
    render_otel_attributes,
    validate_trace,
)


def _expect_trace_error(fn, *args, **kwargs) -> None:
    try:
        fn(*args, **kwargs)
    except TraceSchemaError:
        return
    raise AssertionError("expected TraceSchemaError")


def _span(**over):
    base = {
        "span_id": "s1",
        "kind": "generation",
        "started_at": "2026-11-23T00:00:00Z",
        "payload_sha256": "c" * 64,
    }
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


def test_valid_span_and_otel_render():
    attrs = render_otel_attributes(_span())
    assert attrs["agents.kind"] == "generation"


def test_bad_kind_rejected():
    _expect_trace_error(validate_trace, [_span(kind="vibes")])


def test_bad_timestamp_rejected():
    _expect_trace_error(validate_trace, [_span(started_at="yesterday")])


def test_payload_content_rejected_by_design():
    _expect_trace_error(validate_trace, [_span(payload="secret")])


def test_parent_link_integrity():
    _expect_trace_error(validate_trace, [_span(parent_span_id="ghost")])
    validate_trace([
        _span(span_id="root"),
        _span(span_id="child", parent_span_id="root"),
    ])


def test_duplicate_span_id_rejected():
    _expect_trace_error(validate_trace, [_span(), _span()])
