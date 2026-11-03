"""Offline-qualified orchestration for one D1-held Snowflake dispatch.

The scheduled operator remains blocked. This module supplies the source-level
boundary a future separately admitted operator must use: reserve one immutable
request in durable D1 custody, dispatch only on the first reservation response,
and keep the hold after every provider outcome. It has no release operation.
"""

import datetime as dt
import hashlib
import http.client
import json
import re
import urllib.error
import urllib.request
import uuid

from snowflake_metrics_export import (
    ExportError,
    MERGE_SQL,
    NoRedirect,
    SCHEMA_VERSION,
    SERVICE_USER,
    WRITER_ROLE,
    _qualified_table,
    _snowflake_failure,
    _source_revision,
    _with_identity,
    fetch_metrics,
    service_identity_status,
    snowflake_config,
)


DISPATCH_URL = "https://agents-store.a-11-oy.com/v1/snowflake-dispatch"
MAX_HOLD_RESPONSE_BYTES = 32768
SHA256 = re.compile(r"[a-f0-9]{64}\Z")
UUID_V4 = re.compile(r"[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}\Z")


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _dispatch_secret(environ):
    secret = environ.get("STUDIO_SNOWFLAKE_DISPATCH_SECRET", "")
    others = {environ.get("STUDIO_METRICS_SECRET"), environ.get("STUDIO_STORE_SECRET"),
              environ.get("STUDIO_ADMISSION_SECRET")}
    if (not isinstance(secret, str) or not 32 <= len(secret) <= 256
            or not all("!" <= char <= "~" for char in secret) or secret in others):
        raise ExportError("dispatch_hold_configuration_invalid")
    return secret


def _target(config):
    return {"account": config["account"], "user": config["user"], "role": config["role"],
            "warehouse": config["warehouse"], "database": config["database"],
            "schema": config["schema"], "table": "DAILY_ADMISSIONS"}


def _read_json_response(response, expected_url):
    if response.status not in (200, 201) or response.geturl() != expected_url:
        raise ExportError("dispatch_hold_unavailable")
    headers = getattr(response, "headers", None)
    lengths = headers.get_all("Content-Length", []) if headers is not None else []
    expected_length = None
    if lengths:
        if (len(lengths) != 1 or not re.fullmatch(r"[0-9]+", lengths[0].strip())
                or headers.get("Transfer-Encoding") is not None):
            raise ExportError("dispatch_hold_unavailable")
        expected_length = int(lengths[0])
        if expected_length > MAX_HOLD_RESPONSE_BYTES:
            raise ExportError("dispatch_hold_unavailable")
    raw = response.read(MAX_HOLD_RESPONSE_BYTES + 1)
    if len(raw) > MAX_HOLD_RESPONSE_BYTES or (
            expected_length is not None and len(raw) != expected_length):
        raise ExportError("dispatch_hold_unavailable")
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, ValueError):
        raise ExportError("dispatch_hold_unavailable") from None


def _post_hold(secret, data, *, opener, url=DISPATCH_URL):
    encoded = _canonical(data).encode("utf-8")
    request = urllib.request.Request(url, data=encoded, method="POST", headers={
        "Authorization": "Bearer " + secret,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "SZL-Agents-Studio-Dispatch/1",
    })
    try:
        with opener.open(request, timeout=15) as response:
            return _read_json_response(response, url)
    except ExportError:
        raise
    except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException):
        # A timeout can follow a committed reservation. The caller must not
        # infer absence or dispatch after this boundary.
        raise ExportError("dispatch_hold_unavailable") from None


def _validate_hold(data, *, request=None, require_first=False):
    if not isinstance(data, dict) or set(data) != {"status", "dispatch_permitted", "hold"}:
        raise ExportError("dispatch_hold_invalid")
    hold = data["hold"]
    allowed = {"request_id", "target", "target_sha256", "rows", "payload_sha256",
               "state", "created_at", "evidence_class", "signing_status"}
    if not isinstance(hold, dict) or set(hold) != allowed:
        raise ExportError("dispatch_hold_invalid")
    if (not isinstance(hold["request_id"], str) or not UUID_V4.fullmatch(hold["request_id"])
            or hold["state"] != "HELD_UNKNOWN" or hold["evidence_class"] != "DECLARED"
            or hold["signing_status"] != "UNSIGNED"):
        raise ExportError("dispatch_hold_invalid")
    try:
        created = dt.datetime.fromisoformat(hold["created_at"].replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        raise ExportError("dispatch_hold_invalid") from None
    if created.tzinfo is None:
        raise ExportError("dispatch_hold_invalid")
    target_json, rows_json = _canonical(hold["target"]), _canonical(hold["rows"])
    if (hold["target_sha256"] != _digest(target_json)
            or hold["payload_sha256"] != _digest(rows_json)
            or not SHA256.fullmatch(hold["target_sha256"])
            or not SHA256.fullmatch(hold["payload_sha256"])):
        raise ExportError("dispatch_hold_invalid")
    if request is not None and (hold["request_id"] != request["request_id"]
                                or hold["target"] != request["target"]
                                or hold["rows"] != request["rows"]):
        raise ExportError("dispatch_hold_invalid")
    permitted = data["dispatch_permitted"] is True and data["status"] == "reserved"
    held = data["dispatch_permitted"] is False and data["status"] == "held"
    if not (permitted or held) or (require_first and not permitted):
        raise ExportError("dispatch_already_held" if held else "dispatch_hold_invalid")
    return hold


def reserve_dispatch(secret, request_id, target, rows, *, opener, url=DISPATCH_URL):
    request = {"op": "reserve", "request_id": request_id, "target": target, "rows": rows}
    return _validate_hold(_post_hold(secret, request, opener=opener, url=url),
                          request=request, require_first=True)


def read_dispatch(secret, *, opener, url=DISPATCH_URL):
    data = _post_hold(secret, {"op": "status"}, opener=opener, url=url)
    if isinstance(data, dict) and data == {"status": "empty", "dispatch_permitted": False,
                                          "hold": None}:
        raise ExportError("dispatch_hold_missing")
    return _validate_hold(data)


def _execute(config, rows, request_id, connect):
    config = {**config, "session_parameters": {**config["session_parameters"],
                                                "QUERY_TAG": request_id}}
    connection = cursor = None
    failure = None
    phase = "connect"
    try:
        connection = connect(**config)
        phase = "cursor"
        cursor = connection.cursor()
        phase = "execute"
        cursor.execute(MERGE_SQL.format(table=_qualified_table(config)),
                       (_canonical(rows),))
    except Exception as error:
        failure = _snowflake_failure(phase, error)
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception as error:
                if failure is None:
                    failure = _snowflake_failure("cursor_close", error)
        if connection is not None:
            try:
                connection.close()
            except Exception as error:
                if failure is None:
                    failure = _snowflake_failure("connection_close", error)
    if failure is not None:
        raise failure from None


def export_with_durable_hold(environ, *, metrics_opener=None, hold_opener=None,
                             connect=None, request_id=None, today=None):
    """Reserve exactly once, then perform at most one injected Snowflake call.

    Missing adapters fail before any remote request. The production operator
    does not call this function until D1 custody and the connector are admitted.
    """
    if connect is None or hold_opener is None or metrics_opener is None:
        raise ExportError("snowflake_transport_not_verified")
    if service_identity_status(environ)["state"] == "BLOCKED":
        raise ExportError("snowflake_service_user_disabled")
    config = snowflake_config(environ)
    if config["user"] != SERVICE_USER or config["role"] != WRITER_ROLE:
        raise ExportError("snowflake_service_identity_invalid")
    secret = _dispatch_secret(environ)
    source_revision = _source_revision(environ)
    request_id = request_id or str(uuid.uuid4())
    if not isinstance(request_id, str) or not UUID_V4.fullmatch(request_id):
        raise ExportError("dispatch_request_invalid")
    days = fetch_metrics(environ.get("STUDIO_METRICS_SECRET", ""), opener=metrics_opener,
                         today=today, url=environ.get("STUDIO_METRICS_URL") or
                         "https://agents-store.a-11-oy.com/v1/metrics")
    if not days:
        return 0
    rows = _with_identity(days, source_revision)
    reserve_dispatch(secret, request_id, _target(config), rows, opener=hold_opener,
                     url=environ.get("STUDIO_SNOWFLAKE_DISPATCH_URL") or DISPATCH_URL)
    _execute(config, rows, request_id, connect)
    return len(rows)


HISTORY_SQL = """SELECT QUERY_ID, EXECUTION_STATUS, ERROR_CODE, ROWS_INSERTED,
       ROWS_UPDATED, ROWS_DELETED, START_TIME, END_TIME
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE QUERY_TAG = %s AND USER_NAME = %s
  AND ROLE_NAME = %s AND WAREHOUSE_NAME = %s
  AND DATABASE_NAME = %s AND SCHEMA_NAME = %s
  AND START_TIME >= %s AND START_TIME < %s AND QUERY_TYPE = 'MERGE'
ORDER BY START_TIME, QUERY_ID
LIMIT 21"""

DESTINATION_SQL = """SELECT METRIC_DAY, CONVERSATIONS_ADMITTED, RUNS_ADMITTED,
       SOURCE_REVISION, SCHEMA_VERSION, IDEMPOTENCY_KEY
FROM {table}
WHERE SOURCE_REVISION = %s AND SCHEMA_VERSION = %s
  AND METRIC_DAY >= %s AND METRIC_DAY <= %s
ORDER BY METRIC_DAY
LIMIT 31"""


def _reconciliation_config(environ):
    mapped = dict(environ)
    mapped["STUDIO_SNOWFLAKE_USER"] = environ.get("STUDIO_SNOWFLAKE_RECONCILE_USER", "")
    mapped["STUDIO_SNOWFLAKE_ROLE"] = environ.get("STUDIO_SNOWFLAKE_RECONCILE_ROLE", "")
    mapped["STUDIO_SNOWFLAKE_PRIVATE_KEY_FILE"] = environ.get(
        "STUDIO_SNOWFLAKE_RECONCILE_PRIVATE_KEY_FILE", "")
    if environ.get("STUDIO_SNOWFLAKE_RECONCILE_PRIVATE_KEY_PASSWORD"):
        mapped["STUDIO_SNOWFLAKE_PRIVATE_KEY_PASSWORD"] = environ[
            "STUDIO_SNOWFLAKE_RECONCILE_PRIVATE_KEY_PASSWORD"]
    config = snowflake_config(mapped)
    if config["user"] == SERVICE_USER or config["role"] == WRITER_ROLE:
        raise ExportError("snowflake_reconciliation_configuration_invalid")
    config["session_parameters"] = {**config["session_parameters"],
                                    "QUERY_TAG": "szl-studio-readonly-reconciliation"}
    return config


def _provider_rows(rows):
    normalized = []
    for row in rows:
        try:
            day = row[0].isoformat() if hasattr(row[0], "isoformat") else str(row[0])
            normalized.append({"day": day, "conversations_admitted": int(row[1]),
                               "runs_admitted": int(row[2]), "source_revision": row[3],
                               "schema_version": row[4], "idempotency_key": row[5]})
        except (IndexError, TypeError, ValueError):
            raise ExportError("snowflake_reconciliation_invalid") from None
    return normalized


def reconcile_readonly(environ, *, hold_opener=None, connect=None):
    """Observe one durable hold and Snowflake with SELECT statements only.

    The returned result never grants resend authority and never updates D1 or
    Snowflake. It is operator evidence for a separate reviewed decision.
    """
    if hold_opener is None or connect is None:
        raise ExportError("snowflake_reconciliation_transport_not_verified")
    secret = _dispatch_secret(environ)
    config = _reconciliation_config(environ)
    hold = read_dispatch(secret, opener=hold_opener,
                         url=environ.get("STUDIO_SNOWFLAKE_DISPATCH_URL") or DISPATCH_URL)
    observed_target = hold["target"]
    if (not isinstance(observed_target, dict)
            or any(observed_target.get(key) != config[key]
                   for key in ("account", "database", "schema"))
            or observed_target.get("table") != "DAILY_ADMISSIONS"):
        raise ExportError("snowflake_reconciliation_configuration_invalid")
    rows = hold["rows"]
    if not rows:
        raise ExportError("snowflake_reconciliation_invalid")
    created = dt.datetime.fromisoformat(hold["created_at"].replace("Z", "+00:00"))
    start, end = created - dt.timedelta(minutes=1), created + dt.timedelta(minutes=30)
    connection = cursor = None
    try:
        connection = connect(**config)
        cursor = connection.cursor()
        cursor.execute(HISTORY_SQL, (hold["request_id"], observed_target["user"],
                                     observed_target["role"], observed_target["warehouse"],
                                     observed_target["database"], observed_target["schema"],
                                     start, end))
        history = cursor.fetchall()
        cursor.execute(DESTINATION_SQL.format(table=_qualified_table(config)),
                       (rows[0]["source_revision"], SCHEMA_VERSION,
                        rows[0]["day"], rows[-1]["day"]))
        observed = _provider_rows(cursor.fetchall())
    except ExportError:
        raise
    except Exception:
        raise ExportError("snowflake_reconciliation_unavailable") from None
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
    truncated = len(history) > 20
    history = history[:20]
    successful = [row for row in history if len(row) >= 2 and row[1] == "SUCCESS"]
    exact = observed == rows
    if truncated or len(successful) > 1 or (successful and not exact):
        outcome = "CONFLICT"
    elif len(successful) == 1 and exact:
        outcome = "OBSERVED_MATCH"
    else:
        outcome = "UNKNOWN"
    query_ids = []
    for row in history:
        value = row[0] if row else None
        if isinstance(value, str):
            query_ids.append(_digest(value))
    return {"evidence_class": "OBSERVED", "outcome": outcome,
            "request_id": hold["request_id"], "payload_sha256": hold["payload_sha256"],
            "destination_payload_sha256": _digest(_canonical(observed)),
            "history_count": len(history), "history_truncated": truncated,
            "query_id_sha256": query_ids, "resend_permitted": False,
            "hold_state": hold["state"]}
