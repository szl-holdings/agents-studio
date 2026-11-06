# SZL Agents Studio

This private repository contains the local Windows Agents Studio application.
The source publication is not a cloud deployment or an inference qualification.
Follow the machine SZL Holdings instructions and the canonical doctrine in
`szl-holdings/a11oy/AGENTS.md` and `szl-holdings/.github`.

- Preserve the original New agent and separate Scout profiles.
- Keep the server loopback-only, capability-protected, and deny-by-default.
- Never publish credentials, `.studio/`, state files, transcripts, live-run
  records, private operation receipts, or encrypted DPAPI material.
- A GET must not write jobs, receipts, cache files, or profile state.
- Keep source equality, control readiness, inference configuration, provider
  outcomes, authorization, and independent attestation separate.
- Never resend an uncertain submission automatically or clear its pending
  marker without exact provider-bound evidence.
- Cloud observation uses only the fixed public control origin. It must not
  forward credentials, enable egress, or configure a provider.
- Use signed commits, exact-head CI, ordinary pull requests after initial
  repository bootstrap, and read back publication results. Do not force-push
  or bypass protected rules. Do not fabricate an independent approval.

## Verification

On Windows, run the offline suites before publishing code changes:

```powershell
py -3 -B -m unittest discover -s tests -v
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tests/test_run_new_agent.ps1
pwsh.exe -NoProfile -File tests/test_run_new_agent.ps1
node --check studio/studio.js
```

The tests use local stand-ins and synthetic credentials. A live provider call
is a separate explicit operation; test CI must not receive production credentials.
The one exception is the opt-in `store/deploy` job in
`.github/workflows/hosted-deploy.yml`: it receives a Workers-scoped Cloudflare token
only from the reviewer-protected `cloudflare-production` environment and must read
back the exact deployed revision (`hosted/README.md#deploy`).
Browser checks and source-bound local release records are separate evidence,
not proof that a remote deployment or model evaluation succeeded.
