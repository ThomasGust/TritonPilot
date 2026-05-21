[CmdletBinding()]
param(
    [string]$TetherAlias = "Ethernet 3",
    [string]$InternetAlias = "",
    [string]$TetherAddress = "192.168.1.1",
    [int]$PrefixLength = 24,
    [string]$RovAddress = "192.168.1.4",
    [string]$NatName = "TritonROV",
    [switch]$ProbeOnly,
    [switch]$TuneAdapter,
    [switch]$ResetAdapter
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

function Get-TetherPrefix {
    param(
        [string]$Address,
        [int]$Length
    )

    if ($Length -ne 24) {
        throw "This helper currently expects a /24 tether prefix. Got /$Length."
    }
    $octets = $Address.Split(".")
    if ($octets.Count -ne 4) {
        throw "Invalid IPv4 address: $Address"
    }
    return "$($octets[0]).$($octets[1]).$($octets[2]).0/$Length"
}

function Get-DefaultInternetAlias {
    $route = Get-NetRoute -AddressFamily IPv4 -DestinationPrefix "0.0.0.0/0" |
        Where-Object { $_.NextHop -and $_.NextHop -ne "0.0.0.0" } |
        Sort-Object RouteMetric, InterfaceMetric |
        Select-Object -First 1
    if (-not $route) {
        throw "Could not find an IPv4 default route for the internet-facing interface."
    }
    return $route.InterfaceAlias
}

function Show-Probe {
    param(
        [string]$TetherAlias,
        [string]$InternetAlias,
        [string]$RovAddress,
        [string]$TetherAddress
    )

    Write-Step "Current adapters"
    Get-NetAdapter | Format-Table ifIndex, Name, InterfaceDescription, Status, LinkSpeed -AutoSize

    Write-Step "IPv4 configuration"
    Get-NetIPConfiguration |
        Format-List InterfaceAlias, InterfaceIndex, IPv4Address, IPv4DefaultGateway, DNSServer

    Write-Step "Forwarding state"
    Get-NetIPInterface -AddressFamily IPv4 |
        Sort-Object InterfaceAlias |
        Format-Table InterfaceAlias, ifIndex, Forwarding, Dhcp, ConnectionState, InterfaceMetric -AutoSize

    Write-Step "WinNAT state"
    Get-NetNat -ErrorAction SilentlyContinue |
        Format-Table Name, InternalIPInterfaceAddressPrefix, Active -AutoSize

    Write-Step "Tether neighbor table"
    Get-NetNeighbor -AddressFamily IPv4 -InterfaceAlias $TetherAlias -ErrorAction SilentlyContinue |
        Format-Table IPAddress, LinkLayerAddress, State -AutoSize

    Write-Step "Route selection"
    Get-NetRoute -AddressFamily IPv4 |
        Where-Object { $_.DestinationPrefix -eq "0.0.0.0/0" -or $_.DestinationPrefix -like "192.168.1.*" } |
        Sort-Object DestinationPrefix, InterfaceMetric |
        Format-Table DestinationPrefix, NextHop, InterfaceAlias, RouteMetric, InterfaceMetric -AutoSize

    Write-Step "ROV tether probe"
    ping -n 2 -S $TetherAddress $RovAddress

    Write-Step "Internet probe"
    Test-NetConnection -ComputerName github.com -Port 443
}

function Ensure-TetherAddress {
    param(
        [string]$Alias,
        [string]$Address,
        [int]$Length
    )

    $existing = Get-NetIPAddress -InterfaceAlias $Alias -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -eq $Address }
    if ($existing -and $existing.PrefixLength -eq $Length) {
        Write-Host "Tether address already present: $Address/$Length"
        return
    }

    Get-NetIPAddress -InterfaceAlias $Alias -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.PrefixOrigin -eq "Manual" -and $_.IPAddress -like "192.168.1.*" } |
        Remove-NetIPAddress -Confirm:$false

    New-NetIPAddress -InterfaceAlias $Alias -IPAddress $Address -PrefixLength $Length | Out-Null
}

function Ensure-WinNat {
    param(
        [string]$Name,
        [string]$Prefix
    )

    $nat = Get-NetNat -Name $Name -ErrorAction SilentlyContinue
    if ($nat) {
        if ($nat.InternalIPInterfaceAddressPrefix -ne $Prefix) {
            Write-Host "Replacing WinNAT '$Name' because its prefix is $($nat.InternalIPInterfaceAddressPrefix), not $Prefix."
            Remove-NetNat -Name $Name -Confirm:$false
            New-NetNat -Name $Name -InternalIPInterfaceAddressPrefix $Prefix | Out-Null
        }
        else {
            Write-Host "WinNAT already present: $Name ($Prefix)"
        }
        return
    }

    New-NetNat -Name $Name -InternalIPInterfaceAddressPrefix $Prefix | Out-Null
}

function Set-OptionalAdvancedProperty {
    param(
        [string]$Alias,
        [string]$RegistryKeyword,
        [string]$DisplayValue
    )

    $prop = Get-NetAdapterAdvancedProperty -Name $Alias -RegistryKeyword $RegistryKeyword -ErrorAction SilentlyContinue
    if (-not $prop) {
        return
    }
    if ($prop.DisplayValue -eq $DisplayValue) {
        return
    }

    try {
        Set-NetAdapterAdvancedProperty -Name $Alias -RegistryKeyword $RegistryKeyword -DisplayValue $DisplayValue -NoRestart
        Write-Host "Set $RegistryKeyword to $DisplayValue"
    }
    catch {
        Write-Warning "Could not set $RegistryKeyword to '$DisplayValue': $_"
    }
}

$prefix = Get-TetherPrefix -Address $TetherAddress -Length $PrefixLength
if (-not $InternetAlias) {
    $InternetAlias = Get-DefaultInternetAlias
}

Write-Host "Tether:   $TetherAlias ($TetherAddress/$PrefixLength)"
Write-Host "Internet: $InternetAlias"
Write-Host "ROV:      $RovAddress"
Write-Host "NAT:      $NatName ($prefix)"

if ($ProbeOnly) {
    Show-Probe -TetherAlias $TetherAlias -InternetAlias $InternetAlias -RovAddress $RovAddress -TetherAddress $TetherAddress
    exit 0
}

if (-not (Test-IsAdmin)) {
    throw "This script must run in an elevated PowerShell window. Re-run PowerShell as Administrator, then run this script again."
}

Write-Step "Validating interfaces"
$null = Get-NetAdapter -Name $TetherAlias -ErrorAction Stop
$null = Get-NetAdapter -Name $InternetAlias -ErrorAction Stop

Write-Step "Configuring tether IPv4 and forwarding"
Set-NetIPInterface -InterfaceAlias $TetherAlias -AddressFamily IPv4 -Dhcp Disabled
Ensure-TetherAddress -Alias $TetherAlias -Address $TetherAddress -Length $PrefixLength
Set-NetIPInterface -InterfaceAlias $TetherAlias -AddressFamily IPv4 -Forwarding Enabled -InterfaceMetric 25
Set-NetIPInterface -InterfaceAlias $InternetAlias -AddressFamily IPv4 -Forwarding Enabled
Get-NetRoute -InterfaceAlias $TetherAlias -DestinationPrefix "0.0.0.0/0" -ErrorAction SilentlyContinue |
    Remove-NetRoute -Confirm:$false

Write-Step "Configuring WinNAT"
Start-Service iphlpsvc
Ensure-WinNat -Name $NatName -Prefix $prefix

if ($TuneAdapter) {
    Write-Step "Applying conservative USB Ethernet tuning"
    Set-OptionalAdvancedProperty -Alias $TetherAlias -RegistryKeyword "*PMARPOffload" -DisplayValue "Disabled"
    Set-OptionalAdvancedProperty -Alias $TetherAlias -RegistryKeyword "*PMNSOffload" -DisplayValue "Disabled"
    Set-OptionalAdvancedProperty -Alias $TetherAlias -RegistryKeyword "*SelectiveSuspend" -DisplayValue "Disabled"
    Set-OptionalAdvancedProperty -Alias $TetherAlias -RegistryKeyword "SuspendAutoDetach" -DisplayValue "Disabled"
    Set-OptionalAdvancedProperty -Alias $TetherAlias -RegistryKeyword "SuspendLowPower" -DisplayValue "Disabled"
}

if ($ResetAdapter) {
    Write-Step "Resetting tether adapter"
    Disable-NetAdapter -Name $TetherAlias -Confirm:$false
    Start-Sleep -Seconds 2
    Enable-NetAdapter -Name $TetherAlias -Confirm:$false
    Start-Sleep -Seconds 4
}

Show-Probe -TetherAlias $TetherAlias -InternetAlias $InternetAlias -RovAddress $RovAddress -TetherAddress $TetherAddress
