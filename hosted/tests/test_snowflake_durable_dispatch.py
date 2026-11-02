import datetime as dt
import hashlib
import json
import os
import unittest
from unittest import mock

from snowflake_durable_dispatch import (
    DESTINATION_SQL,
    HISTORY_SQL,
    ExportError,
    export_with_durable_hold,
    reconcile_readonly,
)
from snowflake_metrics_export import (SCHEMA_VERSION, SERVICE_USER, WRITER_ROLE, _with_identity)
from tests.test_snowflake_metrics_export import DOCUMENT, ROWS, SOURCE_REVISION, TODAY, Response


REQUEST_ID = "123e4567-e89b-42d3-a456-426614174000"
DISPATCH_SECRET = "synthetic-independent-dispatch-secret-for-tests"
TARGET = {"account": "synthetic-account", "user": SERVICE_USER, "role": WRITER_ROLE,
          "warehouse": "METRICS_WH", "database": "METRICS_DB", "schema": "METRICS_SCHEMA",
          "table": "DAILY_ADMISSIONS"}


def environment():
    return {"STUDIO_METRICS_SECRET": "synthetic-credential-never-used-live",
            "STUDIO_SNOWFLAKE_DISPATCH_SECRET": DISPATCH_SECRET,
            "STUDIO_SNOWFLAKE_ACCOUNT": "synthetic-account",
            "STUDIO_SNOWFLAKE_USER": SERVICE_USER,
            "STUDIO_SNOWFLAKE_ROLE": WRITER_ROLE,
            "STUDIO_SNOWFLAKE_SERVICE_USER_DISABLED": "0",
            "STUDIO_SNOWFLAKE_WAREHOUSE": "METRICS_WH",
            "STUDIO_SNOWFLAKE_DATABASE": "METRICS_DB",
            "STUDIO_SNOWFLAKE_SCHEMA": "METRICS_SCHEMA",
            "STUDIO_SNOWFLAKE_PRIVATE_KEY_FILE": os.path.abspath("synthetic-only-key.p8"),
            "STUDIO_SNOWFLAKE_SOURCE_REVISION": SOURCE_REVISION}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class JsonResponse:
    def __init__(self, data, status, url):
        self.payload = canonical(data).encode()
        self.status = status
        self.url = url
        self.headers = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def geturl(self):
        return self.url

    def read(self, size):
        return self.payload[:size]


def hold_payload(request, *, permitted):
    return {"status": "reserved" if permitted else "held", "dispatch_permitted": permitted,
            "hold": {"request_id": request["request_id"], "target": request["target"],
                     "target_sha256": digest(request["target"]), "rows": request["rows"],
                     "payload_sha256": digest(request["rows"]), "state": "HELD_UNKNOWN",
                     "created_at": "2026-11-02T04:00:00.000Z", "evidence_class": "DECLARED",
                     "signing_status": "UNSIGNED"}}


def hold_opener(*, permitted=True, failure=None):
    opener = mock.Mock()

    def open_request(request, timeout):
        if failure:
            raise failure
        body = json.loads(request.data)
        if body == {"op": "status"}:
            rows = _with_identity(ROWS, SOURCE_REVISION)
            body = {"op": "reserve", "request_id": REQUEST_ID, "target": TARGET, "rows": rows}
            return JsonResponse(hold_payload(body, permitted=False), 200, request.full_url)
        return JsonResponse(hold_payload(body, permitted=permitted), 201, request.full_url)

    opener.open.side_effect = open_request
    return opener


def metrics_opener(payload=DOCUMENT):
    opener = mock.Mock()
    opener.open.return_value = Response(canonical(payload).encode())
    return opener


class DurableDispatchTests(unittest.TestCase):
    def test_first_reservation_dispatches_once_with_provider_query_tag(self):
        connection = mock.Mock()
        connect = mock.Mock(return_value=connection)
        self.assertEqual(export_with_durable_hold(
            environment(), metrics_opener=metrics_opener(), hold_opener=hold_opener(),
            connect=connect, request_id=REQUEST_ID, today=TODAY), 2)
        connect.assert_called_once()
        self.assertEqual(connect.call_args.kwargs["session_parameters"]["QUERY_TAG"], REQUEST_ID)
        cursor = connection.cursor.return_value
        cursor.execute.assert_called_once()
        sql, params = cursor.execute.call_args.args
        self.assertIn('MERGE INTO "METRICS_DB"."METRICS_SCHEMA"."DAILY_ADMISSIONS"', sql)
        self.assertEqual(json.loads(params[0]), _with_identity(ROWS, SOURCE_REVISION))
        cursor.close.assert_called_once()
        connection.close.assert_called_once()

    def test_replay_or_lost_hold_ack_never_reaches_snowflake(self):
        for opener, code in ((hold_opener(permitted=False), "dispatch_already_held"),
                             (hold_opener(failure=TimeoutError("lost response")),
                              "dispatch_hold_unavailable")):
            with self.subTest(code=code):
                connect = mock.Mock()
                with self.assertRaisesRegex(ExportError, f"^{code}$"):
                    export_with_durable_hold(
                        environment(), metrics_opener=metrics_opener(), hold_opener=opener,
                        connect=connect, request_id=REQUEST_ID, today=TODAY)
                connect.assert_not_called()

    def test_provider_failure_retains_uncertainty_without_second_dispatch(self):
        connection = mock.Mock()
        connection.cursor.return_value.execute.side_effect = OSError("private provider detail")
        connect = mock.Mock(return_value=connection)
        with self.assertRaisesRegex(ExportError, "^snowflake_write_uncertain$"):
            export_with_durable_hold(
                environment(), metrics_opener=metrics_opener(), hold_opener=hold_opener(),
                connect=connect, request_id=REQUEST_ID, today=TODAY)
        connect.assert_called_once()
        connection.cursor.return_value.execute.assert_called_once()

    def test_uncertain_attempt_and_fresh_runner_replay_dispatch_only_once(self):
        custody = mock.Mock()
        calls = 0

        def reserve_then_hold(request, timeout):
            nonlocal calls
            body = json.loads(request.data)
            permitted = calls == 0
            calls += 1
            return JsonResponse(hold_payload(body, permitted=permitted), 201, request.full_url)

        custody.open.side_effect = reserve_then_hold
        first_connection = mock.Mock()
        # The MERGE may have committed before connection cleanup failed.
        first_connection.close.side_effect = OSError("lost provider acknowledgement")
        first_connect = mock.Mock(return_value=first_connection)
        with self.assertRaisesRegex(ExportError, "^snowflake_write_uncertain$"):
            export_with_durable_hold(
                environment(), metrics_opener=metrics_opener(), hold_opener=custody,
                connect=first_connect, request_id=REQUEST_ID, today=TODAY)
        second_connect = mock.Mock()
        with self.assertRaisesRegex(ExportError, "^dispatch_already_held$"):
            export_with_durable_hold(
                environment(), metrics_opener=metrics_opener(), hold_opener=custody,
                connect=second_connect, request_id=REQUEST_ID, today=TODAY)
        first_connect.assert_called_once()
        first_connection.cursor.return_value.execute.assert_called_once()
        second_connect.assert_not_called()
        self.assertEqual(calls, 2)

    def test_missing_adapter_or_invalid_request_fails_before_remote_io(self):
        metrics = metrics_opener()
        connect = mock.Mock()
        with self.assertRaisesRegex(ExportError, "^snowflake_transport_not_verified$"):
            export_with_durable_hold(environment(), metrics_opener=metrics, connect=connect)
        metrics.open.assert_not_called()
        connect.assert_not_called()
        with self.assertRaisesRegex(ExportError, "^dispatch_request_invalid$"):
            export_with_durable_hold(
                environment(), metrics_opener=metrics, hold_opener=hold_opener(),
                connect=connect, request_id="not-a-request", today=TODAY)
        metrics.open.assert_not_called()
        connect.assert_not_called()


class ReconciliationTests(unittest.TestCase):
    def reconciler_environment(self):
        return {**environment(), "STUDIO_SNOWFLAKE_RECONCILE_USER": "METRICS_READER",
                "STUDIO_SNOWFLAKE_RECONCILE_ROLE": "METRICS_READER_ROLE",
                "STUDIO_SNOWFLAKE_RECONCILE_PRIVATE_KEY_FILE":
                    os.path.abspath("synthetic-readonly-key.p8")}

    def connection(self, history, destination):
        connection = mock.Mock()
        connection.cursor.return_value.fetchall.side_effect = [history, destination]
        return connection

    def test_readonly_match_observes_provider_evidence_without_releasing_hold(self):
        rows = _with_identity(ROWS, SOURCE_REVISION)
        destination = [(dt.date.fromisoformat(row["day"]), row["conversations_admitted"],
                        row["runs_admitted"], row["source_revision"], row["schema_version"],
                        row["idempotency_key"]) for row in rows]
        query_id = "01abc123-0000-0000-0000-000000000001"
        history = [(query_id, "SUCCESS", None, 2, 0, 0,
                    dt.datetime(2026, 11, 2, 4, 0, tzinfo=dt.timezone.utc),
                    dt.datetime(2026, 11, 2, 4, 1, tzinfo=dt.timezone.utc))]
        connection = self.connection(history, destination)
        result = reconcile_readonly(self.reconciler_environment(), hold_opener=hold_opener(),
                                    connect=mock.Mock(return_value=connection))
        self.assertEqual(result["outcome"], "OBSERVED_MATCH")
        self.assertEqual(result["evidence_class"], "OBSERVED")
        self.assertFalse(result["resend_permitted"])
        self.assertEqual(result["hold_state"], "HELD_UNKNOWN")
        self.assertNotIn(query_id, canonical(result))
        self.assertEqual(result["query_id_sha256"],
                         [hashlib.sha256(query_id.encode()).hexdigest()])
        cursor = connection.cursor.return_value
        self.assertEqual(cursor.execute.call_count, 2)
        self.assertEqual(cursor.execute.call_args_list[0].args[0], HISTORY_SQL)
        self.assertEqual(cursor.execute.call_args_list[1].args[0],
                         DESTINATION_SQL.format(table='"METRICS_DB"."METRICS_SCHEMA"."DAILY_ADMISSIONS"'))
        self.assertTrue(all(call.args[0].lstrip().startswith("SELECT")
                            for call in cursor.execute.call_args_list))
        cursor.close.assert_called_once()
        connection.close.assert_called_once()

    def test_unbound_or_conflicting_provider_evidence_remains_non_resendable(self):
        rows = _with_identity(ROWS, SOURCE_REVISION)
        destination = [(row["day"], row["conversations_admitted"], row["runs_admitted"],
                        row["source_revision"], row["schema_version"], row["idempotency_key"])
                       for row in rows]
        cases = [([], destination, "UNKNOWN"),
                 ([('qid', "SUCCESS", None, 1, 0, 0, None, None)], destination[:-1], "CONFLICT"),
                 ([('qid1', "SUCCESS", None, 1, 0, 0, None, None),
                   ('qid2', "SUCCESS", None, 1, 0, 0, None, None)], destination, "CONFLICT")]
        for history, observed, outcome in cases:
            with self.subTest(outcome=outcome, count=len(history)):
                result = reconcile_readonly(
                    self.reconciler_environment(), hold_opener=hold_opener(),
                    connect=mock.Mock(return_value=self.connection(history, observed)))
                self.assertEqual(result["outcome"], outcome)
                self.assertFalse(result["resend_permitted"])

    def test_writer_identity_and_missing_adapters_are_refused(self):
        with self.assertRaisesRegex(ExportError, "^snowflake_reconciliation_transport_not_verified$"):
            reconcile_readonly(self.reconciler_environment())
        env = {**self.reconciler_environment(),
               "STUDIO_SNOWFLAKE_RECONCILE_USER": "SZL_STUDIO_METRICS_SERVICE"}
        connect = mock.Mock()
        with self.assertRaisesRegex(ExportError,
                                    "^snowflake_reconciliation_configuration_invalid$"):
            reconcile_readonly(env, hold_opener=hold_opener(), connect=connect)
        connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
