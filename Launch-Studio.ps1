<# Open the authorized local UI without tying the server to a visible console. #>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
try {
    $dataDirectory = Join-Path $PSScriptRoot '.studio'
    $null = New-Item -ItemType Directory -Path $dataDirectory -Force
    $stamp = '{0}-{1}' -f (Get-Date -Format 'yyyyMMdd-HHmmss'), ([guid]::NewGuid().ToString('N'))
    $stdout = Join-Path $dataDirectory ("launcher-{0}.stdout.log" -f $stamp)
    $stderr = Join-Path $dataDirectory ("launcher-{0}.stderr.log" -f $stamp)
    $launcher = Join-Path $PSScriptRoot 'run-new-agent.ps1'
    $engine = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $arguments = '-NoProfile -ExecutionPolicy Bypass -File "{0}" studio -NonInteractive -OpenBrowser' -f $launcher
    $process = Start-Process -FilePath $engine -ArgumentList $arguments -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    $deadline = [DateTime]::UtcNow.AddSeconds(25)
    while ([DateTime]::UtcNow -lt $deadline) {
        $metadataPath = Join-Path $dataDirectory 'server.json'
        if (Test-Path -LiteralPath $metadataPath -PathType Leaf) {
            try {
                $metadata = Get-Content -LiteralPath $metadataPath -Raw | ConvertFrom-Json
                if ($metadata.origin -match '^http://127\.0\.0\.1:[0-9]{1,5}$' -and
                    $metadata.token -match '^[A-Za-z0-9_-]{40,100}$') {
                    $response = Invoke-WebRequest -UseBasicParsing -Uri ($metadata.origin + '/api/state') -Headers @{'X-Studio-Token' = $metadata.token} -TimeoutSec 2 -MaximumRedirection 0
                    $state = $response.Content | ConvertFrom-Json
                    if ($state.instance_id -eq $metadata.instance_id) {
                        Write-Host 'SZL Agent Studio is ready in your browser.'
                        exit 0
                    }
                }
            } catch {
                # A starting server may not have entered its request loop yet.
            }
        }
        $process.Refresh()
        if ($process.HasExited -and $process.ExitCode -ne 0) { break }
        Start-Sleep -Milliseconds 250
    }
    throw 'Studio could not confirm local startup. Run run-new-agent.ps1 studio -OpenBrowser to see the setup error. Local logs are under .studio.'
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
