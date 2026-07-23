$ErrorActionPreference = "Continue"

$repoRoot = Split-Path -Parent $PSScriptRoot
$runtime = Join-Path $repoRoot "data\runtime\temporary-dual-station"
$runner = Join-Path $PSScriptRoot "run_temporary_station.py"
$supervisor = Join-Path $PSScriptRoot "run_temporary_station_supervisor.py"

$patterns = @([regex]::Escape($runner), [regex]::Escape($supervisor))
$processes = @(
    Get-CimInstance Win32_Process |
        Where-Object {
            $commandLine = $_.CommandLine
            $commandLine -and @(
                $patterns | Where-Object { $commandLine -match $_ }
            ).Count -gt 0
        }
)
$ids = @($processes | ForEach-Object { [int]$_.ProcessId })
$roots = @($processes | Where-Object { $ids -notcontains [int]$_.ParentProcessId })
foreach ($process in $roots) {
    & taskkill.exe /PID ([int]$process.ProcessId) /T /F | Out-Null
}

foreach ($station in @("radiotedu-en", "radiotedu-fr")) {
    foreach ($name in @("station.pid", "supervisor.pid")) {
        Remove-Item -LiteralPath (Join-Path $runtime "$station\$name") -Force -ErrorAction SilentlyContinue
    }
}
