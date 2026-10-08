<# Run the reusable New agent profile with curl-backed HTTP calls. #>
[CmdletBinding()]
param(
    [ValidateSet('create', 'start', 'send', 'status', 'studio')]
    [string]$Action = 'start',
    [string]$Message,
    [string]$SessionId,
    [string]$IdempotencyKey,
    [switch]$NewSubmission,
    [switch]$RawEvents,
    [switch]$Items,
    [string]$KeyFile,
    [switch]$NonInteractive,
    [switch]$OpenBrowser,
    [ValidateRange(1024, 65535)]
    [int]$Port = 8787
)

$ErrorActionPreference = 'Stop'
$temporaryKey = $false
$previousKey = $null
$secret = $null
$messageFile = $null
$previousPythonEncoding = [Environment]::GetEnvironmentVariable('PYTHONIOENCODING', 'Process')
$previousConsoleEncoding = [Console]::OutputEncoding
$previousOutputEncoding = $OutputEncoding
$changedEncoding = $false
$exitCode = 1
try {
    if ($PSBoundParameters.ContainsKey('Message') -and $Action -notin @('start', 'send')) {
        throw '-Message is supported only with start or send.'
    }
    if ($Action -eq 'studio' -and ($SessionId -or $IdempotencyKey -or $NewSubmission -or $RawEvents -or $Items)) {
        throw 'Session and stream options belong to CLI actions, not studio.'
    }
    if ($Action -ne 'studio' -and ($OpenBrowser -or $PSBoundParameters.ContainsKey('Port'))) {
        throw '-OpenBrowser and -Port are supported only with studio.'
    }
    if ([string]::IsNullOrWhiteSpace($env:OPENAI_API_KEY) -or
        $env:OPENAI_API_KEY.StartsWith('<') -or
        $env:OPENAI_API_KEY -match '^\*+$') {
        $previousKey = [Environment]::GetEnvironmentVariable('OPENAI_API_KEY', 'Process')
        if (-not $PSBoundParameters.ContainsKey('KeyFile')) {
            $KeyFile = Join-Path $env:LOCALAPPDATA 'SZL\Secrets\openai-default-estate-agents-20261003.dpapi'
        }
        if (Test-Path -LiteralPath $KeyFile -PathType Leaf) {
            # A Python parent may inherit PowerShell 7's module path before launching 5.1.
            # Load this engine's built-in security module rather than an incompatible copy.
            Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1') -ErrorAction Stop
            # ConvertFrom-SecureString without a supplied key uses current-user Windows DPAPI.
            $secret = ConvertTo-SecureString -String ((Get-Content -LiteralPath $KeyFile -Raw).Trim())
            Write-Host 'Loaded the current-user DPAPI credential for this process.'
        } elseif ($NonInteractive -or $PSBoundParameters.ContainsKey('KeyFile')) {
            throw 'No usable OPENAI_API_KEY or DPAPI key file is available. Supply a key through your secret manager or -KeyFile.'
        } else {
            Write-Host 'Enter the actual API secret for OpenAI project proj_M074YvE258sr0Rg13cNmJn8j (input hidden).'
            $secret = Read-Host 'OpenAI API key' -AsSecureString
        }
        $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secret)
        try {
            $env:OPENAI_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
            $temporaryKey = $true
        } finally {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
        }
        if ([string]::IsNullOrWhiteSpace($env:OPENAI_API_KEY)) {
            throw 'OPENAI_API_KEY is empty. Paste the actual secret key, not the masked asterisks.'
        }
    }

    $arguments = @('-3', (Join-Path $PSScriptRoot 'new_agent.py'), $Action)
    if ($Action -eq 'studio') {
        $arguments = @('-3', (Join-Path $PSScriptRoot 'studio.py'), '--port', [string]$Port)
        if ($OpenBrowser) { $arguments += '--open-browser' }
    }
    if ($PSBoundParameters.ContainsKey('Message')) {
        # Windows PowerShell 5.1 alters embedded quotes when passing native arguments.
        $messageFile = Join-Path ([System.IO.Path]::GetTempPath()) ("new-agent-message-{0}.txt" -f [guid]::NewGuid())
        [System.IO.File]::WriteAllText($messageFile, $Message, [System.Text.UTF8Encoding]::new($false))
        $arguments += @('--message-file', $messageFile)
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

    # Keep streamed Unicode intact even when a Python parent captures this launcher.
    $changedEncoding = $true
    $env:PYTHONIOENCODING = 'utf-8'
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    $OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    & py @arguments
    $exitCode = $LASTEXITCODE
} catch {
    [Console]::Error.WriteLine("Error: $($_.Exception.Message)")
    $exitCode = 1
} finally {
    if ($null -ne $messageFile) {
        Remove-Item -LiteralPath $messageFile -ErrorAction SilentlyContinue
    }
    if ($temporaryKey) {
        if ($null -eq $previousKey) {
            Remove-Item Env:OPENAI_API_KEY -ErrorAction SilentlyContinue
        } else {
            $env:OPENAI_API_KEY = $previousKey
        }
    }
    if ($null -ne $secret) { $secret.Dispose() }
    if ($changedEncoding) {
        if ($null -eq $previousPythonEncoding) {
            Remove-Item Env:PYTHONIOENCODING -ErrorAction SilentlyContinue
        } else {
            $env:PYTHONIOENCODING = $previousPythonEncoding
        }
        [Console]::OutputEncoding = $previousConsoleEncoding
        $OutputEncoding = $previousOutputEncoding
    }
}
exit $exitCode
