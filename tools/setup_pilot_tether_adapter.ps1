[CmdletBinding()]
param(
    [string]$AdapterAlias = "",
    [string]$TetherAddress = "192.168.1.1",
    [int]$PrefixLength = 24,
    [string]$RovAddress = "192.168.1.4",
    [switch]$ProbeOnly,
    [switch]$KeepExistingIPv4,
    [switch]$SkipFirewall,
    [switch]$ResetAdapter
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$script:RovTcpPorts = @(6000, 6001, 5555, 5556)

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Test-IsAdmin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Assert-IPv4 {
    param([string]$Address)
    $parsed = $null
    if (-not [System.Net.IPAddress]::TryParse($Address, [ref]$parsed)) {
        throw "Invalid IPv4 address: $Address"
    }
    if ($parsed.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork) {
        throw "Expected IPv4 address, got: $Address"
    }
}

function Test-TcpPortFromSource {
    param(
        [string]$HostName,
        [int]$Port,
        [string]$SourceAddress,
        [int]$TimeoutMs = 1200
    )

    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        if ($SourceAddress) {
            $localIp = [System.Net.IPAddress]::Parse($SourceAddress)
            $client.Client.Bind([System.Net.IPEndPoint]::new($localIp, 0))
        }
        $async = $client.BeginConnect($HostName, $Port, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne($TimeoutMs)) {
            return $false
        }
        $client.EndConnect($async)
        return $true
    }
    catch {
        return $false
    }
    finally {
        try {
            $client.Close()
        }
        catch {
        }
    }
}

function Ensure-FirewallRule {
    param(
        [string]$DisplayName,
        [ValidateSet("TCP", "UDP")]
        [string]$Protocol,
        [string]$LocalPort
    )

    $rule = Get-NetFirewallRule -DisplayName $DisplayName -ErrorAction SilentlyContinue
    if (-not $rule) {
        New-NetFirewallRule `
            -DisplayName $DisplayName `
            -Direction Inbound `
            -Action Allow `
            -Protocol $Protocol `
            -LocalPort $LocalPort `
            -Profile Any | Out-Null
        Write-Host "Created firewall rule: $DisplayName"
        return
    }

    Set-NetFirewallRule -DisplayName $DisplayName -Enabled True -Direction Inbound -Action Allow -Profile Any | Out-Null
    Get-NetFirewallPortFilter -AssociatedNetFirewallRule $rule |
        Set-NetFirewallPortFilter -Protocol $Protocol -LocalPort $LocalPort | Out-Null
    Write-Host "Verified firewall rule: $DisplayName"
}

function Ensure-TritonPilotFirewall {
    Write-Step "Configuring Windows Firewall"
    Ensure-FirewallRule -DisplayName "TritonPilot Camera UDP" -Protocol UDP -LocalPort "5000-5003"
    Ensure-FirewallRule -DisplayName "TritonPilot Analysis Transfer TCP" -Protocol TCP -LocalPort "8765"
}

function Show-AdapterProbe {
    param(
        [string]$Alias,
        [string]$PilotAddress,
        [string]$RovHost
    )

    Write-Step "Current adapters"
    Get-NetAdapter | Format-Table ifIndex, Name, InterfaceDescription, Status, LinkSpeed -AutoSize

    Write-Step "IPv4 configuration"
    Get-NetIPConfiguration |
        Format-List InterfaceAlias, InterfaceIndex, IPv4Address, IPv4DefaultGateway, DNSServer

    if (-not $Alias) {
        Write-Host ""
        Write-Host "Pick the USB/Ethernet tether adapter name, then run:" -ForegroundColor Yellow
        Write-Host "  powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\setup_pilot_tether_adapter.ps1 -AdapterAlias `"Ethernet X`""
        return
    }

    $adapter = Get-NetAdapter -Name $Alias -ErrorAction SilentlyContinue
    if (-not $adapter) {
        Write-Warning "Adapter '$Alias' was not found."
        return
    }

    Write-Step "Selected adapter"
    $adapter | Format-List Name, InterfaceDescription, Status, LinkSpeed, MacAddress

    Write-Step "Selected adapter IPv4"
    Get-NetIPAddress -InterfaceAlias $Alias -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Format-Table InterfaceAlias, IPAddress, PrefixLength, PrefixOrigin, AddressState -AutoSize

    Write-Step "Selected adapter routes"
    Get-NetRoute -InterfaceAlias $Alias -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Sort-Object DestinationPrefix, RouteMetric |
        Format-Table DestinationPrefix, NextHop, RouteMetric, InterfaceMetric -AutoSize

    $source = Get-NetIPAddress -InterfaceAlias $Alias -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -eq $PilotAddress } |
        Select-Object -ExpandProperty IPAddress -First 1

    if (-not $source) {
        Write-Warning "Pilot tether address $PilotAddress is not configured on '$Alias'; skipping source-bound ROV probes."
        return
    }

    Write-Step "ROV reachability from $source"
    ping -n 2 -S $source $RovHost
    foreach ($port in $script:RovTcpPorts) {
        $ok = Test-TcpPortFromSource -HostName $RovHost -Port $port -SourceAddress $source
        $status = if ($ok) { "OPEN" } else { "closed/no response" }
        Write-Host ("tcp/{0}: {1}" -f $port, $status)
    }
}

function Ensure-TetherAddress {
    param(
        [string]$Alias,
        [string]$Address,
        [int]$Length,
        [switch]$KeepExisting
    )

    Write-Step "Configuring static tether IPv4"
    Set-NetIPInterface -InterfaceAlias $Alias -AddressFamily IPv4 -Dhcp Disabled

    if (-not $KeepExisting) {
        Get-NetIPAddress -InterfaceAlias $Alias -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object { $_.IPAddress -ne $Address } |
            Remove-NetIPAddress -Confirm:$false
    }

    $existing = Get-NetIPAddress -InterfaceAlias $Alias -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -eq $Address } |
        Select-Object -First 1

    if ($existing) {
        if ([int]$existing.PrefixLength -ne [int]$Length) {
            Remove-NetIPAddress -InterfaceAlias $Alias -IPAddress $Address -Confirm:$false
            New-NetIPAddress -InterfaceAlias $Alias -IPAddress $Address -PrefixLength $Length | Out-Null
        }
        else {
            Write-Host "Tether address already present: $Address/$Length"
        }
    }
    else {
        New-NetIPAddress -InterfaceAlias $Alias -IPAddress $Address -PrefixLength $Length | Out-Null
        Write-Host "Added tether address: $Address/$Length"
    }

    Set-NetIPInterface -InterfaceAlias $Alias -AddressFamily IPv4 -InterfaceMetric 10
    Get-NetRoute -InterfaceAlias $Alias -DestinationPrefix "0.0.0.0/0" -ErrorAction SilentlyContinue |
        Remove-NetRoute -Confirm:$false

    try {
        Set-DnsClientServerAddress -InterfaceAlias $Alias -ResetServerAddresses
    }
    catch {
        Write-Warning "Could not reset DNS servers on '$Alias': $_"
    }
}

Assert-IPv4 -Address $TetherAddress
Assert-IPv4 -Address $RovAddress

Write-Host "Pilot tether address: $TetherAddress/$PrefixLength"
Write-Host "ROV address:          $RovAddress"
if ($AdapterAlias) {
    Write-Host "Adapter:              $AdapterAlias"
}
else {
    Write-Host "Adapter:              (not selected)"
}

if ($ProbeOnly) {
    Show-AdapterProbe -Alias $AdapterAlias -PilotAddress $TetherAddress -RovHost $RovAddress
    exit 0
}

if (-not $AdapterAlias) {
    throw "Pass -AdapterAlias with the exact tether adapter name. Run this script with -ProbeOnly first to list adapters."
}

if (-not (Test-IsAdmin)) {
    throw "This script must run in an elevated PowerShell window."
}

$null = Get-NetAdapter -Name $AdapterAlias -ErrorAction Stop

Ensure-TetherAddress -Alias $AdapterAlias -Address $TetherAddress -Length $PrefixLength -KeepExisting:$KeepExistingIPv4

if (-not $SkipFirewall) {
    Ensure-TritonPilotFirewall
}

if ($ResetAdapter) {
    Write-Step "Resetting tether adapter"
    Disable-NetAdapter -Name $AdapterAlias -Confirm:$false
    Start-Sleep -Seconds 2
    Enable-NetAdapter -Name $AdapterAlias -Confirm:$false
    Start-Sleep -Seconds 4
}

Show-AdapterProbe -Alias $AdapterAlias -PilotAddress $TetherAddress -RovHost $RovAddress

Write-Step "Done"
Write-Host "If the ROV probes pass, launch TritonPilot with:"
Write-Host "  .\.venv\Scripts\Activate.ps1"
Write-Host "  `$env:ROV_HOST=`"$RovAddress`""
Write-Host "  python .\main_topside.py"
