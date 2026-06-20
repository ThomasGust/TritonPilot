#requires -RunAsAdministrator
<#
  ensure_rov_nat.ps1
  Share the laptop's internet with the ROV (Raspberry Pi) over the USB-Ethernet
  tether, regardless of which upstream Wi-Fi / phone hotspot the laptop is on.

  How it works: the laptop holds 192.168.1.1 on the tether NIC and runs a WinNAT
  instance that NATs the whole 192.168.1.0/24 link out of whatever interface
  currently owns the default route. The Pi (192.168.1.4, static) uses 192.168.1.1
  as its gateway. So in the field you only ever join the *laptop* to the new
  network -- the Pi needs no changes.

  This script is idempotent: safe to run at boot, at logon, on a timer, or by hand.
  It only asserts state, never tears down a working config.

  Link facts (keep in sync with the Pi's NetworkManager "Wired connection 1"):
    laptop tether IP : 192.168.1.1/24
    Pi eth0          : 192.168.1.4/24  (gateway 192.168.1.1, dns 8.8.8.8/1.1.1.1)
    NAT name         : TritonROV  ->  192.168.1.0/24
#>

$ErrorActionPreference = 'Stop'

$LinkIp      = '192.168.1.1'
$PrefixLen   = 24
$NatName     = 'TritonROV'
$NatPrefix   = '192.168.1.0/24'
# Match the tether by hardware description: the Windows alias ("Ethernet 6") can
# change if it's plugged into a different USB port, but the description is stable.
$AdapterDesc = 'Realtek USB GbE*'

function Log($m) { Write-Host ("[ensure-rov-nat] {0}" -f $m) }

# 1) Locate the USB-Ethernet tether adapter.
$ad = Get-NetAdapter -Physical -ErrorAction SilentlyContinue |
      Where-Object { $_.InterfaceDescription -like $AdapterDesc } |
      Select-Object -First 1
if (-not $ad) {
    Log "Tether adapter ('$AdapterDesc') not found -- USB cable unplugged? Nothing to do."
    exit 0
}
Log ("Tether: '{0}' ({1}) status={2} ifIndex={3}" -f $ad.Name, $ad.InterfaceDescription, $ad.Status, $ad.ifIndex)

# 2) Ensure the static link IP 192.168.1.1/24 is present on the tether.
$hasIp = Get-NetIPAddress -InterfaceIndex $ad.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
         Where-Object { $_.IPAddress -eq $LinkIp }
if (-not $hasIp) {
    Log "Setting static $LinkIp/$PrefixLen on tether (dedicated ROV link)."
    Get-NetIPAddress -InterfaceIndex $ad.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Remove-NetIPAddress -Confirm:$false -ErrorAction SilentlyContinue
    New-NetIPAddress -InterfaceIndex $ad.ifIndex -IPAddress $LinkIp -PrefixLength $PrefixLen | Out-Null
} else {
    Log "Static link IP already present."
}

# 3) Enable IPv4 forwarding on the tether (the NAT's internal side).
Set-NetIPInterface -InterfaceIndex $ad.ifIndex -AddressFamily IPv4 -Forwarding Enabled -ErrorAction SilentlyContinue
Log "Forwarding enabled on tether."

# 4) Ensure the WinNAT instance exists with the right prefix.
$nat = Get-NetNat -Name $NatName -ErrorAction SilentlyContinue
if (-not $nat) {
    Log "Creating WinNAT '$NatName' for $NatPrefix."
    New-NetNat -Name $NatName -InternalIPInterfaceAddressPrefix $NatPrefix | Out-Null
} elseif ($nat.InternalIPInterfaceAddressPrefix -ne $NatPrefix) {
    Log "WinNAT '$NatName' has prefix '$($nat.InternalIPInterfaceAddressPrefix)'; recreating as $NatPrefix."
    $nat | Remove-NetNat -Confirm:$false
    New-NetNat -Name $NatName -InternalIPInterfaceAddressPrefix $NatPrefix | Out-Null
} else {
    Log "WinNAT '$NatName' present ($NatPrefix)."
}

# 5) WinNAT occasionally goes inactive after sleep/wake or a network switch.
#    If so, bounce the service so the persisted NAT comes back.
$nat = Get-NetNat -Name $NatName -ErrorAction SilentlyContinue
if ($nat -and -not $nat.Active) {
    Log "WinNAT inactive -- restarting 'winnat' service."
    Restart-Service winnat -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
}

$nat = Get-NetNat -Name $NatName -ErrorAction SilentlyContinue
Log ("Result: NAT active={0}" -f ($nat -and $nat.Active))
exit 0
