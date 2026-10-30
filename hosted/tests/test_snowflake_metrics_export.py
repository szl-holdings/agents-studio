import datetime as dt
import json
import os
import unittest
from unittest import mock

from snowflake_metrics_export import (ExportError, MAX_RESPONSE_BYTES, METRICS_URL,
                                      SERVICE_USER, export_once, fetch_metrics, parse_metrics,
                                      service_identity_status, snowflake_config)


TODAY = dt.date(2026, 10, 29)
SOURCE_REVISION = "a" * 40
ROWS = [{"day": "2026-10-01", "conversations_admitted": 2, "runs_admitted": 3},
        {"day": "2026-10-28", "conversations_admitted": 0, "runs_admitted": 8}]
DOCUMENT = {"evidence_class": "DECLARED", "receipt_minted": False, "days": ROWS}


def environment():
    value = {"STUDIO_METRICS_SECRET": "synthetic-credential-never-used-live",
             "STUDIO_SNOWFLAKE_ACCOUNT": "synthetic-account",
             "STUDIO_SNOWFLAKE_USER": "METRICS_SERVICE",
             "STUDIO_SNOWFLAKE_ROLE": "METRICS_EXPORTER",
             "STUDIO_SNOWFLAKE_WAREHOUSE": "METRICS_WH",
             "STUDIO_SNOWFLAKE_DATABASE": "METRICS_DB",
             "STUDIO_SNOWFLAKE_SCHEMA": "METRICS_SCHEMA",
             "STUDIO_SNOWFLAKE_PRIVATE_KEY_FILE": os.path.abspath("synthetic-only-key.p8")}
    value["STUDIO_SNOWFLAKE_SOURCE_REVISION"] = SOURCE_REVISION
    return value


class Response:
    status = 200

    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def geturl(self):
        return METRICS_URL

    def read(self, size):
        return self.payload[:size]


class ExportTests(unittest.TestCase):
    def opener(self, payload=DOCUMENT):
        obj = mock.Mock()
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        obj.open.return_value = Response(raw)
        return obj

    def test_default_runtime_is_disabled_before_any_remote_request(self):
        opener = self.opener()
        with self.assertRaisesRegex(ExportError, "^snowflake_transport_not_verified$"):
            export_once(environment(), opener=opener, today=TODAY)
        opener.open.assert_not_called()

    def test_disabled_service_identity_is_explicit_and_blocks_before_transport(self):
        env = {**environment(), "STUDIO_SNOWFLAKE_USER": SERVICE_USER}
        status = service_identity_status(env)
        self.assertEqual(status["evidence_class"], "DECLARED")
        self.assertEqual(status["state"], "BLOCKED")
        self.assertEqual(status["reason"], "service_user_disabled")
        opener = self.opener()
        with self.assertRaisesRegex(ExportError, "^snowflake_service_user_disabled$"):
            export_once(env, opener=opener, connect=mock.Mock(), today=TODAY)
        opener.open.assert_not_called()

    def test_export_uses_one_bounded_http_read_and_one_parameterized_merge(self):
        opener = self.opener()
        connection = mock.Mock()
        connect = mock.Mock(return_value=connection)
        self.assertEqual(export_once(environment(), opener=opener, connect=connect, today=TODAY), 2)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, METRICS_URL)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 15)
        self.assertTrue(connect.call_args.kwargs["autocommit"])
        self.assertEqual(connect.call_args.kwargs["role"], "METRICS_EXPORTER")
        cursor = connection.cursor.return_value
        cursor.execute.assert_called_once()
        sql, params = cursor.execute.call_args.args
        self.assertIn('MERGE INTO "METRICS_DB"."METRICS_SCHEMA"."DAILY_ADMISSIONS"', sql)
        exported = json.loads(params[0])
        self.assertEqual([row["day"] for row in exported], [row["day"] for row in ROWS])
        self.assertTrue(all(row["source_revision"] == SOURCE_REVISION for row in exported))
        self.assertTrue(all(row["schema_version"] == "szl.metrics.daily-admissions/v2"
                            for row in exported))
        self.assertTrue(all(len(row["idempotency_key"]) == 64 for row in exported))
        cursor.close.assert_called_once()
        connection.close.assert_called_once()

    def test_private_or_malformed_fields_and_dates_fail_before_snowflake(self):
        bad_rows = [ROWS + [ROWS[-1]],
                    [{**ROWS[0], "prompt": "must not leave"}],
                    [{**ROWS[0], "day": "2026-10-29"}],
                    [{**ROWS[0], "day": "2026-09-28"}],
                    [{**ROWS[0], "runs_admitted": True}],
                    [{**ROWS[0], "runs_admitted": 201}]]
        for rows in bad_rows:
            with self.subTest(rows=rows):
                connect = mock.Mock()
                with self.assertRaises(ExportError):
                    export_once(environment(), opener=self.opener({**DOCUMENT, "days": rows}),
                                connect=connect, today=TODAY)
                connect.assert_not_called()

    def test_bad_configuration_never_calls_http(self):
        for role in ("ACCOUNTADMIN", "PUBLIC", "bad;sql"):
            env = {**environment(), "STUDIO_SNOWFLAKE_ROLE": role}
            opener = self.opener()
            with self.assertRaises(ExportError):
                export_once(env, opener=opener, connect=mock.Mock(), today=TODAY)
            opener.open.assert_not_called()
        with self.assertRaises(ExportError):
            snowflake_config({**environment(), "STUDIO_SNOWFLAKE_PRIVATE_KEY_FILE": "relative.key"})

    def test_encrypted_key_uses_the_connector_passphrase_field(self):
        config = snowflake_config({**environment(),
                                   "STUDIO_SNOWFLAKE_PRIVATE_KEY_PASSWORD": "synthetic-passphrase"})
        self.assertEqual(config["private_key_file_pwd"], "synthetic-passphrase")
        self.assertNotIn("private_key_password", config)

    def test_http_redirect_and_oversize_fail_closed(self):
        opener = self.opener()
        opener.open.return_value.geturl = lambda: "https://other.example/metrics"
        with self.assertRaises(ExportError):
            fetch_metrics(environment()["STUDIO_METRICS_SECRET"], opener=opener, today=TODAY)
        with self.assertRaises(ExportError):
            fetch_metrics(environment()["STUDIO_METRICS_SECRET"],
                          opener=self.opener(b"a" * (MAX_RESPONSE_BYTES + 1)), today=TODAY)

    def test_empty_data_never_opens_snowflake(self):
        connect = mock.Mock()
        self.assertEqual(export_once(environment(), opener=self.opener({**DOCUMENT, "days": []}),
                                     connect=connect, today=TODAY), 0)
        connect.assert_not_called()

    def test_uncertain_write_is_not_retried_or_logged_with_secrets(self):
        connection = mock.Mock()
        connection.cursor.return_value.execute.side_effect = RuntimeError("synthetic private detail")
        connect = mock.Mock(return_value=connection)
        with self.assertRaisesRegex(ExportError, "^snowflake_write_uncertain$"):
            export_once(environment(), opener=self.opener(), connect=connect, today=TODAY)
        connect.assert_called_once()
        connection.cursor.return_value.execute.assert_called_once()
        connection.cursor.return_value.close.assert_called_once()
        connection.close.assert_called_once()

    def test_boundary_days_are_inclusive_and_duplicates_rejected(self):
        rows = [{**ROWS[0], "day": "2026-09-29"}, ROWS[1]]
        self.assertEqual(parse_metrics({**DOCUMENT, "days": rows}, TODAY), rows)
        with self.assertRaises(ExportError):
            parse_metrics({**DOCUMENT, "days": rows[::-1]}, TODAY)


if __name__ == "__main__":
    unittest.main()
