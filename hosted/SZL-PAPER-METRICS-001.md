# SZL-PAPER-METRICS-001: bounded aggregate telemetry

Status: DECLARED design; the Snowflake object setup, key-pair identity check,
and rollback probe are MEASURED. The hosted exporter and public route remain
BLOCKED until their secret custody, transport, and deployment are independently
verified.

## Purpose

Agents Studio exports only owner-free daily admission counts. A row contains a
UTC day, two clipped counters, the admitted source revision, the schema
revision, a deterministic idempotency key, and the export timestamp. It never
contains prompts, responses, account identifiers, IP addresses, or model
content.

The contract is intentionally a daily delta: each row describes one closed UTC
interval. A consumer must reject duplicate days for the same source and schema,
and must treat a missing day as unknown rather than inventing zero.

## Identity and replay

For a source revision r, schema revision s, and day d, the exporter computes:

~~~text
idempotency_key = SHA256(r || ":" || s || ":" || d)
~~~

Snowflake merges on (metric_day, source_revision, schema_version). The
idempotency key is stored as evidence for reconciliation; it is not treated as
an authorization token. A single writer lease is required around the merge.
An uncertain write is retained as UNKNOWN; the exporter never retries it
automatically because the first request may already have committed.

## Access boundary

The dedicated role SZL_STUDIO_METRICS_WRITER receives only:

~~~text
USAGE  on database SZL_STUDIO_METRICS
USAGE  on schema SZL_STUDIO_METRICS.TELEMETRY
SELECT, INSERT, UPDATE on table DAILY_ADMISSIONS
USAGE  on warehouse SZL_STUDIO_METRICS_WH
~~~

It has no OPERATE, MONITOR, MODIFY, OWNERSHIP, account-admin, or
cross-database privilege. The service identity is enabled with a locally
retained encrypted RSA key and the public-key fingerprint is read back from
Snowflake and matched locally. The private key is not part of this repository
or any receipt.

## Reliability and privacy choices

The design takes four ideas from the reviewed literature and turns them into
SZL-specific rules:

1. Explicit interval semantics: daily rows carry a stable interval boundary
   instead of relying on an implicit cumulative counter.
2. Durable identity: source revision, schema revision, and deterministic
   idempotency are persisted with the row.
3. Fail-closed transport: malformed, redirected, oversized, or ambiguous
   responses stop before Snowflake; a disabled service identity returns
   snowflake_service_user_disabled.
4. Privacy first: only aggregate counts cross the boundary. Differential
   privacy is a future, separately budgeted option; it is not silently added
   to a production metric stream.

These rules are original SZL synthesis. They do not copy implementation code
or text from the cited work.

## Evidence boundary

The following sources informed the design:

- [Snowflake user management](https://docs.snowflake.com/en/user-guide/admin-user-management)
  for SERVICE users, disabled users, and role assignment.
- [Snowflake access-control overview](https://docs.snowflake.com/en/user-guide/security-access-control-overview)
  and [privilege reference](https://docs.snowflake.com/en/user-guide/security-access-control-privileges)
  for RBAC and the separation of USAGE from warehouse control privileges.
- [Snowflake key-pair authentication](https://docs.snowflake.com/en/user-guide/key-pair-auth)
  for the future encrypted-key and rotation gate.
- [OpenTelemetry metrics data model](https://opentelemetry.io/docs/specs/otel/metrics/data-model/)
  for explicit temporality and reset/gap handling.
- [OpenTelemetry Collector exporter helper](https://github.com/open-telemetry/opentelemetry-collector/blob/main/exporter/exporterhelper/README.md)
  and [resiliency guidance](https://opentelemetry.io/docs/collector/resiliency/)
  for bounded queues and crash-safe delivery.
- [An Architecture for Privacy-Preserving Telemetry Scheme](https://arxiv.org/abs/2507.06350)
  for the separation of local privacy protection from the transport boundary.

The bounded identity check authenticated as `SZL_STUDIO_METRICS_SERVICE` with
role `SZL_STUDIO_METRICS_WRITER`. A transaction probe inserted one synthetic
row and rolled it back; the before and after counts were both zero. The
dedicated X-Small warehouse was resumed only for that probe and is now
suspended with auto-resume disabled. These checks prove Snowflake identity and
transaction behavior, not a deployed Agents Studio export or a public route.

The sources are references and constraints, not proof that Agents Studio is
deployed or that Snowflake has accepted a live export.
