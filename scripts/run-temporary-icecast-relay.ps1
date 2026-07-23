param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("/ai", "/event")]
    [string]$Mount,
    [string]$LocalInput = "http://127.0.0.1:4320/ai",
    [string]$EnvironmentFile = "C:\Users\tedu\Desktop\voting\rtjukebox\tools\local-voting-agent\.env"
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$runtimeDir = Join-Path $repoRoot "data\runtime\temporary-relays"
$mountName = $Mount.TrimStart("/")
$pidPath = Join-Path $runtimeDir "$mountName.pid"
$logPath = Join-Path $runtimeDir "$mountName.log"

New-Item -ItemType Directory -Force -Path $runtimeDir | Out-Null

if (Test-Path -LiteralPath $pidPath) {
    $existingPid = 0
    [void][int]::TryParse((Get-Content -Raw -LiteralPath $pidPath).Trim(), [ref]$existingPid)
    if ($existingPid -gt 0 -and (Get-Process -Id $existingPid -ErrorAction SilentlyContinue)) {
        exit 0
    }
}

$PID | Set-Content -LiteralPath $pidPath -Encoding ASCII

function Write-RelayLog {
    param([string]$Message)
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8
}

function Read-DotEnv {
    param([string]$Path)
    $values = @{}
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -match "^\s*([^#=]+)=(.*)$") {
            $values[$matches[1].Trim()] = $matches[2].Trim()
        }
    }
    return $values
}

try {
    if (-not (Test-Path -LiteralPath $EnvironmentFile)) {
        throw "Environment file is missing."
    }

    $config = Read-DotEnv -Path $EnvironmentFile
    $ffmpeg = [string]$config["FFMPEG_PATH"]
    $sourceUser = [string]$config["ICECAST_SOURCE_USERNAME"]
    $sourcePassword = [string]$config["ICECAST_SOURCE_PASSWORD"]
    if (-not (Test-Path -LiteralPath $ffmpeg)) {
        throw "FFmpeg is missing."
    }
    if ([string]::IsNullOrWhiteSpace($sourceUser) -or [string]::IsNullOrWhiteSpace($sourcePassword)) {
        throw "Icecast source credentials are missing."
    }

    $encodedUser = [Uri]::EscapeDataString($sourceUser)
    $encodedPassword = [Uri]::EscapeDataString($sourcePassword)
    $target = "icecast://${encodedUser}:${encodedPassword}@stream.radiotedu.com:11154$Mount"
    $streamName = if ($Mount -eq "/ai") { "RadioTEDU AI Temporary" } else { "RadioTEDU Event Temporary" }

    Write-RelayLog "Temporary relay starting for $Mount."
    while ($true) {
        $arguments = @(
            "-hide_banner",
            "-loglevel", "error",
            "-nostdin",
            "-i", $LocalInput,
            "-vn",
            "-c:a", "copy",
            "-content_type", "audio/mpeg",
            "-f", "mp3",
            "-legacy_icecast", "1",
            "-user_agent", "RadioTEDU Temporary Relay",
            "-ice_name", $streamName,
            "-ice_description", "Temporary RadioTEDU AI radio relay",
            "-ice_genre", "RadioTEDU",
            "-ice_public", "1",
            $target
        )

        & $ffmpeg @arguments 2>&1 |
            ForEach-Object {
                $safe = [string]$_
                $safe = $safe -replace "(?i)(icecast://[^:]+:)[^@]+@", '$1<REDACTED>@'
                Write-RelayLog $safe
            }
        Write-RelayLog "Relay disconnected; retrying in 3 seconds."
        Start-Sleep -Seconds 3
    }
}
catch {
    Write-RelayLog ("Relay stopped: " + $_.Exception.Message)
    exit 1
}
finally {
    Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
}
