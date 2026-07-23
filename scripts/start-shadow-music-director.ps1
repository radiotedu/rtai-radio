$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
$runner = Join-Path $PSScriptRoot "run_shadow_music_director.py"
$runtime = Join-Path $repoRoot "data\runtime\shadow-music-director"

foreach ($required in @($python, $runner)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Shadow Music Director dependency is missing: $required"
    }
}

$runnerPattern = [regex]::Escape($runner)
$running = @(
    Get-CimInstance Win32_Process |
        Where-Object {
            $_.CommandLine -and $_.CommandLine -match $runnerPattern
        }
).Count -gt 0

if ($running) {
    Write-Output "RadioTEDU Shadow Music Director is already running."
    exit 0
}

New-Item -ItemType Directory -Force -Path $runtime | Out-Null
$process = Start-Process `
    -FilePath $python `
    -ArgumentList @($runner, "--interval", "60") `
    -WorkingDirectory $repoRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $runtime "director.out.log") `
    -RedirectStandardError (Join-Path $runtime "director.err.log") `
    -PassThru

[pscustomobject]@{
    state = "shadow"
    read_only = $true
    controls_live_playout = $false
    pid = $process.Id
} | ConvertTo-Json
