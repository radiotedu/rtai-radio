[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$Version = "2.4.5",
    [string]$InstallRoot = "C:\ProgramData\RadioTEDU\runtime",
    [string]$ConfigRoot = "C:\ProgramData\RadioTEDU\config"
)

$ErrorActionPreference = "Stop"
$expectedSha256 = "17C29C9F662DB11CED6B85E807F6038E15E32B76F30F9695506905879A43F4B6"
$archiveName = "liquidsoap-$Version-win64.zip"
$downloadUri = "https://github.com/savonet/liquidsoap/releases/download/v$Version/$archiveName"
$archive = Join-Path $env:TEMP $archiveName
$destination = Join-Path $InstallRoot "liquidsoap-$Version-win64"
$executable = Join-Path $destination "liquidsoap.exe"
$supervisorEnvironment = Join-Path $ConfigRoot "RadioTEDU.BroadcastSupervisor.env"

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]$identity
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this installer in an elevated PowerShell session."
}

Invoke-WebRequest -Headers @{ "User-Agent" = "RadioTEDU-Installer" } -Uri $downloadUri -OutFile $archive
$actualSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $archive).Hash
if ($actualSha256 -ne $expectedSha256) {
    throw "Liquidsoap archive checksum mismatch. Expected $expectedSha256, got $actualSha256"
}

if ($PSCmdlet.ShouldProcess($destination, "install official native Windows Liquidsoap $Version")) {
    New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null
    $staging = Join-Path $InstallRoot "liquidsoap-$Version-staging"
    if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force }
    New-Item -ItemType Directory -Path $staging -Force | Out-Null
    Expand-Archive -LiteralPath $archive -DestinationPath $staging -Force
    $expanded = Join-Path $staging "liquidsoap-$Version-win64"
    if (-not (Test-Path -LiteralPath (Join-Path $expanded "liquidsoap.exe") -PathType Leaf)) {
        throw "Official Liquidsoap archive has an unexpected layout."
    }
    if (Test-Path -LiteralPath $destination) { Remove-Item -LiteralPath $destination -Recurse -Force }
    Move-Item -LiteralPath $expanded -Destination $destination
    Remove-Item -LiteralPath $staging -Recurse -Force
}

$buildConfig = & $executable --build-config 2>&1
if ($LASTEXITCODE -ne 0 -or -not ($buildConfig | Select-String -Pattern 'FFmpeg\s*:\s*yes')) {
    throw "The installed Liquidsoap build does not advertise FFmpeg support."
}

if (Test-Path -LiteralPath $supervisorEnvironment -PathType Leaf) {
    $lines = Get-Content -LiteralPath $supervisorEnvironment
    $found = $false
    $updated = foreach ($line in $lines) {
        if ($line -match '^\s*LIQUIDSOAP_COMMAND=') {
            $found = $true
            "LIQUIDSOAP_COMMAND=$executable"
        } elseif ($line -match '^\s*RADIOTEDU_WSL_DISTRO=') {
            continue
        } else {
            $line
        }
    }
    if (-not $found) { $updated += "LIQUIDSOAP_COMMAND=$executable" }
    $updated | Set-Content -LiteralPath $supervisorEnvironment -Encoding UTF8
}

[ordered]@{
    installed_at = (Get-Date).ToUniversalTime().ToString("o")
    version = $Version
    executable = $executable
    archive_sha256 = $actualSha256
    ffmpeg_supported = $true
    runtime = "native-windows"
    secrets_exposed = $false
} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $PSScriptRoot "..\..\data\diagnostics\liquidsoap-windows-status.json") -Encoding UTF8

Write-Host "Installed native Windows Liquidsoap $Version at $executable"
