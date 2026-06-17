# TritonPilot

TritonPilot is the topside operator application for Triton's ROV. It runs on
the pilot computer, reads the game controller, publishes pilot commands to the
ROV, receives live telemetry, starts and stops onboard camera streams, displays
GStreamer video panes, records stream/sensor logs, and exposes operator-facing
management tools.

Mission-specific analysis applets are intentionally not part of this
repository. During competition, detection, measurement, and scoring workflows
run from the sibling `TritonAnalysis` repository on a separate analysis
computer. TritonPilot should stay focused on live vehicle operation and data
logging.

## What Runs On The Pilot Computer

`main_topside.py` starts the PyQt6 GUI and wires together the main topside
services:

- Controller input through `pygame`
- Pilot command publishing to TritonOS on `tcp://<ROV_HOST>:6000`
- Sensor telemetry subscription from TritonOS on `tcp://<ROV_HOST>:6001`
- Video stream control through the TritonOS video RPC endpoint on port `5555`
- Management/calibration tools through the TritonOS management RPC endpoint on
  port `5556`
- Local UDP video receive pipelines using GStreamer
- Stream-log and raw-sensor CSV recording

The normal runtime relationship is:

```text
Xbox-style controller
        |
        v
TritonPilot PilotPublisherService
        |
        v
PilotFrame JSON over ZeroMQ
        |
        v
TritonOS onboard control loop
```

Telemetry, management RPC, and video streaming are separate paths so problems
can be diagnosed independently.

## Repository Layout

```text
main_topside.py      Main GUI entry point
config.py           Runtime endpoints, controller tuning, and UI defaults
data/streams.json   Camera stream definitions expected by the pilot UI
gui/                PyQt windows, panels, video tabs, instruments, raw sensors
input/              Controller discovery and PilotFrame publishing
telemetry/          Sensor subscriber and topside attitude estimator
video/              GStreamer receive path, camera manager, frame correction
recording/          JSONL stream logs, raw CSV telemetry, save locations
stereo/             Stereo calibration and disparity helpers
network/            Management RPC, local network selection, ZMQ helpers
schema/             Shared pilot-control wire schema
tools/              Controller, telemetry, video, and tether diagnostics
tests/              Hardware-free unit and GUI behavior tests
docs/               Maintained setup, operations, and architecture docs
```

## Start Here

- [Documentation Index](docs/README.md)
- [Setup Guide](docs/SETUP.md)
- [Desktop App Build](docs/DESKTOP_APP.md)
- [Network Guide](docs/NETWORKING.md)
- [Analysis Transfer Link](docs/ANALYSIS_TRANSFER.md)
- [Operations Guide](docs/OPERATIONS.md)
- [Architecture Overview](docs/ARCHITECTURE.md)
- [Subsystem Reference](docs/SUBSYSTEMS.md)
- [Configuration Guide](docs/CONFIGURATION.md)
- [Testing And Troubleshooting](docs/TESTING_AND_TROUBLESHOOTING.md)

## Development Quick Start

On Windows, from the repository root:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_windows.ps1
.\.venv\Scripts\activate
python .\main_topside.py
```

For a manual Python-only development environment:

```powershell
python -m venv .venv
.\.venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install pytest
python -m pytest
```

Video reception needs the external `gst-launch-1.0` executable. The Windows
setup script can install and validate GStreamer; see the
[Setup Guide](docs/SETUP.md) for details.

## Network Defaults

The normal tethered layout is:

- Pilot computer tether adapter: `192.168.1.1`
- ROV Ethernet: `192.168.1.4`
- Pilot commands: ROV port `6000`
- Sensor telemetry: ROV port `6001`
- Video RPC: ROV port `5555`
- Management RPC: ROV port `5556`
- UDP video receive ports on the pilot computer: `5000` through `5003`
- Optional network diagnostics: ROV port `7700`

If the ROV address differs, set:

```powershell
$env:ROV_HOST="192.168.1.4"
```

See [Network Guide](docs/NETWORKING.md) for tether routing, firewall
requirements, stream ports, and diagnostics.

## Competition Workflow

Use TritonPilot to operate the vehicle and save operational data. Use
TritonAnalysis to interpret mission-specific images, videos, measurements, and
manually entered task data.
A healthy workflow is:

1. Start TritonOS on the ROV.
2. Start TritonPilot on the pilot computer.
3. Verify controller, telemetry, management RPC, and video.
4. Record the stream/raw sensor logs needed by the team.
5. Hand saved logs or measurements to the analysis computer, either by USB
   drive or the integrated read-only
   [Analysis Transfer Link](docs/ANALYSIS_TRANSFER.md). TritonPilot shows the
   transfer server state as `Analysis Share` in the status bar.

TritonPilot is allowed to display raw data and diagnostics, but it should not
grow mission-scoring applets. That separation keeps the piloting station
predictable under competition pressure.

## Safety Notes

TritonPilot can send arm/disarm edges, hold-mode requests, gain changes, and
manipulator commands. The ROV-side TritonOS service remains responsible for
arming checks, command freshness, output limits, and hardware safety, but the
pilot computer is still part of the live control chain.

Before water tests, verify the controller mapping, BACK/ROV/ARM gain settings,
telemetry freshness, video orientation, and save location. During bench tests, keep the
vehicle secured and remove props whenever thrusters may spin unexpectedly.
