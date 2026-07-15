param([string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path)

$ErrorActionPreference = "Stop"
$prompt = Join-Path $ProjectRoot "handoff\web-server\prompt.md"
if (-not (Test-Path -LiteralPath $prompt -PathType Leaf)) { throw "Missing website Codex prompt: $prompt" }

Set-Location -LiteralPath $ProjectRoot
Write-Host "RadioTEDU website-server handoff (read-only starter)."
if (Test-Path -LiteralPath ".git") { Write-Host "Transferred revision: $(& git rev-parse HEAD)" }
Write-Host "Open the following prompt in Codex on this target computer:"
Write-Host $prompt
Write-Host ""
Get-Content -Raw -LiteralPath $prompt
