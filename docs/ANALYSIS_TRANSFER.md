# Analysis Transfer Link

TritonPilot can expose saved recordings to TritonAnalysis over a dedicated
analysis Ethernet link. This is a file handoff path only; it does not carry ROV
control, telemetry, or live video.

## Recommended Network

Use USB-to-Ethernet adapters or a small unmanaged switch between the pilot and
analysis laptops:

```text
Pilot analysis adapter    10.77.0.1/24
Analysis adapter          10.77.0.2/24
Gateway                   leave blank
DNS                       leave blank
```

Keep the normal ROV tether on its existing `192.168.1.x` network.

## Adapter Setup Script

Use the helper script when setting up a new laptop, a new USB Ethernet adapter,
or a Windows adapter whose name changed.

First list adapters and current network state:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\setup_analysis_link.ps1 -ProbeOnly
```

On the TritonPilot computer, run from an elevated/Admin PowerShell:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\setup_analysis_link.ps1 -Role Pilot -AdapterAlias "Ethernet 4"
```

On the TritonAnalysis computer, run from an elevated/Admin PowerShell, changing
the adapter name to match that computer:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\setup_analysis_link.ps1 -Role Analysis -AdapterAlias "Ethernet"
```

The script:

- Disables DHCP on the selected adapter.
- Sets the static address for the selected role.
- Removes stale `169.254.x.x` and conflicting `10.77.0.x` addresses on that
  adapter.
- Leaves the gateway and DNS blank.
- Sets the network profile to Private when Windows allows it.
- Adds a direct peer route for the other laptop.
- Adds Private-profile firewall rules for ping, and for TCP `8765` on the
  Pilot side.

After setup, this should pass on the Analysis computer:

```powershell
Test-NetConnection 10.77.0.1 -Port 8765
Invoke-RestMethod http://10.77.0.1:8765/health
```

## Integrated App Status

The TritonPilot app starts the transfer server automatically. The status bar
shows:

```text
Analysis Share: ON http://10.77.0.1:8765 | recordings | 12 files/840.0 MB | last pull 3s
```

While TritonAnalysis is downloading a file, the final field changes to a
`sending ...` message. After a file finishes, it briefly shows the last sent
filename and byte count before returning to the normal last-pull age.

Use the `Transfer` menu to start, stop, restart, or copy the URL. If the label
says `waiting for Analysis`, the pilot computer is serving files but the
analysis computer has not pulled the index yet.

Useful environment overrides:

```powershell
$env:TRITON_PILOT_TRANSFER_AUTOSTART="0"
$env:TRITON_PILOT_TRANSFER_HOST="0.0.0.0"
$env:TRITON_PILOT_TRANSFER_ADVERTISE_HOST="10.77.0.1"
$env:TRITON_PILOT_TRANSFER_PORT="8765"
$env:TRITON_PILOT_TRANSFER_ROOT="C:\TritonRecordings"
```

## Backup CLI Server

From the TritonPilot repository root:

```powershell
python -m tools.analysis_transfer_server --root recordings --host 0.0.0.0 --port 8765
```

The server is read-only. It publishes:

- `http://10.77.0.1:8765/health`
- `http://10.77.0.1:8765/index.json`
- `http://10.77.0.1:8765/files/<relative-path>`

Files modified in the last two seconds are skipped by default so an active
recording is less likely to be copied mid-write. For bench simulation only, you
can lower that:

```powershell
python -m tools.analysis_transfer_server --root recordings --host 127.0.0.1 --port 8765 --stable-seconds 0
```

## Pull From TritonAnalysis

The unified TritonAnalysis app pulls automatically and shows its destination in
the top `Pilot Sync` panel. From the TritonAnalysis repository root on the
analysis computer, this CLI command is still available as a backup:

```powershell
python -m tools.pilot_transfer_sync http://10.77.0.1:8765 --output C:\TritonCompetitionMedia\incoming
```

Use `--dry-run` first when you want to confirm what will copy:

```powershell
python -m tools.pilot_transfer_sync http://10.77.0.1:8765 --output C:\TritonCompetitionMedia\incoming --dry-run
```

## One-Computer Simulation

You can test without a second laptop:

1. In a TritonPilot terminal:

   ```powershell
   python -m tools.analysis_transfer_server --root recordings --host 127.0.0.1 --port 8765 --stable-seconds 0
   ```

2. In a TritonAnalysis terminal on the same computer:

   ```powershell
   python -m tools.pilot_transfer_sync http://127.0.0.1:8765 --output .transfer-test --dry-run
   python -m tools.pilot_transfer_sync http://127.0.0.1:8765 --output .transfer-test
   ```

Delete `.transfer-test` after the simulation if you do not need the copied
files.

## Field Notes

- Start the transfer server before the run and leave it open.
- Pull from TritonAnalysis after a capture/session completes.
- Avoid making the pilot station depend on the analysis laptop.
- Keep this analysis link on `10.77.0.x`; keep the ROV tether on
  `192.168.1.x`.
- Do not put a gateway on either analysis-link adapter. Wi-Fi or another
  adapter should keep handling internet.
- If Windows Firewall prompts, allow private-network access for Python on the
  dedicated analysis link.
