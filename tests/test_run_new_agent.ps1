<# Test that the launcher removes only a key it collected itself. Run in a fresh PowerShell process. #>
$ErrorActionPreference = 'Stop'
$launcher = Join-Path (Split-Path $PSScriptRoot -Parent) 'run-new-agent.ps1'
$original = [Environment]::GetEnvironmentVariable('OPENAI_API_KEY', 'Process')
$originalLocalAppData = $env:LOCALAPPDATA
$originalPythonEncoding = [Environment]::GetEnvironmentVariable('PYTHONIOENCODING', 'Process')
$originalConsoleCodePage = [Console]::OutputEncoding.CodePage
$testDirectory = Join-Path ([System.IO.Path]::GetTempPath()) ("SZL launcher tests {0}" -f [guid]::NewGuid())
$null = New-Item -ItemType Directory -Path $testDirectory
$env:LOCALAPPDATA = $testDirectory
$global:pythonLauncher = (Get-Command py -CommandType Application).Source

try {
    function global:py {
        $global:observedArguments = @($args)
        $global:observedKey = [Environment]::GetEnvironmentVariable('OPENAI_API_KEY', 'Process')
        if ($global:invokeNative) {
            $nativeArguments = @($args)
            $nativeArguments[1] = $global:stubFile
            & $global:pythonLauncher @nativeArguments
            return
        }
        $global:LASTEXITCODE = $(if ($global:failPy) { 1 } else { 0 })
    }
    function global:Read-Host {
        param([string]$Prompt, [switch]$AsSecureString)
        if (-not $AsSecureString) { throw 'Expected hidden input.' }
        return ConvertTo-SecureString 'sk-local-synthetic-test' -AsPlainText -Force
    }

    $env:OPENAI_API_KEY = 'sk-already-configured-test'
    & $launcher create
    if ($LASTEXITCODE -ne 0 -or $global:observedKey -ne 'sk-already-configured-test' -or
        $env:OPENAI_API_KEY -ne 'sk-already-configured-test') {
        throw 'Configured-key preservation failed.'
    }

    Remove-Item Env:OPENAI_API_KEY
    $global:observedKey = $null
    & $launcher create
    if ($LASTEXITCODE -ne 0 -or $global:observedKey -ne 'sk-local-synthetic-test' -or
        (Test-Path Env:OPENAI_API_KEY)) {
        throw 'Prompted-key cleanup failed.'
    }

    $global:failPy = $true
    $global:observedKey = $null
    & $launcher create
    if ($LASTEXITCODE -ne 1 -or $global:observedKey -ne 'sk-local-synthetic-test' -or
        (Test-Path Env:OPENAI_API_KEY)) {
        throw 'Prompted-key cleanup after child failure failed.'
    }

    $global:failPy = $false
    $dpapiFile = Join-Path $testDirectory 'synthetic.dpapi'
    $syntheticSecret = ConvertTo-SecureString 'sk-dpapi-synthetic-test' -AsPlainText -Force
    ConvertFrom-SecureString $syntheticSecret | Set-Content -LiteralPath $dpapiFile
    $syntheticSecret.Dispose()
    & $launcher create -KeyFile $dpapiFile -NonInteractive
    if ($LASTEXITCODE -ne 0 -or $global:observedKey -ne 'sk-dpapi-synthetic-test' -or
        (Test-Path Env:OPENAI_API_KEY)) { throw 'DPAPI key scope failed.' }

    $global:observedKey = $null
    & $launcher create -KeyFile (Join-Path $testDirectory 'absent.dpapi') -NonInteractive
    if ($LASTEXITCODE -ne 1 -or $null -ne $global:observedKey -or
        (Test-Path Env:OPENAI_API_KEY)) { throw 'Missing DPAPI file did not stop before child invocation.' }

    & $launcher studio -KeyFile $dpapiFile -NonInteractive -OpenBrowser -Port 9011
    if ($LASTEXITCODE -ne 0 -or $global:observedArguments.Count -ne 5 -or
        [System.IO.Path]::GetFileName($global:observedArguments[1]) -ne 'studio.py' -or
        $global:observedArguments[2] -ne '--port' -or $global:observedArguments[3] -ne '9011' -or
        $global:observedArguments[4] -ne '--open-browser' -or (Test-Path Env:OPENAI_API_KEY)) {
        throw 'Studio launcher arguments or credential cleanup failed.'
    }
    $global:observedArguments = $null
    & $launcher create -OpenBrowser
    if ($LASTEXITCODE -ne 1 -or $null -ne $global:observedArguments) {
        throw 'Studio-only options reached the CLI child.'
    }

    $global:stubFile = Join-Path $testDirectory 'native probe.py'
    $resultFile = Join-Path $testDirectory 'native-result.json'
    $env:SZL_LAUNCHER_TEST_RESULT = $resultFile
    @'
import json, os, pathlib, sys
assert sys.argv[1] == 'send'
assert sys.argv[2] == '--message-file'
assert len(sys.argv) == 4
p = pathlib.Path(sys.argv[3])
pathlib.Path(os.environ['SZL_LAUNCHER_TEST_RESULT']).write_text(json.dumps({'message': p.read_text(encoding='utf-8'), 'file': str(p)}), encoding='utf-8')
print('\u03bb \u201cquoted\u201d')
sys.exit(int(os.environ.get('SZL_LAUNCHER_TEST_EXIT', '0')))
'@ | Set-Content -LiteralPath $global:stubFile -Encoding UTF8
    $global:invokeNative = $true
    $probeMessage = "Explain `"quoted values`" at C:\research\ with Unicode: $([char]0x03BB)`nNext line."
    foreach ($childExit in @('0', '1')) {
        $env:SZL_LAUNCHER_TEST_EXIT = $childExit
        $probeOutput = @(& $launcher send -KeyFile $dpapiFile -NonInteractive -Message $probeMessage)
        if ($LASTEXITCODE -ne [int]$childExit) { throw 'Native child exit code was not preserved.' }
        $nativeResult = Get-Content -LiteralPath $resultFile -Raw | ConvertFrom-Json
        if ($nativeResult.message -cne $probeMessage) { throw 'Native quoted/Unicode/multiline message changed.' }
        if (Test-Path -LiteralPath $nativeResult.file) { throw 'Temporary message file remained after child exit.' }
        if (Test-Path Env:OPENAI_API_KEY) { throw 'DPAPI credential remained after native child exit.' }
        $expectedOutput = "$([char]0x03BB) $([char]0x201C)quoted$([char]0x201D)"
        if ($probeOutput -cnotcontains $expectedOutput) { throw 'Native Unicode output changed.' }
        if ([Environment]::GetEnvironmentVariable('PYTHONIOENCODING', 'Process') -cne $originalPythonEncoding -or
            [Console]::OutputEncoding.CodePage -ne $originalConsoleCodePage) {
            throw 'Launcher output encoding settings were not restored.'
        }
    }
    'Native message transport and DPAPI scope: PASS'
    'Launcher key scope: PASS'
} finally {
    if ($null -eq $original) {
        Remove-Item Env:OPENAI_API_KEY -ErrorAction SilentlyContinue
    } else {
        $env:OPENAI_API_KEY = $original
    }
    Remove-Item Function:py -ErrorAction SilentlyContinue
    Remove-Item Function:Read-Host -ErrorAction SilentlyContinue
    $env:LOCALAPPDATA = $originalLocalAppData
    Remove-Item Env:SZL_LAUNCHER_TEST_RESULT -ErrorAction SilentlyContinue
    Remove-Item Env:SZL_LAUNCHER_TEST_EXIT -ErrorAction SilentlyContinue
    # Only files created by this test; do not recursively delete a computed directory.
    Get-ChildItem -LiteralPath $testDirectory -File | Remove-Item
    Remove-Item -LiteralPath $testDirectory
}
