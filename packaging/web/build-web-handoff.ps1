[CmdletBinding()]
param(
    [string]$OutputDirectory = ""
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$OutputDirectory = if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    Join-Path $root "handoff\web-server"
} else {
    $OutputDirectory
}
$output = [IO.Path]::GetFullPath($OutputDirectory)
$staging = [IO.Path]::GetFullPath((Join-Path $output ".staging-web-handoff"))
$packageRoot = Join-Path $staging "RadioTEDU-web"
$archive = Join-Path $output "RadioTEDU-web-handoff.zip"
$checksum = Join-Path $output "RadioTEDU-web-handoff.sha256"

if (-not $staging.StartsWith($output, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to use a staging path outside the handoff directory."
}
if (-not (Test-Path -LiteralPath (Join-Path $root "node_modules\.bin\vite.cmd") -PathType Leaf)) {
    throw "Workspace Node dependencies are missing. Run npm ci before creating the handoff."
}

if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force }
New-Item -ItemType Directory -Path $packageRoot -Force | Out-Null

$directories = @(
    "backend",
    "frontend",
    "frontend\public",
    "frontend\src",
    "frontend\src\components",
    "frontend\src\test",
    "frontend\src\__tests__",
    "scripts",
    "tests\backend",
    "packaging\web"
)
foreach ($relative in $directories) {
    New-Item -ItemType Directory -Path (Join-Path $packageRoot $relative) -Force | Out-Null
}

$files = @(
    "backend\__init__.py",
    "backend\config.py",
    "backend\platform_api.py",
    "backend\public_app.py",
    "scripts\smoke_public_server.py",
    "tests\backend\test_platform_api.py",
    "tests\backend\test_public_app.py",
    "packaging\web\requirements-web.lock.txt",
    "packaging\web\web.env.example",
    "packaging\web\BROADCAST-CONNECTION.md",
    "packaging\web\verify-website-runtime.ps1",
    "packaging\web\README.md",
    "packaging\web\.gitignore",
    "packaging\web\.gitattributes",
    "frontend\index.html",
    "frontend\src\main.tsx",
    "frontend\src\styles.css",
    "frontend\src\components\PublicDashboard.tsx",
    "frontend\src\test\setup.ts",
    "frontend\src\__tests__\public-app.test.tsx",
    "frontend\tsconfig.json",
    "frontend\vite.config.ts",
    "frontend\vitest.config.ts"
)
foreach ($relative in $files) {
    Copy-Item -LiteralPath (Join-Path $root $relative) -Destination (Join-Path $packageRoot $relative) -Force
}

Copy-Item -Path (Join-Path $root "frontend\public\*") -Destination (Join-Path $packageRoot "frontend\public") -Recurse -Force
Copy-Item -LiteralPath (Join-Path $root "packaging\web\frontend\App.tsx") -Destination (Join-Path $packageRoot "frontend\src\App.tsx") -Force
Copy-Item -LiteralPath (Join-Path $root "packaging\web\frontend\api.ts") -Destination (Join-Path $packageRoot "frontend\src\api.ts") -Force
Copy-Item -LiteralPath (Join-Path $root "packaging\web\backend\database.py") -Destination (Join-Path $packageRoot "backend\database.py") -Force
Copy-Item -LiteralPath (Join-Path $root "packaging\web\package.json") -Destination (Join-Path $packageRoot "package.json") -Force
Copy-Item -LiteralPath (Join-Path $root "packaging\web\package-lock.json") -Destination (Join-Path $packageRoot "package-lock.json") -Force
Copy-Item -LiteralPath (Join-Path $root "handoff\web-server\prompt.md") -Destination (Join-Path $packageRoot "SERVER-CODEX-PROMPT.md") -Force
Copy-Item -LiteralPath (Join-Path $root "packaging\web\README.md") -Destination (Join-Path $packageRoot "README.md") -Force
Copy-Item -LiteralPath (Join-Path $root "packaging\web\.gitignore") -Destination (Join-Path $packageRoot ".gitignore") -Force
Copy-Item -LiteralPath (Join-Path $root "packaging\web\.gitattributes") -Destination (Join-Path $packageRoot ".gitattributes") -Force

& (Join-Path $root "node_modules\.bin\tsc.cmd") -p (Join-Path $packageRoot "frontend\tsconfig.json")
if ($LASTEXITCODE -ne 0) { throw "Website-only TypeScript build failed." }
& (Join-Path $root "node_modules\.bin\vite.cmd") build (Join-Path $packageRoot "frontend")
if ($LASTEXITCODE -ne 0) { throw "Website-only Vite build failed." }

$binaryExtensions = @(".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".woff", ".woff2", ".zip")
$normalizableFiles = Get-ChildItem -LiteralPath $packageRoot -File -Recurse | Where-Object {
    $binaryExtensions -notcontains $_.Extension.ToLowerInvariant()
}
$utf8NoBom = New-Object Text.UTF8Encoding($false)
foreach ($file in $normalizableFiles) {
    $text = [IO.File]::ReadAllText($file.FullName)
    $normalized = $text.Replace("`r`n", "`n").Replace("`r", "`n")
    [IO.File]::WriteAllText($file.FullName, $normalized, $utf8NoBom)
}

$manifestFiles = Get-ChildItem -LiteralPath $packageRoot -File -Recurse | Sort-Object FullName
$manifest = foreach ($file in $manifestFiles) {
    [pscustomobject]@{
        path = $file.FullName.Substring($packageRoot.Length + 1).Replace("\", "/")
        size = $file.Length
        sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $file.FullName).Hash
    }
}
$manifest | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $packageRoot "MANIFEST.json") -Encoding UTF8

if (Test-Path -LiteralPath $archive) { Remove-Item -LiteralPath $archive -Force }
Compress-Archive -LiteralPath $packageRoot -DestinationPath $archive -CompressionLevel Optimal
$archiveHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $archive).Hash
"$archiveHash  RadioTEDU-web-handoff.zip" | Set-Content -LiteralPath $checksum -Encoding ascii

Remove-Item -LiteralPath $staging -Recurse -Force

[pscustomobject]@{
    archive = $archive
    sha256 = $archiveHash
    bytes = (Get-Item -LiteralPath $archive).Length
    files = $manifest.Count + 1
} | ConvertTo-Json
