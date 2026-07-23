$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$stationSupervisor = Join-Path $PSScriptRoot "run_temporary_station_supervisor.py"
$startBroadcast = Join-Path $PSScriptRoot "start-temporary-dual-station.ps1"
$dashboardScript = Join-Path $PSScriptRoot "terminal_health_dashboard.py"
$dashboardLauncher = Join-Path $repoRoot "RadioTEDU-Health-Dashboard.cmd"
$shadowRunner = Join-Path $PSScriptRoot "run_shadow_music_director.py"
$startShadow = Join-Path $PSScriptRoot "start-shadow-music-director.ps1"
$announcementWorker = Join-Path $PSScriptRoot "run_track_announcement_worker.py"
$announcementSupervisor = Join-Path $PSScriptRoot "run_track_announcement_worker_supervisor.py"
$runtimeRoot = Join-Path $repoRoot "data\runtime\temporary-dual-station"
$metadataRelease = "C:\RadioTEDU\ai-broadcast-agent\releases\20260723.1"
$metadataAgent = Join-Path $metadataRelease "agent.py"
$metadataPython = Join-Path $metadataRelease ".venv\Scripts\python.exe"
$metadataConfig = "C:\ProgramData\RadioTEDU\ai-broadcast-agent\config\agent.env"

Start-Sleep -Seconds 20

$supervisorPattern = [regex]::Escape($stationSupervisor)
$supervisors = @(
    Get-CimInstance Win32_Process |
        Where-Object {
            $_.CommandLine -and
            $_.CommandLine -match $supervisorPattern
        }
)
$languagesReady = @("radiotedu-en", "radiotedu-fr") | ForEach-Object {
    $station = $_
    @($supervisors | Where-Object { $_.CommandLine -match "--station\s+$([regex]::Escape($station))" }).Count -gt 0
}

if (@($languagesReady | Where-Object { $_ }).Count -ne 2) {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $startBroadcast
}

$shadowPattern = [regex]::Escape($shadowRunner)
$shadowRunning = @(
    Get-CimInstance Win32_Process |
        Where-Object {
            $_.CommandLine -and $_.CommandLine -match $shadowPattern
        }
).Count -gt 0
if (-not $shadowRunning) {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $startShadow
}

$announcementPattern = [regex]::Escape($announcementSupervisor)
$announcementRunning = @(
    Get-CimInstance Win32_Process |
        Where-Object {
            $_.CommandLine -and $_.CommandLine -match $announcementPattern
        }
).Count -gt 0
if (-not $announcementRunning) {
    $announcementRuntime = Join-Path $repoRoot "data\runtime\qwen-track-announcements"
    New-Item -ItemType Directory -Force -Path $announcementRuntime | Out-Null
    Start-Process -FilePath (Join-Path $repoRoot ".venv\Scripts\python.exe") `
        -ArgumentList @($announcementSupervisor) `
        -WorkingDirectory $repoRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $announcementRuntime "supervisor.out.log") `
        -RedirectStandardError (Join-Path $announcementRuntime "supervisor.err.log")
}

$metadataPattern = [regex]::Escape($metadataAgent)
$metadataRunning = @(
    Get-CimInstance Win32_Process |
        Where-Object {
            $_.CommandLine -and $_.CommandLine -match $metadataPattern
        }
).Count -gt 0
if (-not $metadataRunning) {
    Start-Process -FilePath $metadataPython `
        -ArgumentList @($metadataAgent, "--config", $metadataConfig) `
        -WorkingDirectory $metadataRelease `
        -WindowStyle Hidden
}

$dashboardPattern = [regex]::Escape($dashboardScript)
$dashboardRunning = @(
    Get-CimInstance Win32_Process |
        Where-Object {
            $_.CommandLine -and
            $_.CommandLine -match $dashboardPattern
        }
).Count -gt 0
if (-not $dashboardRunning) {
    Start-Process -FilePath "cmd.exe" `
        -ArgumentList @("/c", $dashboardLauncher) `
        -WorkingDirectory $repoRoot
}

$status = [ordered]@{
    checked_at = (Get-Date).ToUniversalTime().ToString("o")
    broadcast_supervisors = 2
    shadow_music_director = "read_only"
    announcement_worker = "supervised_local_qwen_below_normal_two_threads"
    metadata_agent = "outbound_only"
    dashboard_running = $true
}
$statusPath = Join-Path $runtimeRoot "autostart-status.json"
$status | ConvertTo-Json | Set-Content -LiteralPath $statusPath -Encoding UTF8
