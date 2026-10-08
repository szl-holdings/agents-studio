<#
Run the saved-agent Agents API demo. Set OPENAI_API_KEY in this process before
calling this launcher; app.py reads it and sends HTTP requests through curl.
#>
[CmdletBinding()]
param(
    [ValidateSet('start', 'send', 'status')]
    [string]$Action = 'start',
    [string]$Message,
    [string]$SessionId,
    [string]$IdempotencyKey,
    [switch]$NewSubmission,
    [switch]$RawEvents,
    [switch]$Items
)

$ErrorActionPreference = 'Stop'
try {
    if ([string]::IsNullOrWhiteSpace($env:OPENAI_API_KEY)) {
        throw 'Set OPENAI_API_KEY for project proj_M074YvE258sr0Rg13cNmJn8j in this PowerShell process before running.'
    }

    $arguments = @('-3', (Join-Path $PSScriptRoot 'app.py'), $Action)
    if ($PSBoundParameters.ContainsKey('Message')) {
        $arguments += @('--message', $Message)
    }
    if ($PSBoundParameters.ContainsKey('SessionId')) {
        $arguments += @('--session-id', $SessionId)
    }
    if ($PSBoundParameters.ContainsKey('IdempotencyKey')) {
        $arguments += @('--idempotency-key', $IdempotencyKey)
    }
    if ($NewSubmission) {
        $arguments += '--new-submission'
    }
    if ($RawEvents) {
        $arguments += '--raw-events'
    }
    if ($Items) {
        $arguments += '--items'
    }

    & py @arguments
    exit $LASTEXITCODE
} catch {
    [Console]::Error.WriteLine("Error: $($_.Exception.Message)")
    exit 1
}
