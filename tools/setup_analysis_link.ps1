[CmdletBinding()]
param(
    [ValidateSet("Pilot", "Analysis")]
    [string]$Role = "Pilot",
    [string]$AdapterAlias = "",
    [string]$PilotAddress = "10.77.0.1",
    [string]$AnalysisAddress = "10.77.0.2",
    [int]$PrefixLength = 24,
    [int]$TransferPort = 8765,
    [switch]$ProbeOnly,
    [switch]$NoFirewall,
    [switch]$SkipPrivateProfile,
    [switch]$KeepApipa,
    [switch]$KeepExistingTritonAddresses
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

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

function Get-RoleAddress {
    param([string]$RoleName)
    if ($RoleName -eq "Pilot") {
        return $PilotAddress
    }
    return $AnalysisAddress
}

function Get-PeerAddress {
    param([string]$RoleName)
    if ($RoleName -eq "Pilot") {
        return $AnalysisAddress
    }
    return $PilotAddress
}

function Show-Probe {
    param(
        [string]$Alias,
        [string]$RoleName
    )

    $localAddress = Get-RoleAddress $RoleName
    $peerAddress = Get-PeerAddress $RoleName

    Write-Step "Adapters"
    Get-NetAdapter |
        Sort-Object ifIndex |
        Format-Table -Auto ifIndex, Name, InterfaceDescription, Status, LinkSpeed, MacAddress

    Write-Step "IPv4 configuration"
    Get-NetIPConfiguration |
        Format-List InterfaceAlias, InterfaceIndex, IPv4Address, IPv4DefaultGateway, DNSServer

    Write-Step "Network profiles"
    Get-NetConnectionProfile |
        Format-Table -Auto Name, InterfaceAlias, InterfaceIndex, NetworkCategory, IPv4Connectivity

    Write-Step "Triton analysis routes"
    Get-NetRoute -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.DestinationPrefix -like "10.77.*" -or $_.DestinationPrefix -eq "0.0.0.0/0" } |
        Sort-Object DestinationPrefix, RouteMetric, InterfaceMetric |
        Format-Table -Auto ifIndex, InterfaceAlias, DestinationPrefix, NextHop, RouteMetric, InterfaceMetric

    if ($Alias) {
        Write-Step "Neighbor table for $Alias"
        Get-NetNeighbor -InterfaceAlias $Alias -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Sort-Object IPAddress |
            Format-Table -Auto IPAddress, LinkLayerAddress, State
    }

    if ($RoleName -eq "Pilot") {
        Write-Step "Transfer server listener on TCP $TransferPort"
        $conns = Get-NetTCPConnection -LocalPort $TransferPort -ErrorAction SilentlyContinue |
            Where-Object { $_.State -eq "Listen" }
        if ($conns) {
            $conns | Format-Table -Auto LocalAddress, LocalPort, State, OwningProcess
            $pids = $conns | Select-Object -ExpandProperty OwningProcess -Unique |
                Where-Object { $_ -gt 0 }
            Get-Process -Id $pids -ErrorAction SilentlyContinue |
                Format-Table -Auto Id, ProcessName, Path
        }
        else {
            Write-Host "No process is listening on TCP $TransferPort."
        }
    }

    if ($Alias) {
        Write-Step "Pinned peer ping from $localAddress to $peerAddress"
        ping -n 2 -S $localAddress $peerAddress
    }

    if ($RoleName -eq "Analysis") {
        Write-Step "Pilot transfer port probe"
        Test-NetConnection -ComputerName $PilotAddress -Port $TransferPort
    }
}

function Ensure-AdapterAlias {
    param([string]$Alias)
    if (-not $Alias) {
        throw "Pass -AdapterAlias with the dedicated USB Ethernet adapter name. Use -ProbeOnly to list adapters."
    }
    $adapter = Get-NetAdapter -Name $Alias -ErrorAction Stop
    if ($adapter.Status -ne "Up") {
        Write-Host "Warning: adapter '$Alias' status is $($adapter.Status), not Up." -ForegroundColor Yellow
    }
    return $adapter
}

function Ensure-StaticAddress {
    param(
        [string]$Alias,
        [string]$Address,
        [int]$Length
    )

    Write-Step "Configuring $Alias as $Address/$Length"
    Set-NetIPInterface -InterfaceAlias $Alias -AddressFamily IPv4 -Dhcp Disabled

    if (-not $KeepApipa) {
        Get-NetIPAddress -InterfaceAlias $Alias -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object { $_.IPAddress -like "169.254.*" } |
            Remove-NetIPAddress -Confirm:$false -ErrorAction SilentlyContinue
    }

    if (-not $KeepExistingTritonAddresses) {
        Get-NetIPAddress -InterfaceAlias $Alias -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object { $_.IPAddress -like "10.77.0.*" -and $_.IPAddress -ne $Address } |
            Remove-NetIPAddress -Confirm:$false -ErrorAction SilentlyContinue
    }

    $existing = Get-NetIPAddress -InterfaceAlias $Alias -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -eq $Address }
    if ($existing) {
        Write-Host "Static address already present: $Address/$($existing.PrefixLength)"
    }
    else {
        New-NetIPAddress -InterfaceAlias $Alias -IPAddress $Address -PrefixLength $Length | Out-Null
        Write-Host "Added static address: $Address/$Length"
    }

    Set-DnsClientServerAddress -InterfaceAlias $Alias -ResetServerAddresses
}

function Ensure-PrivateProfile {
    param([string]$Alias)
    if ($SkipPrivateProfile) {
        return
    }
    try {
        Set-NetConnectionProfile -InterfaceAlias $Alias -NetworkCategory Private
        Write-Host "Network profile set to Private."
    }
    catch {
        Write-Host "Could not set network profile to Private yet: $($_.Exception.Message)" -ForegroundColor Yellow
        Write-Host "This can happen before Windows creates a profile for a newly connected adapter." -ForegroundColor Yellow
    }
}

function Ensure-HostRoute {
    param(
        [string]$Alias,
        [string]$PeerAddress
    )

    $prefix = "$PeerAddress/32"
    $existing = Get-NetRoute -DestinationPrefix $prefix -InterfaceAlias $Alias -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Host "Peer host route already present: $prefix via $Alias"
        return
    }

    try {
        New-NetRoute -DestinationPrefix $prefix -InterfaceAlias $Alias -NextHop "0.0.0.0" -RouteMetric 1 -PolicyStore PersistentStore | Out-Null
    }
    catch {
        New-NetRoute -DestinationPrefix $prefix -InterfaceAlias $Alias -NextHop "0.0.0.0" -RouteMetric 1 | Out-Null
    }
    Write-Host "Added peer host route: $prefix via $Alias"
}

function Ensure-Firewall {
    param([string]$RoleName)
    if ($NoFirewall) {
        return
    }

    Write-Step "Configuring firewall rules"

    $icmpRuleName = "Triton Analysis Link ICMPv4 Echo"
    $icmpRule = Get-NetFirewallRule -DisplayName $icmpRuleName -ErrorAction SilentlyContinue
    if (-not $icmpRule) {
        New-NetFirewallRule -DisplayName $icmpRuleName -Direction Inbound -Action Allow -Protocol ICMPv4 -IcmpType 8 -Profile Any | Out-Null
        Write-Host "Added firewall rule: $icmpRuleName"
    }
    else {
        Set-NetFirewallRule -DisplayName $icmpRuleName -Profile Any -Enabled True -Action Allow | Out-Null
        Write-Host "Updated firewall rule: $icmpRuleName"
    }

    if ($RoleName -eq "Pilot") {
        $tcpRuleName = "TritonPilot Analysis Transfer TCP $TransferPort"
        $tcpRule = Get-NetFirewallRule -DisplayName $tcpRuleName -ErrorAction SilentlyContinue
        if (-not $tcpRule) {
            New-NetFirewallRule -DisplayName $tcpRuleName -Direction Inbound -Action Allow -Protocol TCP -LocalPort $TransferPort -Profile Any | Out-Null
            Write-Host "Added firewall rule: $tcpRuleName"
        }
        else {
            Set-NetFirewallRule -DisplayName $tcpRuleName -Profile Any -Enabled True -Action Allow | Out-Null
            Write-Host "Updated firewall rule: $tcpRuleName"
        }
    }
}

$localAddress = Get-RoleAddress $Role
$peerAddress = Get-PeerAddress $Role

if ($ProbeOnly) {
    Show-Probe -Alias $AdapterAlias -RoleName $Role
    exit 0
}

if (-not (Test-IsAdmin)) {
    throw "Run this script from an elevated/Admin PowerShell when not using -ProbeOnly."
}

Ensure-AdapterAlias -Alias $AdapterAlias | Out-Null
Ensure-StaticAddress -Alias $AdapterAlias -Address $localAddress -Length $PrefixLength
Ensure-PrivateProfile -Alias $AdapterAlias
Ensure-HostRoute -Alias $AdapterAlias -PeerAddress $peerAddress
Ensure-Firewall -RoleName $Role

Write-Step "Final analysis-link state"
Get-NetIPConfiguration -InterfaceAlias $AdapterAlias |
    Format-List InterfaceAlias, InterfaceIndex, IPv4Address, IPv4DefaultGateway, DNSServer
Get-NetConnectionProfile -InterfaceAlias $AdapterAlias |
    Format-Table -Auto Name, InterfaceAlias, NetworkCategory, IPv4Connectivity

Write-Step "Next checks"
if ($Role -eq "Pilot") {
    Write-Host "Start TritonPilot, then on the Analysis computer use:"
    Write-Host "  Test-NetConnection $PilotAddress -Port $TransferPort"
    Write-Host "  Invoke-RestMethod http://$PilotAddress`:$TransferPort/health"
    Write-Host "  python -m main_triton_analysis --pilot-transfer-url http://$PilotAddress`:$TransferPort"
}
else {
    Write-Host "From this Analysis computer, verify the Pilot transfer server:"
    Write-Host "  Test-NetConnection $PilotAddress -Port $TransferPort"
    Write-Host "  Invoke-RestMethod http://$PilotAddress`:$TransferPort/health"
    Write-Host "  python -m main_triton_analysis --pilot-transfer-url http://$PilotAddress`:$TransferPort"
}
