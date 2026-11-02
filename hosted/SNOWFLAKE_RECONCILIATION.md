# Snowflake export: unresolved outcome and transport admission

The production operator is **BLOCKED** with
`snowflake_transport_not_verified` and exit code 1. This is a safety repair,
not successful export qualification. Neither a repository enable variable nor
configured credentials can override the hold. The offline injection interface
in `snowflake_metrics_export.py` and the local SQLite ledger are prototypes,
not approved production adapters or independently witnessed receipts.

## Historical evidence

[Run 41265043609](https://github.com/szl-holdings/agents-studio/actions/runs/41265043609)
at source `371e9548e5455920ace38806ef83253ac4550902` ran the operator on
2026-11-01. Its export step began at 02:24:30 UTC and printed
`snowflake_write_uncertain` at 02:24:34 UTC. The inspected run has no retained
workflow artifacts. The log contains no request ID, Snowflake query ID, exact
metrics payload, or payload digest. No Snowflake destination readback was
performed for this source repair.

The exception boundary covers connection establishment, cursor creation,
MERGE execution, and both cleanup calls. With autocommit enabled, even a cleanup
failure after a successful MERGE produces this message. The message therefore
does not prove that no statement was sent or that no rows were committed. It
also does not prove a write succeeded. The outcome remains **UNKNOWN**.

The former operator injected the real connector into a function whose default
runtime deliberately rejects an unverified transport. It also overwrote the
disabled-service declaration. It never reserved the prototype ledger. Workflow
concurrency prevents overlapping jobs but does not retain a dispatch hold
across the next schedule, a manual rerun, a crash, or connector-internal retries.
This repair removes that production bypass before any credential or remote I/O.
Enabled scheduled checks fail visibly; no provider exception is relabeled as
success, and no historical record is erased or marked reconciled.

## Bounded reconciliation by an authorized operator

Do not rerun this historical job, call the injectable exporter with a real
connector, or reconstruct and replay its payload from today's metrics endpoint.
The endpoint uses a moving window, and missing days are omitted. Retain the run
log and any existing operator/provider evidence in their approved private
custody. Inspect subsequent scheduled runs separately; do not assume the cited
run was the only attempt. This change does not change live account settings or
cancel queued jobs.

Use an already authorized read-only Snowflake session and existing bounded
compute controls. These are proposed read-only queries, not evidence that they
have run; this document does not authorize grants, role changes, warehouse
resizing, new resources, or a new write. If the session lacks visibility, retain
UNKNOWN and request the evidence from its existing custodian.

The historical job declared account `xuvuryr-kf96629`, service user
`SZL_STUDIO_METRICS_SERVICE`, role `SZL_STUDIO_METRICS_WRITER`, warehouse
`SZL_STUDIO_METRICS_WH`, and destination
`SZL_STUDIO_METRICS.TELEMETRY.DAILY_ADMISSIONS`. Bind all observations to the
actual account and object identity; names alone do not establish custody.

1. Locate all statements for that service user in the bounded interval below.
   Inspect candidate MERGE query text/bindings privately, including failed and
   successful candidates. Preserve query IDs, session IDs, execution status,
   error codes, row counts, time bounds and target binding. Do not publish raw
   query text, bindings or provider error messages.

   ```sql
   SELECT QUERY_ID, SESSION_ID, START_TIME, END_TIME, USER_NAME, ROLE_NAME,
          WAREHOUSE_NAME, QUERY_TYPE, EXECUTION_STATUS, ERROR_CODE,
          ROWS_INSERTED, ROWS_UPDATED, ROWS_DELETED
   FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
   WHERE USER_NAME = 'SZL_STUDIO_METRICS_SERVICE'
     AND START_TIME >= '2026-11-01 02:20:00 +00:00'::TIMESTAMP_TZ
     AND START_TIME <  '2026-11-01 02:30:00 +00:00'::TIMESTAMP_TZ
   ORDER BY START_TIME, QUERY_ID
   LIMIT 100;
   ```

   Hitting the result limit requires a narrower interval, not a completeness
   claim. Account Usage query history has documented retention and latency
   limits; absent or inaccessible history is not proof of non-dispatch. See
   [Snowflake QUERY_HISTORY](https://docs.snowflake.com/en/sql-reference/account-usage/query_history).

2. Read destination rows for the intended historical source/window, using the
   query below. The eligible window was 2026-10-02 through 2026-10-31 inclusive;
   the exact subset and counts sent are not preserved in the Actions log.

   ```sql
   SELECT METRIC_DAY, SOURCE_REVISION, SCHEMA_VERSION, IDEMPOTENCY_KEY,
          CONVERSATIONS_ADMITTED, RUNS_ADMITTED, EXPORTED_AT
   FROM SZL_STUDIO_METRICS.TELEMETRY.DAILY_ADMISSIONS
   WHERE SOURCE_REVISION = '371e9548e5455920ace38806ef83253ac4550902'
     AND SCHEMA_VERSION = 'szl.metrics.daily-admissions/v2'
     AND METRIC_DAY >= '2026-10-02'::DATE
     AND METRIC_DAY <  '2026-11-01'::DATE
   ORDER BY METRIC_DAY, EXPORTED_AT
   LIMIT 100;
   ```

   Compare each key with SHA-256 of the ASCII string
   `<source_revision>:szl.metrics.daily-admissions/v2:<YYYY-MM-DD>`. This is a
   row identity, not a payload digest or unique provider request ID. It excludes
   aggregate counts, the account and the target table. Check duplicate logical
   keys and count differences; do not deduplicate, overwrite, or delete them.
   Current rows can reflect later overlapping MERGEs and do not alone prove
   which run wrote them. Standard-table primary/unique constraints do not
   enforce uniqueness; see [Snowflake constraints](https://docs.snowflake.com/en/sql-reference/constraints-overview).

3. Match statement evidence, original payload evidence if retained, and row
   evidence. Record confirmed effects only to the extent that they are bound
   to the exact attempt. Missing payload/identity, contradictory rows, duplicate
   candidates, incomplete history or incomplete permissions remain UNKNOWN.
   A query that failed is not proof that another retry did not succeed.
   Preserve that distinction in an append-only reconciliation record reviewed
   by the existing authority. Reconciliation is not permission to resend.

## Requirements for a separately reviewed production adapter

- Persist the exact target, source/window, canonical payload digest, stable
  request identity and reservation before the first provider dispatch. Keep
  that reservation in admitted durable custody independent of an ephemeral
  Actions runner. Fail closed if custody is missing or unavailable. Preserve
  prior holds across restart, new source revisions and scheduler attempts.
- Prove the actual connector version's authentication, timeout and retry paths.
  A single Python `execute` call does not establish one wire dispatch. Bind the
  provider request/query identity to the reservation; a random new identity on
  retry or a row-level MERGE key is insufficient. Do not automatically resend
  an uncertain dispatch, including when no query ID was returned.
- Enforce one writer across all entrypoints and restarts with durable ownership
  and stale-writer rejection. An Actions concurrency group and a standard-table
  uniqueness declaration are not enough. Decide how overlapping daily windows
  and changed source revisions relate without losing historical uncertainty.
- Preserve the provider acknowledgement/query ID and validate terminal status
  and intended target/payload effects before recording success. `len(days)` is
  input size, not a readback receipt. Retain unknown outcomes after timeouts,
  lost responses, process crashes, evidence-write failure and cleanup failure;
  never clear a hold using an unbound local success flag.
- Qualify failure before send, after send/before response, after commit/before
  receipt, restart, duplicate delivery, concurrent/stale writers and failed
  reconciliation using the real transport in a separately authorized bounded
  environment. Offline mocks establish local control flow only. Independently
  read back service disablement, least-privilege grants, target, retention,
  secret custody, request limits and spending controls before admission.

The local SQLite reopen test proves only that its committed prototype hold is
retained on the same local disk. The D1 migration, authenticated hold endpoint,
first-response-only dispatch adapter and read-only reconciliation function now
encode the required no-resend control in source. They are not invoked by the
blocked operator and have not been migrated, configured or observed live. The
historical attempt predates their request-bound payload record and remains
UNKNOWN. No production custody, provider idempotency, successful export,
independent approval or live account change is claimed.
