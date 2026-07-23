$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
$runner = Join-Path $PSScriptRoot "run_temporary_station.py"
$supervisor = Join-Path $PSScriptRoot "run_temporary_station_supervisor.py"
$runtime = Join-Path $repoRoot "data\runtime\temporary-dual-station"
$qwen = Join-Path $repoRoot "data\runtime\qwen-commissioning"

foreach ($required in @(
    $python,
    $runner,
    $supervisor,
    (Join-Path $qwen "en-radio-id.wav"),
    (Join-Path $qwen "en-continuity.wav"),
    (Join-Path $qwen "fr-radio-id.wav"),
    (Join-Path $qwen "fr-continuity.wav")
)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Required temporary broadcast asset is missing: $required"
    }
}

$runnerPattern = [regex]::Escape($runner)
$supervisorPattern = [regex]::Escape($supervisor)
$runnerProcesses = @(
    Get-CimInstance Win32_Process |
        Where-Object {
            $_.CommandLine -and (
                $_.CommandLine -match $runnerPattern -or
                $_.CommandLine -match $supervisorPattern
            )
        }
)
$runnerIds = @($runnerProcesses | ForEach-Object { [int]$_.ProcessId })
$runnerRoots = @(
    $runnerProcesses |
        Where-Object { $runnerIds -notcontains [int]$_.ParentProcessId }
)
foreach ($existing in $runnerRoots) {
    & taskkill.exe /PID ([int]$existing.ProcessId) /T /F | Out-Null
}
if ($runnerRoots.Count -gt 0) {
    Start-Sleep -Seconds 1
}

$legacyStop = Join-Path $PSScriptRoot "stop-temporary-icecast-relays.ps1"
if (Test-Path -LiteralPath $legacyStop) {
    & $legacyStop
}

New-Item -ItemType Directory -Force -Path $runtime | Out-Null

foreach ($station in @("radiotedu-en", "radiotedu-fr")) {
    $stationRuntime = Join-Path $runtime $station
    New-Item -ItemType Directory -Force -Path $stationRuntime | Out-Null
    $stdout = Join-Path $stationRuntime "supervisor.out.log"
    $stderr = Join-Path $stationRuntime "supervisor.err.log"
    $process = Start-Process `
        -FilePath $python `
        -ArgumentList @($supervisor, "--station", $station) `
        -WorkingDirectory $repoRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr `
        -PassThru
    Write-Output "$station supervisor started with pid $($process.Id)"
}
