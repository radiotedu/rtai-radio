[CmdletBinding()]
param(
    [string]$LoopbackBaseUrl = "http://127.0.0.1:8000",
    [string]$FrontendDist = "",
    [switch]$Strict
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($FrontendDist)) {
    $FrontendDist = (Resolve-Path (Join-Path $PSScriptRoot "..\..\dist\frontend")).Path
}

function Get-HttpStatus([string]$Uri) {
    try {
        $request = [Net.HttpWebRequest]::Create($Uri)
        $request.Method = "GET"
        $request.Timeout = 5000
        $request.AllowAutoRedirect = $false
        $response = $request.GetResponse()
        try { return [int]$response.StatusCode } finally { $response.Dispose() }
    } catch [Net.WebException] {
        if ($null -ne $_.Exception.Response) {
            try { return [int]$_.Exception.Response.StatusCode } finally { $_.Exception.Response.Dispose() }
        }
        return $null
    }
}

$base = $LoopbackBaseUrl.TrimEnd('/')
$http = [ordered]@{
    ai = Get-HttpStatus "$base/ai"
    radiotedu_en = Get-HttpStatus "$base/v1/radio/stations/radiotedu-en/status"
    radiotedu_fr = Get-HttpStatus "$base/v1/radio/stations/radiotedu-fr/status"
}

try {
    $openApi = (Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 -Uri "$base/openapi.json").Content
} catch {
    $openApi = ""
}
$handshakePresent = $openApi.Contains("/v1/radio/stations/{station_id}/handshake")

$javascript = Get-ChildItem -LiteralPath (Join-Path $FrontendDist "assets") -Filter "*.js" -File |
    ForEach-Object { [IO.File]::ReadAllText($_.FullName) }
$bundle = $javascript -join "`n"
$englishStream = $bundle.Contains("https://stream.radiotedu.com/ai")
$frenchStream = $bundle.Contains("https://stream.radiotedu.com/event")
$privateIcecastAbsent = $bundle -notmatch '10\.98\.98\.75|:11154'
$operatorControlsAbsent = $bundle -notmatch '/api/air|/api/control|radiotedu_admin_token|OperatorApp'
$firstViewportContentPresent = (
    $bundle.Contains("RadioTEDU") -and
    $bundle.Contains("Current listeners") -and
    $bundle.Contains("Now playing")
)
$httpReady = @($http.Values | Where-Object { $_ -eq 200 }).Count -eq 3
$ok = [bool](
    $httpReady -and
    $handshakePresent -and
    $englishStream -and
    $frenchStream -and
    $privateIcecastAbsent -and
    $operatorControlsAbsent -and
    $firstViewportContentPresent
)

[ordered]@{
    checked_at = (Get-Date).ToUniversalTime().ToString("o")
    loopback_http_status = $http
    browser_streams = [ordered]@{
        english = "https://stream.radiotedu.com/ai"
        french = "https://stream.radiotedu.com/event"
        english_present = $englishStream
        french_present = $frenchStream
    }
    private_icecast_absent = $privateIcecastAbsent
    operator_controls_absent = $operatorControlsAbsent
    mutual_handshake_endpoint_present = $handshakePresent
    first_viewport_content_present = $firstViewportContentPresent
    website_ready = $ok
    credentials_logged = $false
} | ConvertTo-Json -Depth 6

if ($Strict -and -not $ok) { exit 1 }
