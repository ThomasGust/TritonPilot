# TritonPilot Network Guide

TritonPilot communicates with TritonOS over the tether. Control, telemetry,
video control, management, diagnostics, and video frame transport use separate
ports so each path can be tested independently.

## Normal Network Layout

```text
Pilot Wi-Fi adapter       Internet-facing network
Pilot tether adapter      192.168.1.1/24
ROV eth0                  192.168.1.4/24
```

The pilot computer runs TritonPilot. The ROV runs TritonOS.

## Default Ports

| Purpose | Direction | Default |
| --- | --- | --- |
| Pilot command frames | Pilot -> ROV | `tcp://192.168.1.4:6000` |
| Sensor telemetry | ROV -> Pilot subscriber | `tcp://192.168.1.4:6001` |
| Video RPC control | Pilot -> ROV | `tcp://192.168.1.4:5555` |
| Management RPC | Pilot -> ROV | `tcp://192.168.1.4:5556` |
| Network diagnostics | Pilot -> ROV | UDP/TCP `7700` |
| Primary camera video | ROV -> Pilot | UDP `5000` |
| Arm camera video | ROV -> Pilot | UDP `5001` |
| Aux camera video | ROV -> Pilot | UDP `5002` |
| Back gripper camera video | ROV -> Pilot | UDP `5003` |

Camera ports are defined in `data/streams.json`.

## Host And Endpoint Selection

`config.py` chooses the ROV host in this order:

1. Explicit `ROV_HOST`
2. Auto-detection candidates from `TRITON_ROV_HOSTS`
3. `TRITON_ROV_DEFAULT_HOST`, defaulting to `192.168.1.4`

Auto-detection probes ports `6001` and `5556`, which lets bench testing fall
back to `tritonpi.local` when the tether address is unavailable.

Common overrides:

```powershell
$env:ROV_HOST="192.168.1.4"
$env:TRITON_ROV_HOSTS="192.168.1.4,tritonpi.local"
$env:TRITON_ROV_AUTO_DETECT="0"
```

Endpoint-level overrides are also supported:

```powershell
$env:ROV_PILOT_EP="tcp://192.168.1.4:6000"
$env:ROV_SENSOR_EP="tcp://192.168.1.4:6001"
$env:ROV_VIDEO_RPC="tcp://192.168.1.4:5555"
$env:ROV_MANAGEMENT_RPC="tcp://192.168.1.4:5556"
```

## Windows Tether Internet Routing

The pilot computer can share internet access with the ROV over the tether. Use
this when the ROV needs package downloads, updates, or time sync while connected
through Ethernet.

Probe the Windows side without admin:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\setup_tether_nat.ps1 -ProbeOnly
```

Configure or repair NAT from an elevated PowerShell:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\setup_tether_nat.ps1 -TuneAdapter -ResetAdapter
```

Then, on the ROV, configure the tether gateway using the TritonOS script:

```bash
sudo bash bin/configure_tether_gateway.sh --probe
sudo bash bin/configure_tether_gateway.sh --persistent
```

Only switch the ROV default route after the tether gateway responds.

## Video Routing

TritonPilot asks TritonOS to start a named stream through the video RPC
endpoint. TritonOS then sends RTP/UDP video to the pilot computer on the port
listed in `data/streams.json`.

The local receive address is selected by `network/net_select.py`. It chooses a
local interface that can reach the ROV and is suitable for receiving the stream.
If the pilot has multiple adapters active, validate the selected route before
competition.

Firewall rules must allow inbound UDP traffic on the configured camera ports.

## Diagnostics

Check telemetry without the full GUI:

```powershell
python .\tools\sensor_stream_sub_test.py --endpoint tcp://192.168.1.4:6001
```

Run tether diagnostics:

```powershell
python .\tools\netdiag_client.py --host 192.168.1.4
```

Probe controller input:

```powershell
python .\tools\controller_probe.py
```

For the normal GUI, the status bar reports heartbeat, controller state, depth,
gain, mode, video, power, and network health. Use those indicators before
assuming a code failure.

## Common Network Symptoms

The GUI opens but telemetry is stale:

- Check that TritonOS is running.
- Check `ROV_HOST`.
- Confirm port `6001` is reachable.
- Try `tools/sensor_stream_sub_test.py`.

Video tabs connect but stay black:

- Confirm the video RPC endpoint on port `5555`.
- Confirm the pilot firewall allows inbound UDP camera ports.
- Check that GStreamer is installed and discoverable.
- Make sure `data/streams.json` matches the onboard camera layout.

Management page actions time out:

- Confirm port `5556`.
- Check TritonOS logs for the management RPC server.
- Avoid running multiple tools that compete for the same RPC endpoint.
