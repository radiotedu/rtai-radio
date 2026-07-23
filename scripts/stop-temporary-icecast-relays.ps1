$ErrorActionPreference = "Continue"

$repoRoot = Split-Path -Parent $PSScriptRoot
$runtimeDir = Join-Path $repoRoot "data\runtime\temporary-relays"

foreach ($mountName in @("ai", "event")) {
    $pidPath = Join-Path $runtimeDir "$mountName.pid"
    if (-not (Test-Path -LiteralPath $pidPath)) {
        continue
    }

    $relayPid = 0
    [void][int]::TryParse((Get-Content -Raw -LiteralPath $pidPath).Trim(), [ref]$relayPid)
    if ($relayPid -gt 0) {
        & taskkill.exe /PID $relayPid /T /F | Out-Null
    }
    Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
}
