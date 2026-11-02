"""Single-run, opt-in export of aggregate admission counts to Snowflake.

LOCAL PROTOTYPE ONLY: a real Snowflake transport is intentionally disabled.
The connector can retry internally; an exact request identity and durable
uncertainty record must be implemented and verified before enabling dispatch.
The destination, service identity, retention and cost controls must also be
provisioned separately. No conversation content is accepted.
"""

import datetime as dt
import hashlib
import json
import os
import re
import urllib.error
import urllib.request


METRICS_URL = "https://agents-store.a-11-oy.com/v1/metrics"
MAX_RESPONSE_BYTES = 8192
MAX_DAYS = 30
SAFE_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
ACCOUNT_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
FORBIDDEN_ROLES = {"ACCOUNTADMIN", "SECURITYADMIN", "ORGADMIN", "SYSADMIN", "USERADMIN", "PUBLIC"}
SERVICE_USER = "SZL_STUDIO_METRICS_SERVICE"
SERVICE_USER_DISABLED_ENV = "STUDIO_SNOWFLAKE_SERVICE_USER_DISABLED"
WRITER_ROLE = "SZL_STUDIO_METRICS_WRITER"
SCHEMA_VERSION = "szl.metrics.daily-admissions/v2"
SOURCE_REVISION_RE = re.compile(r"[0-9a-f]{40}\Z")


class ExportError(Exception):
    def __init__(self, code, *, phase=None, errno=None, sqlstate=None):
        super().__init__(code)
        self.diagnostic = None
        if phase in ("connect", "cursor", "execute", "cursor_close", "connection_close"):
            self.diagnostic = {"phase": phase}
            if type(errno) is int and 0 <= errno <= 999999:
                self.diagnostic["errno"] = errno
            if isinstance(sqlstate, str) and re.fullmatch(r"[0-9A-Z]{5}", sqlstate):
                self.diagnostic["sqlstate"] = sqlstate


def _snowflake_failure(phase, error):
    # Keep only a local phase and bounded connector codes. Provider messages,
    # SQL, query IDs, account names and credentials must never reach logs.
    return ExportError("snowflake_write_uncertain", phase=phase,
                       errno=getattr(error, "errno", None),
                       sqlstate=getattr(error, "sqlstate", None))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ExportError("metrics_redirect_denied")


def service_identity_status(environ):
    """Return a non-secret, fail-closed status for the declared service identity.

    The disabled Snowflake user is a control-plane declaration only. This
    helper deliberately does not probe Snowflake, read a key, or infer grants
    and warehouse access. It gives the application a stable status to display
    while the real transport remains unavailable.
    """
    environ = environ or {}
    user = environ.get("STUDIO_SNOWFLAKE_USER") or SERVICE_USER
    disabled = str(environ.get(SERVICE_USER_DISABLED_ENV, "1")).strip().lower() in {
        "1", "true", "yes", "on"
    }
    if user != SERVICE_USER:
        return {"evidence_class": "DECLARED", "state": "UNKNOWN",
                "user": user, "role": WRITER_ROLE,
                "transport": "DISABLED", "reason": "service_identity_unbound"}
    return {"evidence_class": "DECLARED", "state": "BLOCKED" if disabled else "UNKNOWN",
            "user": SERVICE_USER, "role": WRITER_ROLE, "transport": "DISABLED",
            "reason": "service_user_disabled" if disabled else "service_user_state_unverified"}


def parse_metrics(data, today=None):
    """Accept only the specified owner-free aggregate contract."""
    today = today or dt.datetime.now(dt.timezone.utc).date()
    if not isinstance(data, dict) or set(data) != {"evidence_class", "receipt_minted", "days"}:
        raise ExportError("metrics_invalid")
    if data["evidence_class"] != "DECLARED" or data["receipt_minted"] is not False:
        raise ExportError("metrics_invalid")
    days = data["days"]
    if not isinstance(days, list) or len(days) > MAX_DAYS:
        raise ExportError("metrics_invalid")
    previous = None
    for item in days:
        if not isinstance(item, dict) or set(item) != {"day", "conversations_admitted", "runs_admitted"}:
            raise ExportError("metrics_invalid")
        try:
            day = dt.date.fromisoformat(item["day"])
        except (TypeError, ValueError):
            raise ExportError("metrics_invalid") from None
        if item["day"] != day.isoformat() or not today - dt.timedelta(days=MAX_DAYS) <= day < today:
            raise ExportError("metrics_invalid")
        if previous is not None and day <= previous:
            raise ExportError("metrics_invalid")
        previous = day
        for key in ("conversations_admitted", "runs_admitted"):
            value = item[key]
            if type(value) is not int or not 0 <= value <= 200:
                raise ExportError("metrics_invalid")
    return days


def _metrics_url(environ):
    value = environ.get("STUDIO_METRICS_URL", METRICS_URL)
    if not isinstance(value, str) or not value.startswith("https://") or "\n" in value or "\r" in value:
        raise ExportError("metrics_configuration_invalid")
    return value


def fetch_metrics(secret, *, opener=None, today=None, url=METRICS_URL):
    if not isinstance(secret, str) or len(secret) < 32 or "\n" in secret or "\r" in secret:
        raise ExportError("metrics_configuration_invalid")
    request = urllib.request.Request(
        url,
        headers={"Authorization": "Bearer " + secret,
                 "Accept": "application/json",
                 "User-Agent": "SZL-Agents-Studio-Metrics/1"},
    )
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=15) as response:
            if response.status != 200 or response.geturl() != url:
                raise ExportError("metrics_unavailable")
            headers = getattr(response, "headers", None)
            lengths = headers.get_all("Content-Length", []) if headers is not None else []
            expected_length = None
            if lengths:
                if (len(lengths) != 1 or not re.fullmatch(r"[0-9]+", lengths[0].strip())
                        or headers.get("Transfer-Encoding") is not None):
                    raise ExportError("metrics_invalid")
                try:
                    expected_length = int(lengths[0])
                except ValueError:
                    raise ExportError("metrics_invalid") from None
                if expected_length > MAX_RESPONSE_BYTES:
                    raise ExportError("metrics_invalid")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            # A bounded HTTPResponse.read() can return a valid JSON prefix
            # without raising when the declared body is truncated.
            if expected_length is not None and len(raw) != expected_length:
                raise ExportError("metrics_unavailable")
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ExportError("metrics_invalid")
        data = json.loads(raw)
    except ExportError:
        raise
    except (OSError, ValueError, urllib.error.URLError):
        raise ExportError("metrics_unavailable") from None
    return parse_metrics(data, today)


def snowflake_config(environ):
    names = ("account", "user", "role", "warehouse", "database", "schema")
    values = {name: environ.get("STUDIO_SNOWFLAKE_" + name.upper(), "") for name in names}
    if not ACCOUNT_IDENTIFIER.fullmatch(values["account"]):
        raise ExportError("snowflake_configuration_invalid")
    if any(not SAFE_IDENTIFIER.fullmatch(values[name]) for name in names[1:]):
        raise ExportError("snowflake_configuration_invalid")
    if values["role"].upper() in FORBIDDEN_ROLES:
        raise ExportError("snowflake_configuration_invalid")
    key_path = environ.get("STUDIO_SNOWFLAKE_PRIVATE_KEY_FILE", "")
    if not key_path or not os.path.isabs(key_path):
        raise ExportError("snowflake_configuration_invalid")
    values.update(private_key_file=key_path, login_timeout=10, network_timeout=30, autocommit=True,
                  session_parameters={"STATEMENT_TIMEOUT_IN_SECONDS": 30,
                                      "CLIENT_SESSION_KEEP_ALIVE": False})
    passphrase = environ.get("STUDIO_SNOWFLAKE_PRIVATE_KEY_PASSWORD")
    if passphrase:
        # snowflake.connector.connect accepts private_key_file_pwd, not
        # private_key_password. The wrong name fails login as an uncertain write.
        values["private_key_file_pwd"] = passphrase
    return values


# The fixed table must be provisioned separately in the configured database/schema.
# A single SQL statement avoids one round-trip per row. A single scheduler/writer
# is required; Snowflake standard-table uniqueness is not enforced.
MERGE_SQL = """MERGE INTO {table} AS target
USING (SELECT value:day::DATE AS metric_day,
              value:conversations_admitted::INTEGER AS conversations_admitted,
              value:runs_admitted::INTEGER AS runs_admitted,
              value:source_revision::VARCHAR AS source_revision,
              value:schema_version::VARCHAR AS schema_version,
              value:idempotency_key::VARCHAR AS idempotency_key
       FROM TABLE(FLATTEN(INPUT => PARSE_JSON(%s)))) AS source
ON target.METRIC_DAY = source.metric_day
AND target.SOURCE_REVISION = source.source_revision
AND target.SCHEMA_VERSION = source.schema_version
WHEN MATCHED THEN UPDATE SET CONVERSATIONS_ADMITTED = source.conversations_admitted,
                             RUNS_ADMITTED = source.runs_admitted,
                             IDEMPOTENCY_KEY = source.idempotency_key,
                             EXPORTED_AT = CURRENT_TIMESTAMP()
WHEN NOT MATCHED THEN INSERT
  (METRIC_DAY, CONVERSATIONS_ADMITTED, RUNS_ADMITTED, SOURCE_REVISION,
   SCHEMA_VERSION, IDEMPOTENCY_KEY, EXPORTED_AT)
  VALUES (source.metric_day, source.conversations_admitted, source.runs_admitted,
          source.source_revision, source.schema_version, source.idempotency_key,
          CURRENT_TIMESTAMP())"""


def _qualified_table(config):
    # snowflake_config has already restricted these identifiers to a safe subset.
    return ".".join(f'"{config[name]}"' for name in ("database", "schema")) + '."DAILY_ADMISSIONS"'


def _source_revision(environ):
    revision = environ.get("STUDIO_SNOWFLAKE_SOURCE_REVISION", "")
    if not isinstance(revision, str) or not SOURCE_REVISION_RE.fullmatch(revision):
        raise ExportError("snowflake_configuration_invalid")
    return revision


def _with_identity(days, source_revision):
    rows = []
    for item in days:
        day = item["day"]
        identity = f"{source_revision}:{SCHEMA_VERSION}:{day}"
        key = hashlib.sha256(identity.encode("ascii")).hexdigest()
        rows.append({**item, "source_revision": source_revision,
                     "schema_version": SCHEMA_VERSION, "idempotency_key": key})
    return rows


def export_once(environ, *, opener=None, connect=None, today=None):
    if service_identity_status(environ)["state"] == "BLOCKED":
        raise ExportError("snowflake_service_user_disabled")
    # Injection exists solely for synthetic tests. No production connector is
    # selected until its retry/dispatch behavior has been proved safe.
    if connect is None:
        raise ExportError("snowflake_transport_not_verified")
    # Validate every local input before the first remote request or query.
    config = snowflake_config(environ)
    source_revision = _source_revision(environ)
    days = fetch_metrics(environ.get("STUDIO_METRICS_SECRET", ""), opener=opener, today=today,
                         url=_metrics_url(environ))
    if not days:
        return 0
    rows = _with_identity(days, source_revision)
    connection = cursor = None
    failure = None
    phase = "connect"
    try:
        connection = connect(**config)
        phase = "cursor"
        cursor = connection.cursor()
        phase = "execute"
        cursor.execute(MERGE_SQL.format(table=_qualified_table(config)),
                       (json.dumps(rows, separators=(",", ":")),))
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
        # Preserve the first failure even if cleanup also fails. A diagnostic
        # phase is not provider reconciliation or permission to retry.
        raise failure from None
    return len(days)


if __name__ == "__main__":
    try:
        count = export_once(os.environ)
    except ExportError as error:
        print(str(error))
        raise SystemExit(1) from None
    print(f"aggregate_days_exported={count}")

