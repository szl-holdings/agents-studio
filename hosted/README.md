# Hosted Agents Studio

This is a separate public-account application. It does not expose the Windows
loopback server, import `.studio/`, use DPAPI credentials, or enable the existing
router-control Space's inference path.

## Boundaries

- Cloudflare remains the product front door; no apex or proof DNS changes.
- GitHub `szl-holdings/agents-studio` is canonical source.
- A dedicated HF Docker Space runs this application with `openid profile` only.
- A dedicated Cloudflare Worker holds the D1 binding. The browser never receives
  its shared credential; the storage API accepts fixed operations, not SQL.
- The model is unmodified Qwen2.5-0.5B-Instruct at the revision and safetensors
  hash in `engine.py`. It runs locally on CPU, proposes text, and cannot call tools.
- Public signup does not grant private SZL data, paid provider usage, or operator
  authority. User identity is a hash of verified issuer and subject.
- Each reservation consumes quota even if generation or completion is uncertain.
  It must never be automatically resent. Uncertain conversations remain blocked.
- Write records are explicitly unsigned. They are not signed authorization,
  model evaluation, energy evidence, or independent witness.

## Configuration

HF supplies `OAUTH_CLIENT_ID`, `OAUTH_CLIENT_SECRET`, `OPENID_PROVIDER_URL` and
`OAUTH_SCOPES` when `hf_oauth: true` is in the Space card. Request no additional
repository, billing, inference, or organization scopes.

Set `PUBLIC_ORIGIN` to the exact approved HTTPS origin; do not derive redirects
from forwarded Host headers. `SESSION_SECRET` must be independently generated
and kept in Space secrets. `STORE_URL` accepts the HTTPS workers.dev origin of
the dedicated storage Worker or the exact approved Worker Custom Domain origin
`https://agents-store.a-11-oy.com`. It must have no path, query, fragment, port,
or credentials. This source allowance does not configure DNS or prove the
Custom Domain is deployed. `STORE_SECRET` is its independently generated server
credential. Never use an existing local Studio or operator credential.

Worker bindings: `DB` to the dedicated D1 database, secret `STORE_SECRET`, an
independently generated `ADMISSION_SECRET`, and `SOURCE_REVISION` to the full
40-character admitted GitHub SHA. The deploy workflow injects it; any other value,
including an abbreviated SHA, is reported in `/healthz` and receipts as `UNKNOWN`.
`edge/wrangler.toml` records these bindings by name. The two secrets must be
distinct. Keep `ADMISSION_SECRET` stable
through each UTC day when rotating `STORE_SECRET`: the first admission records a
day-bound, non-account fingerprint of the admission key. An unexpected same-day
admission-key change holds new admissions even after account erasure; restore
the original key or wait until the next UTC day. Never rewrite the fingerprint
to clear a hold. Missing admission-key configuration leaves readiness and new
admissions unavailable, while authenticated completion, uncertainty recording,
and account erasure remain available for existing data.

For an identified new database, apply `edge/schema.sql` after permission and
billing readback. For an existing version-1 database, preserve and back it up,
then apply the additive migrations in order: `edge/migrations/0002_admission_key_epoch.sql`
and `edge/migrations/0003_run_model_bindings.sql`. The separate Snowflake
dispatch-custody source also adds `edge/migrations/0004_snowflake_dispatch_hold.sql`;
applying it creates an empty irreversible hold slot and does not enable an
export. For a database that already has migration 0002, apply the remaining
migrations in order. Stage the cutover: back up the identified
database, apply the migration, deploy the compatible Worker, deploy the Space
that sends and verifies model pins, then set the Worker's
`REQUIRE_MODEL_BINDING=1`. Read back a fresh authenticated `check` showing
`model_binding_enforced: true` and a new run with the expected declared pin.
Before the exact flag flip, old clients may still reserve unpinned runs; label
their model binding `UNKNOWN`. The new table does not backfill old runs, which
also remain `UNKNOWN`. Only the exact value `1` enables enforcement.
The `studio_schema` row remains version 1; readiness requires its version 1,
the separate admission epoch feature version 1, and the model-binding table.
If the old database has current UTC-day admissions without a known key epoch,
readiness and new admissions remain held until the next UTC day. Do not backfill
or reset those counters.
No live D1 migration or deployment is claimed by these source instructions.
Configure Worker logs/traces without recording Authorization, cookies, prompts
or responses; `edge/wrangler.toml` keeps observability off until such a
configuration is reviewed.
The store Worker is published only by the opt-in, reviewer-gated workflow in
[Deploy](#deploy), from the checked-in `edge/wrangler.toml`. It applies only the
`edge/migrations` files, never `edge/schema.sql`, and it does not publish the Space.

The optional `GET /v1/metrics` Worker endpoint exposes only existing global,
owner-free daily run and conversation admission counts for the preceding 30
completed UTC days. Missing days are omitted rather than backfilled. It
does not expose account identifiers, conversation content, latency, or errors.
An independently generated `METRICS_SECRET` is required and must differ from
the store and admission secrets; leave it unset to keep the endpoint disabled.
The endpoint only reads D1 and does not mint a receipt. Its `DECLARED` data are
not a model or authorization attestation. Configure a narrowly scoped exporter,
enforced request and cost limits, Snowflake service identity and grants,
retention, and appropriate public privacy disclosure before deploying or using
this endpoint to transfer metrics. The Worker retains its full D1 binding; the
separate secret limits only access through this endpoint, not D1 permissions.
This source change alone neither configures nor verifies a Snowflake connection.

The Snowflake operator entrypoint is currently **BLOCKED**. It exits nonzero
with `snowflake_transport_not_verified` before importing the connector, reading
credentials, fetching metrics, or writing anything. The scheduled workflow
retains that explicit failure when `SZL_METRICS_EXPORT_ENABLED=1`; the variable
only schedules an admission check and cannot enable a transport. The workflow
does not install the connector or pass secrets to this blocked entrypoint.

The former operator supplied a real connector through the exporter's synthetic
test hook and overrode the disabled-service declaration. A concurrency group
prevented overlap but supplied no durable uncertainty hold between runs. This
source repair closes that bypass; it does not reconcile a previous write or
make the export operational. See [Snowflake reconciliation and admission](SNOWFLAKE_RECONCILIATION.md)
for the three historical unknown outcomes, bounded readback procedure, and evidence
required before a separately reviewed production adapter can be admitted.

`snowflake_durable_dispatch.py` now contains the offline-qualified boundary for
that future adapter. A dedicated Worker `SNOWFLAKE_DISPATCH_SECRET` and matching
operator `STUDIO_SNOWFLAKE_DISPATCH_SECRET`, distinct from the Store, admission
and metrics credentials, protect
`POST /v1/snowflake-dispatch`. The first exact reservation stores the writer,
target, canonical aggregate payload, digests and request ID in D1 before a
provider connection can open. Worker `SNOWFLAKE_EXPORTER_REVISION` must bind the
payload to the exact admitted exporter source. Only the response that created the row carries
`dispatch_permitted: true`; a replay, competing request, timeout or lost response
cannot produce another permission. The schema exposes no update or delete path,
so the hold remains `HELD_UNKNOWN` across runner and Worker restarts.

The same module has an injected read-only reconciliation function. It reads the
hold, queries provider history by the held request ID and writer identity, and
compares destination rows with the held payload. It issues only bounded `SELECT`
statements, hashes query IDs in its returned report, never changes the hold, and
always returns `resend_permitted: false`. A matching observation is evidence for
operator review; it is not an automatic release or permission to send the next
window. The three historical attempts (2026-10-30, 2026-10-31 and 2026-11-01)
lack this request-bound record and remain UNKNOWN.

These components are source preparation only. The scheduled operator remains
hard-blocked before credentials, metrics, D1 reservation, connector import or
Snowflake I/O. No workflow calls the durable adapter, no dispatch secret is
configured here, and no D1 migration, provider query, export or live state
change is claimed. Admission still requires independent readback of D1 custody,
secret separation, real connector retry/query-tag behavior, the read-only
observer grants, destination identity, retention and spending controls.

`MODEL_ENABLED=1` starts the pinned CPU model loader. Otherwise generation is
UNAVAILABLE, never simulated. CPU Basic can sleep. The 45-second generation
timeout is best effort; it is not a hard execution deadline or SLA.

## Verification

```powershell
..\.venv-hosted\Scripts\python.exe -B -m unittest discover -s tests -p 'test_*.py' -v
node --test tests/test_store.mjs tests/test_studio_ui.mjs
node --check static/studio.js
```

CI (`.github/workflows/hosted.yml`, Ubuntu, Python 3.12.10) runs the same three
commands after installing the hash-locked `requirements/ci/hosted-contract.txt`,
which is compiled from this directory's `requirements.txt`. The job fails if a
line in `requirements.txt` is not an exact `==` pin or names a version the lock
did not install; regenerate the lock with the command in
`requirements/ci/hosted-contract.in`. It does not detect a dependency removed
from `requirements.txt` but still in the lock; the `hosted-image` job installs
`requirements.txt` itself.

Tests use synthetic identities, an in-memory SQLite D1 adapter, and injected
model outputs. They do not establish successful OAuth, provider deployment,
durability across restart, or model quality. The original root offline suites
must also pass before source publication.

`/healthz` is public liveness only. Without a valid signed-in session,
`/api/session` reports storage `UNKNOWN` without probing D1, and `/readyz`
returns 503 with the same `UNKNOWN` evidence. That anonymous 503 does not mean
the store is down. Signed-in `/api/session` and `/readyz` share a per-process
diagnostic budget (six checks per account and 30 total per minute, with at most
two in flight). An admitted `/readyz` performs a fresh schema and admission
check; only that fresh check can return 200. When the budget is exhausted,
storage is reported as `UNKNOWN`, no store call is made, and `/readyz` returns
503. This is a load guard, not a provider-enforced spending cap: multiple
replicas have separate budgets, and other authenticated store operations are
not covered.

### Redacted exporter diagnostics behind the hold

The blocked scheduled operator does not import a connector, read credentials,
fetch metrics or call the exporter. Its output remains
`snowflake_transport_not_verified`; diagnostic code does not authorize dispatch.

The exporter retains PR34's bounded first-failure phase, integer errno and
five-character SQLSTATE for offline synthetic transport tests and a future
separately qualified caller. Provider messages, SQL, query IDs, identities and
credentials remain excluded. `snowflake_write_uncertain` and no-retry behavior
are preserved. These diagnostics do not reconcile old runs or prove whether a
write committed. No current scheduled export reaches this diagnostic path.

## Owner end-to-end witness

`tools/verify_agents_studio.py` is run by the owner on their own Windows PC,
never in CI. It needs Python Playwright and the installed Microsoft Edge:

```powershell
py -3 -m pip install playwright
py -3 hosted\tools\verify_agents_studio.py
```

It opens a separate Edge window with its own profile under
`%LOCALAPPDATA%\szl-agents-studio-witness`, not the everyday browser profile.
The owner signs in to Hugging Face and authorizes the app in that window; the
other steps are automatic. It checks signed-in readiness (`/api/session` and
`/readyz`), the pinned model identity, one bounded generation, the run's model
binding, and persistence after a full page reload. Erasure of the account's
Agents Studio data runs only after the owner types `ERASE`; Enter skips it.
It prints and saves statuses only, never tokens, cookies or CSRF values. Its
receipt, `tools/agents-studio-witness-<UTC time>.json`, is written next to the
script and is git-ignored. A PASS is one owner-attested run, not independent
attestation, a model-quality evaluation or a release.

## Deploy

The storage Worker `szl-agents-studio-store` (`edge/store.mjs`) is deployed only by
[`.github/workflows/hosted-deploy.yml`](../.github/workflows/hosted-deploy.yml).
[`edge/wrangler.toml`](edge/wrangler.toml) is its reviewed configuration: the code,
compatibility date `2026-10-01`, `REQUIRE_MODEL_BINDING = "1"`, the `DB` binding to
D1 `szl-agents-studio-store` with `edge/migrations`, and secret names only. Wrangler
is pinned to an exact version by `edge/package-lock.json` and installed with `npm ci`.

What the workflow does:

1. `store/verify`, on every pull request and push that touches the store: the Worker
   contracts (`node --test`), the deploy-configuration and readback contracts (Python
   standard library only) and a credential-free `wrangler deploy --dry-run` that must
   upload exactly the committed `store.mjs`, byte for byte.
2. `store/deploy`, only for `main` (push or **Run workflow**) and only while the
   repository variable `SZL_STORE_DEPLOY_ENABLED` is `1`. It waits for the
   `cloudflare-production` environment's reviewers, prints the D1 Time Travel bookmark
   (the restore point), runs `wrangler d1 migrations apply DB --remote`, then
   `wrangler deploy --var SOURCE_REVISION:<full github.sha> --message <sha>`. Last,
   `store_deploy_readback.py` fails the job unless
   `https://agents-store.a-11-oy.com/healthz` declares that exact SHA with the D1
   binding and both required secrets configured. On push, `--strict` refuses to
   overwrite a Worker that was last deployed outside Wrangler (through the script
   API, or from the dashboard with conflicting settings); a manually dispatched run
   may reconcile it.

Owner setup, once:

1. Cloudflare dashboard, **Account API tokens**, create a custom token limited to this
   one account with exactly these account permissions: **Workers Scripts: Edit**,
   **D1: Edit** (bookmark and migrations) and **Account Settings: Read**. No zone
   permissions: the Custom Domain `agents-store.a-11-oy.com` stays attached outside
   `wrangler.toml`, and deploys neither change nor remove it. Optionally restrict client
   IP ranges and set an expiry.
2. GitHub, **Settings > Environments**, create `cloudflare-production` with yourself as
   required reviewer and deployment branches limited to `main`. Add the environment
   secrets `CLOUDFLARE_API_TOKEN` (the token above) and `CLOUDFLARE_ACCOUNT_ID`.
3. Leave the Worker secrets where they are. Set or rotate them only from `hosted/edge`
   with `npx wrangler secret put <NAME>`: `STORE_SECRET` and `ADMISSION_SECRET` are
   required (`wrangler deploy` refuses to upload without them; the migrations step
   before it does not check), `METRICS_SECRET` is optional, and
   `SNOWFLAKE_DISPATCH_SECRET` stays unset.
4. Keep `agents-store.a-11-oy.com/healthz` free of Monetization Gateway charges and bot
   challenges. Otherwise the readback fails with "edge returned 402 (x402 paywall)" or
   "403 managed challenge".
5. GitHub, **Settings > Secrets and variables > Actions > Variables**, set
   `SZL_STORE_DEPLOY_ENABLED` to `1`.
6. Run the first deployment from **Actions > Hosted store deploy > Run workflow** on
   `main`; a push-triggered first run stops at `--strict` because the current Worker
   was last uploaded through the API. That first run replaces the manually uploaded
   source of commit `d739c95b` with `main`, sets `SOURCE_REVISION` to the full SHA (until
   then receipts record `UNKNOWN`), applies `0004_snowflake_dispatch_hold.sql` (empty
   hold tables; 0002 and 0003 are idempotent if already present), leaves workers.dev
   enabled, turns preview URLs off, turns Workers observability (logs and traces) off
   and sets `REQUIRE_MODEL_BINDING` to `1`. The live values of that variable, of
   workers.dev and preview URLs, and of observability could not be read back for this
   change: confirm them before approving. The account audit log shows an API-token
   settings patch of this Worker at 2026-11-06 15:27:38Z, reported by another automated
   actor as enabling observability; the first run reverses it.

Local check without credentials:

```powershell
cd hosted\edge
npm ci --ignore-scripts
npx --no-install wrangler deploy --dry-run --outdir $env:TEMP\store-dry-run
```

Rollback: `npx wrangler rollback` restores the previous Worker version together with
its own `SOURCE_REVISION`; then revert the change on `main` so the next deploy agrees.
`npx wrangler d1 time-travel restore DB --bookmark=<bookmark from the run log>` rewinds
D1 and discards every write after that point, so use it deliberately.

Proof boundary: `/healthz` is the Worker's own DECLARED statement. A matching readback
shows that the Custom Domain serves a version carrying the deployed `SOURCE_REVISION`;
it is not independent attestation of the running code, storage readiness or model
behaviour. The SQL contracts run on SQLite stand-ins, not on D1.

## Release

1. Review, sign and push the smallest source change through the ordinary PR path.
2. Read back exact-head checks and merge normally. Refresh `origin/main`.
3. Run `python hosted/prepare_space.py --revision <main-sha> --output <fresh-path>`.
   The exporter reads committed allowlisted blobs, not private working files.
4. Verify the approved account's Worker access, plan and resource collision checks.
   Publish the dedicated store through [Deploy](#deploy) and read back its exact
   revision; configure secrets only with `wrangler secret put`, never through CI or logs.
5. Publish only the exported directory to the dedicated `SZLHOLDINGS` Space.
   Read back all blobs, the Space revision and application source identity.
6. Verify real login, cross-account isolation, persistence across restart,
   generation and quota exhaustion before enabling the approved product route.
7. Publish actual release evidence through the proof site's existing owner.
   A status HTTP 200 is not a completed release or certification.

The application exposes owner-bound live-data deletion and a public privacy
page. Deletion removes live D1 account rows and signs out the requesting
browser after a confirmed storage response. It does not close the Hugging Face
account or revoke another browser's 8-hour session. Day-specific pseudonymous
usage counters remain until the first successful storage write after that UTC
day, so erasure cannot reset daily admission. A separate, day-bound fingerprint
of the admission key is not account-linked and is retained without automatic
expiry. Rotating `STORE_SECRET` does not reset owner counters; changing
`ADMISSION_SECRET` during an admitted UTC day holds new admissions. D1 Time
Travel may retain recoverable history for 7 days (Free) or 30 days (Paid), so
deletion is not immediate from backups. There is no automatic age-based
retention or legal review in this source slice. Do not enable public signup
until privacy terms, real identity/persistence checks, plan and backup horizon
are confirmed live.

Prior art: [HF OAuth](https://huggingface.co/docs/hub/en/spaces-oauth),
[Authlib OIDC](https://docs.authlib.org/en/latest/client/flask.html),
[D1 transactional batches](https://developers.cloudflare.com/d1/worker-api/d1-database/#batch),
[Qwen model](https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct).
