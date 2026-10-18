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
independently generated `ADMISSION_SECRET`, and `SOURCE_REVISION` to the admitted
GitHub SHA. The two secrets must be distinct. Keep `ADMISSION_SECRET` stable
through each UTC day when rotating `STORE_SECRET`: the first admission records a
day-bound, non-account fingerprint of the admission key. An unexpected same-day
admission-key change holds new admissions even after account erasure; restore
the original key or wait until the next UTC day. Never rewrite the fingerprint
to clear a hold. Missing admission-key configuration leaves readiness and new
admissions unavailable, while authenticated completion, uncertainty recording,
and account erasure remain available for existing data.

For an identified new database, apply `edge/schema.sql` after permission and
billing readback. For an existing version-1 database, preserve and back it up,
then apply only the additive `edge/migrations/0002_admission_key_epoch.sql`.
The `studio_schema` row remains version 1; readiness requires its version 1 and
the separate admission epoch feature version 1. If the old database has current
UTC-day admissions without a known key epoch, readiness and new admissions
remain held until the next UTC day. Do not backfill or reset those counters.
No live D1 migration or deployment is claimed by these source instructions.
Configure Worker logs/traces without recording Authorization, cookies, prompts
or responses.
Use the current `cf` CLI and official configuration schema for deployment;
this repository does not introduce a second automatic publisher.

`MODEL_ENABLED=1` starts the pinned CPU model loader. Otherwise generation is
UNAVAILABLE, never simulated. CPU Basic can sleep. The 45-second generation
timeout is best effort; it is not a hard execution deadline or SLA.

## Verification

```powershell
..\.venv-hosted\Scripts\python.exe -B -m unittest discover -s tests -p 'test_*.py' -v
node --test tests/test_store.mjs tests/test_studio_ui.mjs
node --check static/studio.js
```

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

## Release

1. Review, sign and push the smallest source change through the ordinary PR path.
2. Read back exact-head checks and merge normally. Refresh `origin/main`.
3. Run `python hosted/prepare_space.py --revision <main-sha> --output <fresh-path>`.
   The exporter reads committed allowlisted blobs, not private working files.
4. Verify the approved account's Worker access, plan and resource collision checks.
   Publish the dedicated store and schema; configure both secrets without logs.
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
