"""Tests for tracing.trace_schema (Agents SDK tracing absorb).

unittest.TestCase style so the repository CI command
``python -B -m unittest discover -s tests -v`` discovers and runs them.
"""
from __future__ import annotations

import unittest

from tracing.trace_schema import (
    TraceSchemaError,
    render_otel_attributes,
    validate_trace,
)


def _span(**over):
    base = {
        "span_id": "s1",
        "kind": "generation",
        "started_at": "2026-11-23T00:00:00Z",
        "payload_sha256": "c" * 64,
    }
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


class TraceSchemaTests(unittest.TestCase):
    def _expect_trace_error(self, fn, *args, **kwargs) -> None:
        with self.assertRaises(TraceSchemaError):
            fn(*args, **kwargs)

    def test_valid_span_and_otel_render(self):
        attrs = render_otel_attributes(_span())
        self.assertEqual(attrs["agents.kind"], "generation")

    def test_bad_kind_rejected(self):
        self._expect_trace_error(validate_trace, [_span(kind="vibes")])

    def test_bad_timestamp_rejected(self):
        self._expect_trace_error(validate_trace, [_span(started_at="yesterday")])

    def test_calendar_invalid_timestamp_rejected(self):
        # Regex-shape-valid but calendar-invalid dates must fail closed.
        self._expect_trace_error(validate_trace, [_span(started_at="2026-02-31T00:00:00Z")])

    def test_calendar_invalid_offset_rejected(self):
        self._expect_trace_error(validate_trace, [_span(started_at="2026-11-23T00:00:00+99:99")])

    def test_payload_content_rejected_by_design(self):
        self._expect_trace_error(validate_trace, [_span(payload="secret")])

    def test_parent_link_integrity(self):
        self._expect_trace_error(validate_trace, [_span(parent_span_id="ghost")])
        validate_trace([
            _span(span_id="root"),
            _span(span_id="child", parent_span_id="root"),
        ])

    def test_duplicate_span_id_rejected(self):
        self._expect_trace_error(validate_trace, [_span(), _span()])

    def test_cyclic_parent_rejected(self):
        self._expect_trace_error(validate_trace, [
            _span(span_id="a", parent_span_id="b"),
            _span(span_id="b", parent_span_id="a"),
        ])

    def test_self_parent_rejected(self):
        self._expect_trace_error(validate_trace, [_span(span_id="a", parent_span_id="a")])


if __name__ == "__main__":
    unittest.main()
