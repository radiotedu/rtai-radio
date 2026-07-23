[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path,
    [string]$ConfigRoot = "C:\ProgramData\RadioTEDU\config",
    [string]$Python = "py.exe",
    [switch]$InitializeConfig,
    [switch]$Automatic,
    [switch]$Start
)

$ErrorActionPreference = "Stop"
$services = @("RadioTEDU.SharedAI", "RadioTEDU.BroadcastSupervisor")
$serviceHost = Join-Path $PSScriptRoot "windows_service_host.py"
$selectors = @{
    "RadioTEDU.SharedAI" = "shared-ai"
    "RadioTEDU.BroadcastSupervisor" = "broadcast-supervisor"
}

if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this installer in an elevated PowerShell session."
}
if (-not (Test-Path -LiteralPath $ProjectRoot -PathType Container)) { throw "Project root does not exist: $ProjectRoot" }
if (-not (Test-Path -LiteralPath $serviceHost -PathType Leaf)) { throw "Missing native service host: $serviceHost" }

function Invoke-ServiceHost([string[]]$Arguments) {
    if ([IO.Path]::GetFileName($Python).ToLowerInvariant() -eq "py.exe") {
        & $Python "-3.12" $serviceHost @Arguments
    } else {
        & $Python $serviceHost @Arguments
    }
    if ($LASTEXITCODE -ne 0) { throw "PyWin32 service host failed with exit code $LASTEXITCODE" }
}

function Import-ServiceEnvironment([string]$Path) {
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

if ($InitializeConfig) {
    New-Item -ItemType Directory -Force -Path $ConfigRoot | Out-Null
    foreach ($service in $services) {
        $envFile = Join-Path $ConfigRoot "$service.env"
        $example = Join-Path $PSScriptRoot "service-env\$service.env.example"
        if (-not (Test-Path -LiteralPath $example -PathType Leaf)) { throw "Missing service environment example: $example" }
        if (-not (Test-Path -LiteralPath $envFile -PathType Leaf)) {
            Copy-Item -LiteralPath $example -Destination $envFile
        }
    }
    & icacls.exe $ConfigRoot /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Failed to protect service configuration directory" }
    foreach ($service in $services) {
        $envFile = Join-Path $ConfigRoot "$service.env"
        & icacls.exe $envFile /inheritance:r /grant:r '*S-1-5-18:F' '*S-1-5-32-544:F' | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Failed to protect service environment file: $envFile" }
    }
}

foreach ($service in $services) {
    $envFile = Join-Path $ConfigRoot "$service.env"
    if (-not (Test-Path -LiteralPath $envFile -PathType Leaf)) {
        throw "Create the required service environment file before installation: $envFile"
    }
}

foreach ($service in $services) {
    if (Get-Service -Name $service -ErrorAction SilentlyContinue) {
        if ($PSCmdlet.ShouldProcess($service, "replace service definition")) {
            & sc.exe stop $service | Out-Null
            & sc.exe delete $service | Out-Null
            Start-Sleep -Seconds 1
        }
    }
    if ($PSCmdlet.ShouldProcess($service, "install service")) {
        Invoke-ServiceHost @($selectors[$service], "--startup", "manual", "install")
        & sc.exe failure $service reset= 600 actions= restart/2000/restart/4000/restart/8000 | Out-Null
        & sc.exe failureflag $service 1 | Out-Null
        & sc.exe description $service "RadioTEDU shared AI or dual-station broadcast supervisor" | Out-Null
        if ($Automatic) {
            & sc.exe config $service start= delayed-auto | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "Failed to enable delayed automatic startup for $service" }
        }
    }
}

if ($Start) {
    $runtimePython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    $smoke = Join-Path $ProjectRoot "scripts\smoke_broadcast.py"
    if (-not (Test-Path -LiteralPath $runtimePython -PathType Leaf)) { throw "Missing isolated runtime Python: $runtimePython" }
    Import-ServiceEnvironment (Join-Path $ConfigRoot "RadioTEDU.SharedAI.env")
    Import-ServiceEnvironment (Join-Path $ConfigRoot "RadioTEDU.BroadcastSupervisor.env")
    if ($PSCmdlet.ShouldProcess("RadioTEDU.SharedAI", "start loopback AI service")) {
        Start-Service -Name "RadioTEDU.SharedAI"
    }
    try {
        $qwenReady = $false
        for ($attempt = 0; $attempt -lt 60; $attempt++) {
            if (Get-NetTCPConnection -LocalPort 8090 -State Listen -ErrorAction SilentlyContinue) {
                $qwenReady = $true
                break
            }
            Start-Sleep -Seconds 1
        }
        if (-not $qwenReady) { throw "SharedAI did not expose loopback Qwen TTS on port 8090" }
        & $runtimePython $smoke --strict --json
        if ($LASTEXITCODE -ne 0) { throw "Strict dual-station smoke failed; broadcast supervisor will not be started" }
        if ($PSCmdlet.ShouldProcess("RadioTEDU.BroadcastSupervisor", "start dual-station supervisor")) {
            foreach ($service in $services) {
                & sc.exe config $service start= delayed-auto | Out-Null
                if ($LASTEXITCODE -ne 0) { throw "Failed to enable automatic startup for $service" }
            }
            Start-Service -Name "RadioTEDU.BroadcastSupervisor"
        }
    } catch {
        Stop-Service -Name "RadioTEDU.SharedAI" -Force -ErrorAction SilentlyContinue
        throw
    }
}
