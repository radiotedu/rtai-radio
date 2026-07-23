[CmdletBinding()]
param(
    [string]$ComputerName = "teduradioweb25.tedu.edu.tr",
    [string]$OutputPath = (Join-Path (Resolve-Path (Join-Path $PSScriptRoot "..")) "data\webserver\discovery.json")
)

$ErrorActionPreference = "Stop"
$outputDirectory = Split-Path -Parent $OutputPath
New-Item -ItemType Directory -Force -Path $outputDirectory | Out-Null

$credential = Get-Credential -Message "Authorized administrator for $ComputerName (credential is not saved)"
if ($null -eq $credential) { throw "Webserver credential prompt was cancelled" }

$session = $null
try {
    $session = New-PSSession -ComputerName $ComputerName -Authentication Negotiate -Credential $credential
    $remote = Invoke-Command -Session $session -ScriptBlock {
        $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
            [Security.Principal.WindowsBuiltInRole]::Administrator
        )
        $commands = @{}
        foreach ($name in "python", "py", "docker", "git", "node", "npm", "iisreset") {
            $command = Get-Command $name -ErrorAction SilentlyContinue | Select-Object -First 1
            $commands[$name] = if ($command) { $command.Source } else { $null }
        }
        $sites = @()
        try {
            Import-Module WebAdministration -ErrorAction Stop
            $sites = Get-Website | ForEach-Object {
                [pscustomobject]@{
                    name = $_.Name
                    state = $_.State.ToString()
                    physical_path = $_.PhysicalPath
                    bindings = @($_.Bindings.Collection | ForEach-Object {
                        [pscustomobject]@{
                            protocol = $_.protocol
                            binding_information = $_.bindingInformation
                        }
                    })
                }
            }
        } catch {
            $sites = @()
        }
        $containers = @()
        if ($commands.docker) {
            try {
                $containers = @(& $commands.docker ps --format '{{json .}}' | ForEach-Object {
                    $_ | ConvertFrom-Json
                })
            } catch {
                $containers = @()
            }
        }
        [pscustomobject]@{
            connected = $true
            hostname = $env:COMPUTERNAME
            user = (whoami)
            administrator = $isAdmin
            os = (Get-CimInstance Win32_OperatingSystem).Caption
            powershell = $PSVersionTable.PSVersion.ToString()
            commands = $commands
            iis_sites = $sites
            containers = $containers
            listening_ports = @(
                Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
                    Select-Object -ExpandProperty LocalPort -Unique |
                    Sort-Object
            )
            relevant_services = @(
                Get-Service -ErrorAction SilentlyContinue |
                    Where-Object { $_.Name -match 'W3SVC|IIS|docker|RadioTEDU|voter|jukebox' } |
                    Select-Object Name, DisplayName, Status, StartType
            )
            root_directories = @(
                Get-ChildItem C:\ -Directory -Force -ErrorAction SilentlyContinue |
                    Select-Object -ExpandProperty Name
            )
        }
    }
    $remote | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $OutputPath -Encoding UTF8
    Write-Host "CONNECTED: redacted discovery written to $OutputPath"
} catch {
    [pscustomobject]@{
        connected = $false
        error_type = $_.Exception.GetType().Name
        category = $_.CategoryInfo.Category.ToString()
    } | ConvertTo-Json | Set-Content -LiteralPath $OutputPath -Encoding UTF8
    throw
} finally {
    if ($null -ne $session) { Remove-PSSession $session -ErrorAction SilentlyContinue }
    $credential = $null
}
