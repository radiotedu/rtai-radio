[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("RadioTEDU.SharedAI", "RadioTEDU.Station.EN", "RadioTEDU.Station.FR", "RadioTEDU.PublicSync")]
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

$envFile = Join-Path $ConfigRoot "$ServiceName.env"
Import-RadioTEDUEnvironment $envFile
Set-Location -LiteralPath $ProjectRoot
$env:PYTHONPATH = $ProjectRoot

switch ($ServiceName) {
    "RadioTEDU.SharedAI" {
        if ($env:QWEN_TTS_HOST -notin @("127.0.0.1", "localhost", "::1")) {
            throw "SharedAI Qwen must bind to loopback"
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
    "RadioTEDU.Station.EN" {
        if ($env:STATION_ID -ne "radiotedu-en" -or $env:API_PORT -ne "8765") { throw "EN service has an invalid station scope" }
        if ($env:RADIOTEDU_FR_SOURCE_CREDENTIALS -or $env:RADIOTEDU_FR_SNAPSHOT_SECRET) { throw "EN service contains FR state" }
        Invoke-Python @("-m", "uvicorn", "backend.app:app", "--host", "127.0.0.1", "--port", "8765")
    }
    "RadioTEDU.Station.FR" {
        if ($env:STATION_ID -ne "radiotedu-fr" -or $env:API_PORT -ne "8766") { throw "FR service has an invalid station scope" }
        if ($env:RADIOTEDU_EN_SOURCE_CREDENTIALS -or $env:RADIOTEDU_EN_SNAPSHOT_SECRET) { throw "FR service contains EN state" }
        Invoke-Python @("-m", "uvicorn", "backend.app:app", "--host", "127.0.0.1", "--port", "8766")
    }
    "RadioTEDU.PublicSync" {
        Invoke-Python @("-m", "scripts.run_public_sync_service")
    }
}
