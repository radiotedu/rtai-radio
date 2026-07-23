[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("RadioTEDU.SharedAI", "RadioTEDU.BroadcastSupervisor")]
    [string]$ServiceName,
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,
    [Parameter(Mandatory = $true)]
    [string]$ConfigRoot,
    [string]$Python = "py.exe"
)

$ErrorActionPreference = "Stop"

function Import-RadioTEDUEnvironment([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Missing service environment file: $Path"
    }
    Get-Content -LiteralPath $Path | ForEach-Object {
        $line = $_.Trim()
        if ($line.Length -eq 0 -or $line.StartsWith("#")) { return }
        $separator = $line.IndexOf("=")
        if ($separator -lt 1) { throw "Invalid environment line in $Path" }
        $name = $line.Substring(0, $separator).Trim()
        $value = $line.Substring($separator + 1).Trim().Trim('"').Trim("'")
        [Environment]::SetEnvironmentVariable($name, $value, "Process")
    }
}

function Invoke-Python([string[]]$Arguments) {
    if ([IO.Path]::GetFileName($Python).ToLowerInvariant() -eq "py.exe") {
        & $Python "-3.12" @Arguments
    } else {
        & $Python @Arguments
    }
    exit $LASTEXITCODE
}

function Test-ProtectedValue([string]$Value) {
    if ([string]::IsNullOrWhiteSpace($Value)) { return $false }
    return $Value -notmatch '(?i)^<.*>$|replace-with|placeholder|changeme|example'
}

$envFile = Join-Path $ConfigRoot "$ServiceName.env"
Import-RadioTEDUEnvironment $envFile
Set-Location -LiteralPath $ProjectRoot
$env:PYTHONPATH = $ProjectRoot

switch ($ServiceName) {
    "RadioTEDU.SharedAI" {
        if ($env:QWEN_TTS_HOST -notin @("127.0.0.1", "localhost", "::1")) {
            throw "SharedAI Qwen must bind to loopback"
        }
        foreach ($requiredFile in @($env:QWEN_MODEL_CHECKSUM_FILE, $env:QWEN_WARMUP_REQUEST_JSON)) {
            if (-not (Test-ProtectedValue $requiredFile) -or -not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
                throw "SharedAI requires its protected model and warmup files"
            }
        }
        if (-not (Test-ProtectedValue $env:QWEN_VOICE_ROOT) -or -not (Test-Path -LiteralPath $env:QWEN_VOICE_ROOT -PathType Container)) {
            throw "SharedAI requires its protected voice root"
        }
        $warmup = Get-Content -LiteralPath $env:QWEN_WARMUP_REQUEST_JSON -Raw | ConvertFrom-Json
        if (
            -not (Test-ProtectedValue ([string]$warmup.voice.voice_pack)) -or
            ([string]$warmup.voice.voice_pack) -match '(?i)technical|not-approved|qualification'
        ) {
            throw "SharedAI warmup must use an approved production voice pack"
        }
        foreach ($relativeAsset in @($warmup.voice.reference_audio_path, $warmup.voice.clone_prompt_path)) {
            $asset = Join-Path $env:QWEN_VOICE_ROOT ([string]$relativeAsset)
            if (-not (Test-Path -LiteralPath $asset -PathType Leaf)) {
                throw "SharedAI approved warmup voice assets are incomplete"
            }
        }
        $ollama = $null
        if ($env:OLLAMA_COMMAND) {
            $ollama = Start-Process -FilePath $env:OLLAMA_COMMAND -ArgumentList "serve" -PassThru -WindowStyle Hidden
        }
        try {
            Invoke-Python @("-m", "scripts.run_qwen_tts_service")
        } finally {
            if ($null -ne $ollama -and -not $ollama.HasExited) { Stop-Process -Id $ollama.Id -Force }
        }
    }
    "RadioTEDU.BroadcastSupervisor" {
        if ($env:RADIOTEDU_AGENT_ID -ne "school-radio-pc") { throw "Broadcast supervisor has an invalid agent identity" }
        if ($env:RADIOTEDU_AGENT_SCOPE -ne "agent:playout") { throw "Broadcast supervisor has an invalid agent scope" }
        if (
            -not (Test-ProtectedValue $env:RADIOTEDU_EN_SOURCE_CREDENTIALS) -or
            -not (Test-ProtectedValue $env:RADIOTEDU_FR_SOURCE_CREDENTIALS)
        ) {
            throw "Broadcast supervisor requires protected source credentials for both mounts"
        }
        if (
            -not (Test-ProtectedValue $env:RADIOTEDU_EN_SNAPSHOT_SECRET) -or
            -not (Test-ProtectedValue $env:RADIOTEDU_FR_SNAPSHOT_SECRET)
        ) {
            throw "Broadcast supervisor requires per-station HMAC secrets"
        }
        try {
            $qwenHealth = Invoke-RestMethod -Uri "http://127.0.0.1:8090/health" -TimeoutSec 5
        } catch {
            throw "Broadcast supervisor requires the warmed loopback Qwen service"
        }
        if ($qwenHealth.status -ne "ready" -or -not $qwenHealth.warmed) {
            throw "Broadcast supervisor requires the warmed loopback Qwen service"
        }
        if (-not (Test-ProtectedValue $env:LIQUIDSOAP_COMMAND)) {
            throw "Broadcast supervisor requires a protected Liquidsoap command"
        }
        $liquidsoap = Get-Command $env:LIQUIDSOAP_COMMAND -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -eq $liquidsoap) {
            throw "Broadcast supervisor cannot find its Liquidsoap command"
        }
        $buildConfig = & $liquidsoap.Source --build-config 2>&1
        if ($LASTEXITCODE -ne 0 -or -not ($buildConfig | Select-String -Pattern 'FFmpeg\s*:\s*yes')) {
            throw "Broadcast supervisor requires service-visible FFmpeg encoding"
        }
        Invoke-Python @("-m", "scripts.run_station_forever", "--root", $ProjectRoot, "--interval-seconds", "10")
    }
}
