# TritonPilot Setup Guide

TritonPilot is primarily developed and operated from a Windows pilot computer.
The Python code is portable in many places, but the production video receive
path expects the Windows GStreamer runtime and Windows process behavior.

## Required Software

- Python 3.10 or newer
- `pip`
- A virtual environment
- GStreamer 1.0 with H.264, JPEG, RTP, decode, convert, and `fdsink` plugins
- A supported Xbox-style controller for live piloting

The Python dependency set is in `requirements.txt`. The platform wrapper files
`requirements-windows.txt` and `requirements-macos.txt` currently include the
same base requirements.

## Recommended Windows Setup

Run this from the TritonPilot repository root on the pilot computer:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_windows.ps1
```

The setup script:

- Creates or updates `.venv`
- Installs the Python dependencies
- Finds or installs GStreamer using the available Windows package manager
- Persists the GStreamer runtime path into the user's environment
- Verifies the GStreamer elements required by TritonPilot's receive pipelines

After setup, activate the environment and start the app:

```powershell
.\.venv\Scripts\activate
python .\main_topside.py
```

## Windows Desktop App Build

For pilot handoff, build the standalone desktop bundle instead of launching
from VSCode or a Python prompt:

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\build_windows_app.ps1 -Clean -CreateDesktopShortcut
```

The executable is written to `dist\TritonPilot\TritonPilot.exe` with the
golden trident icon. See [Desktop App Build](DESKTOP_APP.md) for rebuild and
runtime notes.

For a single executable that can be copied to Desktop without the `_internal`
folder, build:

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\build_windows_app.ps1 -Clean -OneFile
```

## Manual Python Setup

Use this path for development machines, CI-like checks, or systems where
GStreamer is managed separately:

```powershell
python -m venv .venv
.\.venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install pytest
```

Run tests:

```powershell
python -m pytest
```

Start the GUI:

```powershell
python .\main_topside.py
```

## GStreamer Setup

Video panes use `gst-launch-1.0` through `video/gst_receiver.py`. The receiver
searches common Windows install locations, environment variables, and `PATH`.
If detection fails, set the executable explicitly:

```powershell
$env:GST_LAUNCH="C:\Program Files\GStreamer\1.0\msvc_x86_64\bin\gst-launch-1.0.exe"
```

The install must include these elements:

- `rtph264depay`
- `h264parse`
- `avdec_h264`
- `rtpjpegdepay`
- `jpegdec`
- `videoconvert`
- `fdsink`

A complete x86_64 GStreamer install is the safest production choice.

## Controller Setup

Connect the controller before starting the app. TritonPilot uses `pygame` and
SDL for joystick access. To inspect the mapping:

```powershell
python .\tools\controller_probe.py
```

If the wrong joystick index is selected:

```powershell
$env:TRITON_CONTROLLER_INDEX="1"
python .\main_topside.py
```

If the axes are misdetected, force a mapping in the order
`lx,ly,rx,ry,lt,rt`:

```powershell
$env:TRITON_CONTROLLER_AXIS_MAP="0,1,3,4,2,5"
python .\main_topside.py
```

## ROV Services Required

For full operation, TritonOS must be running on the ROV and reachable from the
pilot computer. TritonPilot expects:

```text
tcp://<ROV_HOST>:6000   Pilot command intake
tcp://<ROV_HOST>:6001   Sensor telemetry publisher
tcp://<ROV_HOST>:5555   Video stream RPC
tcp://<ROV_HOST>:5556   Management RPC
```

The default ROV host is `192.168.1.4`. Override it for bench or Wi-Fi testing:

```powershell
$env:ROV_HOST="tritonpi.local"
```

## Platform Notes

Windows is the supported competition pilot platform. macOS and Linux can run
many tests and non-video code paths, but camera receive behavior should be
validated on the actual pilot laptop before competition use.

The application can open without a controller or ROV connection, which is useful
for layout development and tests. It is only fully operational when controller,
telemetry, management RPC, and video are all healthy.
