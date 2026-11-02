"""Reject incomplete declared metrics bodies before the synthetic SQL boundary."""
import http.client
import io
import json
import unittest
from unittest import mock

from snowflake_metrics_export import ExportError, MAX_RESPONSE_BYTES, METRICS_URL, export_once
from tests.test_snowflake_metrics_export import DOCUMENT, TODAY, environment


class WireSocket:
    def __init__(self, body, headers):
        self.wire = b"HTTP/1.1 200 OK\r\n" + headers + b"\r\n" + body

    def makefile(self, *unused):
        return io.BytesIO(self.wire)


def response(body, headers):
    result = http.client.HTTPResponse(WireSocket(body, headers))
    result.begin()
    result.url = METRICS_URL
    return result


class MetricsFramingTests(unittest.TestCase):
    def export(self, headers):
        body = json.dumps(DOCUMENT).encode("utf-8")
        opener, connect = mock.Mock(), mock.Mock()
        opener.open.return_value = response(body, headers)
        return body, opener, connect

    def test_complete_json_prefix_cannot_hide_a_truncated_declared_body(self):
        size = len(json.dumps(DOCUMENT).encode("utf-8"))
        _, opener, connect = self.export(f"Content-Length: {size + 20}\r\n".encode())
        with self.assertRaisesRegex(ExportError, "^metrics_unavailable$"):
            export_once(environment(), opener=opener, connect=connect, today=TODAY)
        connect.assert_not_called()

    def test_invalid_or_oversized_declared_lengths_fail_before_sql(self):
        for value in ("-1", "+209", "0xD1", "209, 209", "not-a-length", str(MAX_RESPONSE_BYTES + 1)):
            with self.subTest(value=value):
                _, opener, connect = self.export(f"Content-Length: {value}\r\n".encode())
                with self.assertRaisesRegex(ExportError, "^metrics_invalid$"):
                    export_once(environment(), opener=opener, connect=connect, today=TODAY)
                connect.assert_not_called()

    def test_multiple_length_headers_do_not_select_one_ambiguous_value(self):
        size = len(json.dumps(DOCUMENT).encode("utf-8"))
        _, opener, connect = self.export(f"Content-Length: {size}\r\nContent-Length: {size + 20}\r\n".encode())
        with self.assertRaisesRegex(ExportError, "^metrics_invalid$"):
            export_once(environment(), opener=opener, connect=connect, today=TODAY)
        connect.assert_not_called()

    def test_complete_length_delimited_and_close_delimited_responses_remain_valid(self):
        size = len(json.dumps(DOCUMENT).encode("utf-8"))
        for headers in (b"", f"Content-Length: {size}\r\n".encode()):
            with self.subTest(headers=headers):
                _, opener, connect = self.export(headers)
                self.assertEqual(export_once(environment(), opener=opener, connect=connect,
                                             today=TODAY), 2)
                connect.return_value.cursor.return_value.execute.assert_called_once()

    def test_length_and_transfer_encoding_are_not_combined(self):
        _, opener, connect = self.export(b"Content-Length: 209\r\nTransfer-Encoding: chunked\r\n")
        with self.assertRaisesRegex(ExportError, "^metrics_invalid$"):
            export_once(environment(), opener=opener, connect=connect, today=TODAY)
        connect.assert_not_called()

    def export_chunked(self, truncated):
        body = json.dumps(DOCUMENT).encode("utf-8")
        # A valid JSON prefix does not make an incomplete HTTP message valid.
        size = len(body) + 20 if truncated else len(body)
        wire_body = f"{size:x}\r\n".encode() + body + b"\r\n"
        opener, connect = mock.Mock(), mock.Mock()
        opener.open.return_value = response(wire_body, b"Transfer-Encoding: chunked\r\n")
        with self.assertRaisesRegex(ExportError, "^metrics_unavailable$"):
            export_once(environment(), opener=opener, connect=connect, today=TODAY)
        connect.assert_not_called()

    def test_truncated_chunk_is_classified_before_sql(self):
        self.export_chunked(truncated=True)

    def test_missing_terminal_chunk_is_classified_before_sql(self):
        self.export_chunked(truncated=False)


if __name__ == "__main__":
    unittest.main()
