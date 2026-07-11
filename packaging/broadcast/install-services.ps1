[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path,
    [string]$ConfigRoot = "C:\ProgramData\RadioTEDU\config",
    [string]$Python = "py.exe",
    [switch]$Start
)

$ErrorActionPreference = "Stop"
$services = @("RadioTEDU.SharedAI", "RadioTEDU.Station.EN", "RadioTEDU.Station.FR", "RadioTEDU.PublicSync")
$runner = Join-Path $PSScriptRoot "run-service.ps1"

if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this installer in an elevated PowerShell session."
}
if (-not (Test-Path -LiteralPath $ProjectRoot -PathType Container)) { throw "Project root does not exist: $ProjectRoot" }
if (-not (Test-Path -LiteralPath $runner -PathType Leaf)) { throw "Missing service runner: $runner" }

New-Item -ItemType Directory -Force -Path $ConfigRoot | Out-Null
foreach ($service in $services) {
    $envFile = Join-Path $ConfigRoot "$service.env"
    if (-not (Test-Path -LiteralPath $envFile -PathType Leaf)) {
        throw "Create the required service environment file before installation: $envFile"
    }
}

foreach ($service in $services) {
    $binaryPath = "`"$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe`" -NoProfile -ExecutionPolicy Bypass -File `"$runner`" -ServiceName `"$service`" -ProjectRoot `"$ProjectRoot`" -ConfigRoot `"$ConfigRoot`" -Python `"$Python`""
    if (Get-Service -Name $service -ErrorAction SilentlyContinue) {
        if ($PSCmdlet.ShouldProcess($service, "replace service definition")) {
            & sc.exe stop $service | Out-Null
            & sc.exe delete $service | Out-Null
            Start-Sleep -Seconds 1
        }
    }
    if ($PSCmdlet.ShouldProcess($service, "install service")) {
        & sc.exe create $service binPath= $binaryPath start= auto obj= "LocalSystem" | Out-Null
        & sc.exe failure $service reset= 600 actions= restart/2000/restart/4000/restart/8000 | Out-Null
        & sc.exe failureflag $service 1 | Out-Null
        & sc.exe description $service "RadioTEDU independently supervised broadcast service" | Out-Null
    }
}

if ($Start) {
    foreach ($service in $services) {
        if ($PSCmdlet.ShouldProcess($service, "start service")) { Start-Service -Name $service }
    }
}
