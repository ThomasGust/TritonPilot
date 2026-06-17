# Pilot Tether Adapter Setup

Use this guide when the pilot computer has the USB/Ethernet tether adapter
plugged in and the ROV is connected or powered.

The normal live-control link is:

```text
Pilot tether adapter    192.168.1.1/24
ROV eth0                192.168.1.4/24
Gateway/DNS             blank on the pilot tether adapter
```

Do not put a default gateway on the pilot tether adapter. The laptop should
keep using Wi-Fi or another adapter for internet access unless you explicitly
configure tether NAT.

## 1. Find The Adapter Name

Run this from the TritonPilot repository root:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\setup_pilot_tether_adapter.ps1 -ProbeOnly
```

Look for the USB/Ethernet adapter in the `Current adapters` table. Windows names
it something like `Ethernet`, `Ethernet 2`, or `USB 10/100/1000 LAN`.

## 2. Configure The Pilot Tether Adapter

Open PowerShell as Administrator and run the script with the adapter name you
found:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\setup_pilot_tether_adapter.ps1 -AdapterAlias "Ethernet 2"
```

The script will:

- Disable DHCP on that adapter.
- Set `192.168.1.1/24`.
- Remove a default route from that adapter so it does not steal internet
  traffic.
- Add Windows Firewall allows for display camera UDP ports `5000-5003` and the
  optional analysis-transfer TCP port `8765`.
- Probe ROV TCP ports `6000`, `6001`, `5555`, and `5556` from the tether
  address.

If the adapter has other IPv4 addresses that must be kept for a bench setup,
add `-KeepExistingIPv4`.

## 3. Verify ROV Reachability

With TritonOS running on the ROV, the end of the script should show successful
responses for:

```text
tcp/6000  pilot command intake
tcp/6001  sensor telemetry publisher
tcp/5555  video stream RPC
tcp/5556  management RPC
```

If the script configures the adapter but probes fail:

- Confirm the tether cable and switch are connected.
- Confirm the ROV is powered and TritonOS is running.
- Confirm the ROV Ethernet address is `192.168.1.4/24`.
- Try `ping 192.168.1.4` from the pilot computer.
- Re-run the probe:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\setup_pilot_tether_adapter.ps1 -ProbeOnly -AdapterAlias "Ethernet 2"
```

## 4. Launch TritonPilot

From the repository root:

```powershell
.\.venv\Scripts\Activate.ps1
$env:ROV_HOST="192.168.1.4"
python .\main_topside.py
```

If the ROV uses a different address, pass it to the setup script and set the
same `ROV_HOST` before launching:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\setup_pilot_tether_adapter.ps1 -AdapterAlias "Ethernet 2" -RovAddress "192.168.1.10"
$env:ROV_HOST="192.168.1.10"
```

## Optional Internet Sharing

The pilot tether adapter setup above is enough for live control. If the ROV also
needs internet access through the pilot computer for package downloads, updates,
or time sync, use the NAT helper after the basic tether link works:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\setup_tether_nat.ps1 -ProbeOnly -TetherAlias "Ethernet 2"
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\setup_tether_nat.ps1 -TetherAlias "Ethernet 2" -TuneAdapter
```

Only change the ROV default route after the tether gateway responds.
