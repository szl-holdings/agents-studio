# OpenAI Agents API curl application

Private source: [szl-holdings/agents-studio](https://github.com/szl-holdings/agents-studio).
The GitHub workflow runs offline Windows contracts with synthetic credentials.
Source publication and green CI do not enable hosted inference. Only the opt-in,
reviewer-gated `Hosted store deploy` workflow publishes the hosted storage Worker
([hosted/README.md](hosted/README.md#deploy)).

<!-- szl:header v1 -->
<!-- Native identity: szl-brand@4309f6fce89179331b0b3ed8c99494e76c5be256; Orbit v2 icon. -->
<p>
  <a href="https://github.com/szl-holdings">
    <img src="https://raw.githubusercontent.com/szl-holdings/szl-brand/4309f6fce89179331b0b3ed8c99494e76c5be256/kit/logos/orbit-v2/suite/szl-icon-32.svg" alt="SZL Holdings — Orbit v2" width="32" height="32" />
  </a>
</p>

**Control before action. Evidence after.**

[Product](https://a-11-oy.com) · [Proof](https://a11oy.net) · [GitHub organization](https://github.com/szl-holdings)
<!-- /szl:header -->

## Agents Studio

Double-click `Start-Agents.cmd` to open the local browser workspace. It uses the
existing Windows encrypted credential store and the original `New agent`
configuration. No Node install, Python package install, or API key in the browser
is required. The command-line interface below remains available.

Studio provides saved conversations with paginated history, streamed replies,
search/tool activity, copy and Markdown export, session details, a Stop control,
connection and provider-outcome recovery, and a responsive desktop/mobile layout.
Opening a conversation reads saved provider state; a new conversation does not
run or incur inference until you send its first message. Public-research starters
fill the composer for review before submission.

The server binds only to `127.0.0.1`. Its random local connection capability is
passed in a URL fragment, then removed from the address bar and retained in that
browser tab's session storage. It is not an OpenAI API key. Do not share the
launch URL or expose this server through a tunnel. API credentials stay in the
server process and existing encrypted store; they are not sent to the page.
Host/origin checks, a strict content policy, static-file allowlisting, and
text/DOM-only rendering protect the local interface. Icons and Markdown parsing
are bundled locally with their licenses, not loaded from a CDN.

Conversation records are private local data in ignored `.studio/`; they can
contain prompts and model output. Do not publish that folder, the state files,
runtime connection metadata, or credentials. A local operation record is not a
signed SZL receipt, model evaluation, or authorization proof. Copy/export are
explicit user actions and may include conversation contents.

One shared profile lock serializes CLI and Studio operations. Before sending,
Studio saves a client submission UUID and exact request in the browser tab's
session storage. The server binds that UUID to the action, session, and message
hash on disk before dispatch. An identical retry returns the original job;
changed content with the same UUID is rejected. Bindings survive restarts and
the pruning of old job transcripts. The ledger has a 10,000-entry bound; it
refuses new submissions when full rather than forgetting earlier identities.

After a lost acknowledgement or reload, recovery uses local GET requests only.
If no acceptance record is found, **Retry saved submission** explicitly reuses
the original UUID. No automatic message resend occurs. A missing transcript is
not treated as permission to replay an accepted submission. The saved request
contains prompt text, so it is private like the conversation itself. Its browser
copy is removed after a known terminal local job; server bindings remain.

**Stop** sends the documented provider cancellation event through the existing
CLI process, which retains its profile lock. It becomes available only after
that process observes the active root turn. A requested or uncertain cancellation
is not labelled cancelled. An exact terminal provider event or turn readback
confirms cancellation, preserves partial output, and reconciles a matching pending
follow-up under the same lock. Completed work wins a completion/cancel race.
The provider endpoint has no turn selector: another external project writer
can race the latest-turn check, so cross-client atomic cancellation is not claimed.

**Check provider outcome** inspects an uncertain original run using provider GET
requests under the shared profile lock. It never resends a message or cancels a
turn. Follow-up recovery requires an already observed exact root-turn ID and
matching saved input. A previous-turn boundary and identical prompt alone cannot
distinguish another writer's request, so an unobserved follow-up stays unavailable.
New Studio starts also bind an opaque nonce and original job ID in
provider session metadata, so identical prompts from another writer cannot be
adopted as this run. Legacy starts without that binding and without an observed
session ID remain unavailable for automatic reconciliation.

Recovery verifies the exact session, root turn, original input, and terminal
outcome before clearing a matching pending marker. It checks saved tool results
as well as answer text; a completed turn with a failed tool is not reported as a
successful job. Ambiguous, incomplete, out-of-bounds, and missing records stay
unresolved. Recovery allows at most 10 pages per listing and 64 provider reads,
and stops admitting reads after 180 seconds. One already-running request can
take up to the existing transport's 100-second subprocess limit. No match within
those bounds is not proof that the request never ran.

**Load earlier messages** retrieves older saved items without running inference.
Pages are deduplicated by item ID, scroll position is preserved, and missing or
repeated cursors stop pagination. The loaded-item count reports when earlier
history remains or completeness is unknown. Closing the browser does not cancel
a provider turn; reopen the workspace to inspect it. This local application is
separate from the hosted router control interface, whose provider inference
remains unconfigured.

The **Connections** view shows the local API credential state and an explicit
**Check connection** for the hosted SZL control platform. Nothing is fetched
automatically on startup or tab selection. The local capability-protected
`GET /api/cloud-status` reads only `/api/source`, `/readyz`, and
`/readyz/inference` at the fixed public control origin. It sends no credentials,
ignores proxy settings, does not follow redirects, bounds time and response size,
and writes no receipt, cache, conversation, or job record.

Source comparison checks the reported revision and controlled-file metadata
against the pinned release. It is not signature verification or an independent
runtime attestation. Control readiness and inference configuration are separate;
even a positive configuration response does not establish live inference. The
view never enables egress, accepts an arbitrary endpoint, or reuses the local
OpenAI key for cloud hosting. Missing or malformed observations remain UNKNOWN
or UNAVAILABLE, and a failed refresh clears the previous displayed result.

Design references consulted October 7, 2026:
[run and continue sessions](https://developers.openai.com/api/docs/guides/agents-api/sessions),
[events and saved items](https://developers.openai.com/api/docs/guides/agents-api/sessions/events),
and [ChatKit customization](https://developers.openai.com/api/docs/guides/chatkit-themes).
Recovery and cancellation also follow
[errors and recovery](https://developers.openai.com/api/docs/guides/agents-api/errors).
Paginated history and start identity use the documented
[saved items API](https://developers.openai.com/api/reference/python/resources/beta/subresources/agents/subresources/sessions/subresources/items/methods/list)
and [session metadata](https://developers.openai.com/api/reference/resources/beta/subresources/agents/subresources/sessions/methods/create).
The implementation reuses this application's curl transport; it does not add a
ChatKit service or replace the original saved agent.

## Original `New agent` profile

[`new_agent.json`](new_agent.json) contains the seven fields of the requested reusable **New agent** definition: live `web_search`, multi-agent disabled, `gpt-6-astra`, empty custom instructions, medium reasoning, and text output. [`new_agent.py`](new_agent.py) sends that definition with `curl.exe` to `POST /v1/agents`, saves the **returned** agent ID locally, and starts a streamed session using that ID. It uses `environment: { "type": "none" }`, which is suitable for built-in web search and needs no runtime image or executor. The first session sends `What can you help me research today?` unless you supply another message. The saved definition does not contain credentials.

Use Windows PowerShell 5.1 or PowerShell 7, Python 3.10 or later, and `curl.exe` 7.76 or later. Set `OPENAI_API_KEY` to an actual secret for project `proj_M074YvE258sr0Rg13cNmJn8j`; the key needs Agents read/write and Responses write access. Do not type the angle-bracket placeholder from documentation. The New agent launcher prompts with hidden input when `OPENAI_API_KEY` is missing or still contains a placeholder:

```powershell
cd C:\Users\steph\szl-work\openai-agents-api-demo
.\run-new-agent.ps1 start
.\run-new-agent.ps1 send -Message 'What research topics can you help with?'
.\run-new-agent.ps1 status -Items
```

You can also set `OPENAI_API_KEY` from your secret manager before running the launcher; then it leaves your configured value in place. When that variable is absent or contains a placeholder, the launcher checks the previously configured current-user Windows DPAPI store at `%LOCALAPPDATA%\SZL\Secrets\openai-default-estate-agents-20261003.dpapi`. If that store exists, it decrypts the key only into the temporary process environment. If no store exists, it prompts with hidden input. Temporary credentials are removed or the prior process value is restored in `finally`, including after failure. The encrypted store stays outside this application and is bound to the Windows user; it is not portable to another account or computer.

**This configured computer can run without another key prompt:**

```powershell
.\run-new-agent.ps1 create -NonInteractive
.\run-new-agent.ps1 start -NonInteractive
.\run-new-agent.ps1 send -NonInteractive -Message 'Use live web search to find the official Agents API guide and cite it.'
.\run-new-agent.ps1 status -NonInteractive -Items
```

`-NonInteractive` stops with a useful error if neither an environment key nor a usable encrypted store is available. To select another existing current-user DPAPI store, pass `-KeyFile 'C:\path\credential.dpapi'`; an already configured `OPENAI_API_KEY` takes priority. A store that cannot be decrypted stops the run. The launcher does not create a key, change a saved agent, or persist plaintext credentials.

Use `run-new-agent.ps1` for the requested **New agent** profile. The older `run.ps1` starts the separate SZL Estate Evidence Scout profile below. Quoted, Unicode, and multiline `-Message` values are carried through a temporary UTF-8 file, avoiding Windows PowerShell 5.1's native argument quoting behavior; that file is deleted after success or failure. Streamed output is also UTF-8, and the launcher restores the caller's Python and console encoding settings afterward. The Python CLI also accepts `--message-file PATH` with UTF-8 input, mutually exclusive with `--message`.

`start` first checks for an identical saved agent in the project, creates one with `POST /agents` when none exists, persists its returned ID in ignored `.new-agent-state.json`, and reuses it for later sessions. `create` performs just that create-or-verify step without starting a session. If a creation response is lost, run `create` again: it scans the project's saved agents and recovers one exact match. If it finds multiple matches or cannot finish the listing, it stops before creating another. `send` and `status` use the latest session ID from the separate New agent state file, or accept `-SessionId`. Streaming prints text, event status, and tool activity; unexpected required actions and failed tools cause a nonzero exit. Follow-up messages use the same subscription, idempotency, and recovery flow described below for the Scout profile.

The direct HTTP flow is also available from PowerShell. The reusable definition is sent exactly as written, and the session uses the ID returned by the create response:

```powershell
$curlConfig = @"
header = "Authorization: Bearer $env:OPENAI_API_KEY"
header = "OpenAI-Project: proj_M074YvE258sr0Rg13cNmJn8j"
header = "OpenAI-Beta: agents=v1"
header = "Content-Type: application/json"
"@
$agent = $curlConfig | curl.exe -sS --fail-with-body --config - --request POST `
  'https://api.openai.com/v1/agents' --data-binary '@new_agent.json' | ConvertFrom-Json
$agentId = $agent.id
$body = @{ agent_id = $agentId; environment = @{ type = 'none' }; input = 'What can you help me research today?'; stream = $true } |
  ConvertTo-Json -Depth 8 -Compress
$bodyFile = Join-Path $env:TEMP ("new-agent-start-{0}.json" -f [guid]::NewGuid())
try {
    [System.IO.File]::WriteAllText($bodyFile, $body, [System.Text.UTF8Encoding]::new($false))
    $curlConfig | curl.exe -N -sS --fail-with-body --config - --request POST `
      'https://api.openai.com/v1/agents/sessions' -H 'Accept: text/event-stream' `
      --data-binary "@$bodyFile"
} finally {
    Remove-Item -LiteralPath $bodyFile -ErrorAction SilentlyContinue
}
```

The script handles session IDs, completed versus failed turns, tool errors, and follow-up submissions automatically. See the [Agents API configuration guide](https://developers.openai.com/api/docs/guides/agents-api/configuration) for saved agents and the [create agent API reference](https://developers.openai.com/api/reference/typescript/resources/beta/subresources/agents/methods/create) for `POST /agents`.

The original workstation retains private live-run and execution records outside
Git. They are intentionally excluded from this source publication along with
session state, transcripts, credentials, and local operation receipts.

## SZL Estate Evidence Scout profile

This Python CLI starts and continues a session from the saved **SZL Estate Evidence Scout** agent (`agent_b37d41c0efa3435e84b94b172d10e4ed80563c721f3a4481b8`) in OpenAI project `proj_M074YvE258sr0Rg13cNmJn8j`. Every Agents HTTP request is made by `curl.exe`; the application does not use the OpenAI SDK or require third-party Python packages.

[`agent.json`](agent.json) supplies the current **session overrides** for `model`, `instructions`, `reasoning`, and `text`. It includes the unsaved edits in the session without updating the saved agent. The request includes both `agent_id` and `agent`; omitted saved settings, including tools and multi-agent configuration, are copied into the session. The app reads back the saved agent first and fails if its name differs, its tool list is anything other than one `web_search` tool, or multi-agent is enabled. [OpenAI's configuration guide](https://developers.openai.com/api/docs/guides/agents-api/configuration) describes how these overrides work.

The environment is `{ "type": "none" }`. The agent only needs the built-in web search tool to audit public resources; it does not need a shell, files, an executor, or a provisioned sandbox. OpenAI's [architecture guide](https://developers.openai.com/api/docs/guides/agents-api/architecture) describes `none` for agents that use external tools without local compute, and its [web search guide](https://developers.openai.com/api/docs/guides/agents-api/tools/web-search) shows this pairing. A session with this environment requires initial input.

## Setup

1. Use PowerShell 7, Python **3.10 or later**, and `curl.exe` **7.76 or later** (`--fail-with-body` is required). Check with `py -3 --version` and `curl.exe --version`.
2. In [OpenAI Platform API keys](https://platform.openai.com/api-keys), choose project `proj_M074YvE258sr0Rg13cNmJn8j`. Use an application API key that can access this saved agent and model and has `api.agents.read`, `api.agents.write`, and `api.responses.write`. The [Agents API quickstart](https://developers.openai.com/api/docs/guides/agents-api/quickstart) documents these scopes and the required `OpenAI-Beta: agents=v1` header for cURL.
3. Supply that key to the current PowerShell process as `OPENAI_API_KEY` using your normal secret manager or the masked prompt below. Check only that the variable is present with `Test-Path Env:OPENAI_API_KEY`; do not print it. The launcher reads this variable and never asks for, saves, or copies a key.

```powershell
cd C:\Users\steph\szl-work\openai-agents-api-demo
$env:OPENAI_API_KEY = Read-Host 'OpenAI project API key' -MaskInput
Test-Path Env:OPENAI_API_KEY
.\run.ps1 start
```

The first `start` sends a bounded, read-only request to identify public GitHub `szl-holdings` and Hugging Face `SZLHOLDINGS` entry points and a small sample of directly observed resources. It asks for source URLs, observation times, access scope, explicit unknowns, and the next safe public read. You can supply another initial user message with `-Message`.

```powershell
.\run.ps1 start -Message 'Inventory the public szl-holdings GitHub organization and SZLHOLDINGS Hugging Face profile. Paginate where possible, cite direct URLs, and label inaccessible or unverified counts.'
.\run.ps1 send -Message 'For the cited sample, separate source, CI, Hub publication, and runtime evidence.'
.\run.ps1 status -Items
```

`start` creates a new session each time; `send` and `status` use the latest locally saved session ID. Use `-SessionId 'sess_...'` with `send` or `status` to select another session. The local `.agent-state.json` stores the selected project, saved agent ID, session ID, and a pending submission record when needed; it is ignored by Git. It never stores the API key. `start` prints the API-returned session ID, which is the identifier to retain for later turns. If authentication, project access, saved-agent lookup, or session creation fails, correct that failure before sending a follow-up.

```powershell
.\run.ps1 send -SessionId 'sess_...' -Message 'Summarize the remaining unknowns.' -RawEvents
.\run.ps1 status -SessionId 'sess_...' -Items
```

The CLI streams text and events, shows built-in tool activity, and waits for the root turn's `completed`, `failed`, or `cancelled` event. An idle session or a closed stream alone is not counted as success. An incomplete or failed web search makes the command exit nonzero even if the turn completes, so treat that answer as degraded. If a function result or environment connection is required, it reports `required_actions` and exits nonzero; it does not execute an unexpected tool call. The saved agent is expected to have only built-in web search, which OpenAI handles. [Running sessions](https://developers.openai.com/api/docs/guides/agents-api/sessions) explains turn outcomes and required actions; [session events and items](https://developers.openai.com/api/docs/guides/agents-api/sessions/events) documents the event stream.

If the stream disconnects, run `status -Items` to inspect saved work before sending anything else. A follow-up submission is recorded with its idempotency key before POSTing. Retry the **same message** to reuse that key. If you have inspected the uncertain turn and deliberately need a different message, use `-NewSubmission`. You can provide `-IdempotencyKey` for a manually coordinated retry. A pending message can temporarily appear in `.agent-state.json`, so keep that file private. [OpenAI's session guide](https://developers.openai.com/api/docs/guides/agents-api/sessions) recommends subscribing before a follow-up POST, using an idempotency key, and recovering from saved state when a stream is missed.

Recovered answers are checked against the saved tool-call items too. An incomplete or failed search, including a saved error on a completed tool item, keeps the command nonzero and the pending submission in state. Repeating the same message retrieves that outcome without posting a duplicate. After reviewing a degraded answer, use `-NewSubmission` for a deliberate new turn.

If the first session creation disconnects before its ID is observed, the server may have created a session that the CLI cannot identify. Check the project's sessions in OpenAI Platform before repeating `start` when avoiding a duplicate matters.

## Direct `curl.exe` requests

The app uses these same endpoints. Run the commands below in a PowerShell session with a real `OPENAI_API_KEY`. The configuration goes to curl on standard input, so the secret does not appear in curl's process arguments. These examples are for inspecting the HTTP flow; the CLI above parses events, persists IDs and retry keys, and handles errors.

```powershell
$agentId = 'agent_b37d41c0efa3435e84b94b172d10e4ed80563c721f3a4481b8'
$curlConfig = @"
header = "Authorization: Bearer $env:OPENAI_API_KEY"
header = "OpenAI-Project: proj_M074YvE258sr0Rg13cNmJn8j"
header = "OpenAI-Beta: agents=v1"
header = "Content-Type: application/json"
"@

# Verify the saved agent. No POST /v1/agents and no saved-agent update is needed.
$curlConfig | curl.exe -sS --fail-with-body --config - `
  "https://api.openai.com/v1/agents/$agentId"

# Include only the four unsaved fields; the saved tool and multi-agent fields carry over.
$overrides = Get-Content .\agent.json -Raw | ConvertFrom-Json
$startBody = @{
  agent_id = $agentId
  agent = $overrides
  environment = @{ type = 'none' }
  input = 'Begin a bounded, read-only public evidence scan of GitHub szl-holdings and Hugging Face SZLHOLDINGS. Cite each observed resource and mark unverified totals UNKNOWN.'
  stream = $true
} | ConvertTo-Json -Depth 16 -Compress
$startFile = Join-Path $env:TEMP ("agents-start-{0}.json" -f [guid]::NewGuid())
[System.IO.File]::WriteAllText($startFile, $startBody, [System.Text.UTF8Encoding]::new($false))
$curlConfig | curl.exe -N -sS --fail-with-body --config - --request POST `
  'https://api.openai.com/v1/agents/sessions' -H 'Accept: text/event-stream' `
  --data-binary "@$startFile"
Remove-Item -LiteralPath $startFile
```

Read the session ID from the `agent.session.created` event. For a later message, subscribe to events **before** POSTing input. Use two PowerShell windows, each with `OPENAI_API_KEY` and `$curlConfig` set as above:

```powershell
# Window A: keep this stream open while Window B submits the message.
$sessionId = 'sess_...'
$curlConfig | curl.exe -N -sS --fail-with-body --config - -H 'Accept: text/event-stream' `
  "https://api.openai.com/v1/agents/sessions/$sessionId/events?stream=true"
```

```powershell
# Window B: use a fresh key for this logical message; retain it if the outcome is uncertain.
$sessionId = 'sess_...'
$retryKey = [guid]::NewGuid().ToString()
$sendBody = @{
  events = @(@{
    type = 'agent.session.input.message'
    input = @(@{ role = 'user'; content = @(@{ type = 'input_text'; text = 'What evidence remains missing?' }) })
  })
} | ConvertTo-Json -Depth 12 -Compress
$sendFile = Join-Path $env:TEMP ("agents-send-{0}.json" -f [guid]::NewGuid())
[System.IO.File]::WriteAllText($sendFile, $sendBody, [System.Text.UTF8Encoding]::new($false))
$curlConfig | curl.exe -sS --fail-with-body --config - --request POST `
  "https://api.openai.com/v1/agents/sessions/$sessionId/events" `
  -H "Idempotency-Key: $retryKey" --data-binary "@$sendFile"
Remove-Item -LiteralPath $sendFile
```

Inspect remote state and saved output after an interrupted stream:

```powershell
$curlConfig | curl.exe -sS --fail-with-body --config - `
  "https://api.openai.com/v1/agents/sessions/$sessionId"
$curlConfig | curl.exe -sS --fail-with-body --config - `
  "https://api.openai.com/v1/agents/sessions/$sessionId/items?order=desc&limit=100"
```

Saved items are paginated; use the returned `last_id` as `after` for older pages. [Managing sessions](https://developers.openai.com/api/docs/guides/agents-api/sessions/manage) and [session events and items](https://developers.openai.com/api/docs/guides/agents-api/sessions/events) give the recovery details. If a manual curl request fails before cleanup, remove its temporary JSON payload after inspecting it.

## Local verification

```powershell
py -3 -m unittest discover -s tests -v
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tests\test_run_new_agent.ps1
pwsh.exe -NoProfile -File tests\test_run_new_agent.ps1
```

The local tests use an HTTP stand-in and a real `curl.exe`; no OpenAI key is needed. A live session requires a project key and access to the saved agent. Local tests do not establish that a live session ran or that its estate findings are complete.

Historical live-start and follow-up records remain private to the original
workstation and are not part of the repository or its CI evidence.

## Official OpenAI documentation

- [Agents API overview](https://developers.openai.com/api/docs/guides/agents-api/overview)
- [Agents API quickstart](https://developers.openai.com/api/docs/guides/agents-api/quickstart)
- [Environment architecture](https://developers.openai.com/api/docs/guides/agents-api/architecture)
- [Configuring and reusing agents](https://developers.openai.com/api/docs/guides/agents-api/configuration)
- [Running and continuing sessions](https://developers.openai.com/api/docs/guides/agents-api/sessions)
- [Session events and items](https://developers.openai.com/api/docs/guides/agents-api/sessions/events)
