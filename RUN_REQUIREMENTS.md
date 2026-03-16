# TritonPilot Run Requirements

This file describes what another user needs in order to run the TritonPilot topside application successfully.

## What this repo runs

The main topside app is started with:

```powershell
python .\main_topside.py
```

It launches a PyQt6 GUI that:

- reads an Xbox-style controller with `pygame`
- publishes pilot control frames to the ROV over ZeroMQ
- subscribes to sensor telemetry from the ROV over ZeroMQ
- starts/stops ROV video streams through a ZeroMQ RPC service
- receives video locally through GStreamer UDP pipelines
- uses OpenCV and NumPy for frame handling, recording, water correction, and crab detection

## Required software

### 1. Python

- Python 3.10 or newer
- `pip`
- A virtual environment is strongly recommended

This code uses modern Python type syntax such as `str | None` and `list[int]`, so older Python versions will not work.

### 2. Python packages

Install these packages into your virtual environment:

```powershell
pip install PyQt6 pyzmq pygame numpy opencv-python
```

Optional but recommended for validation:

```powershell
pip install pytest
```

### 3. GStreamer

The video receiver path depends on the external `gst-launch-1.0` executable. On Windows, install GStreamer 1.0 and make sure one of these is true:

- `gst-launch-1.0.exe` is on `PATH`
- or `GST_LAUNCH` is set to the full path to `gst-launch-1.0.exe`

The current receiver code is Windows-oriented and looks for GStreamer in common Windows install paths if it is not already on `PATH`.

The install must include the plugins needed for:

- RTP UDP input
- JPEG depayload/decode
- H.264 depayload/parse/decode
- `videoconvert`
- `fdsink`

In practice, a normal "Complete" GStreamer install is the safest option.

## Required hardware and external services

### 1. ROV-side services

For full operation, the ROV side must already be running and reachable from the topside machine. This app expects:

- pilot control endpoint at `tcp://<ROV_HOST>:6000`
- sensor telemetry endpoint at `tcp://<ROV_HOST>:6001`
- video RPC endpoint at `tcp://<ROV_HOST>:5555`

By default, `ROV_HOST` is `192.168.1.4`.

### 2. Video stream definitions

`data/streams.json` must describe the streams that exist on the ROV side. The current repo expects four enabled streams on UDP ports:

- `5000`
- `5001`
- `5002`
- `5003`

If the onboard camera layout, device paths, codecs, or ports change, `data/streams.json` must be updated to match.

### 3. Network access

The topside machine must be able to:

- open outbound TCP connections to the ROV on ports `5555`, `6000`, and `6001`
- receive inbound UDP video traffic on the ports defined in `data/streams.json`

Optional network diagnostics in the GUI also probe UDP port `7700` on the ROV.

### 4. Controller

For actual piloting, a supported game controller must be connected to the topside computer. The code is built around an Xbox-style mapping and uses `pygame`/SDL for input.

The GUI can still start without a controller, but pilot control publishing will report the controller as unavailable.

## Environment variables

These are the important runtime overrides supported by the current code.

### Connection

- `ROV_HOST`
- `ROV_PILOT_EP`
- `ROV_SENSOR_EP`
- `ROV_VIDEO_RPC`

### Controller selection and tuning

- `TRITON_CONTROLLER_DEADZONE`
- `TRITON_CONTROLLER_INDEX`
- `TRITON_CONTROLLER_DEBUG`
- `TRITON_CONTROLLER_DUMP_RAW_EVERY`
- `TRITON_CONTROLLER_AXIS_MAP`
- `TRITON_CONTROLLER_HAT_INDEX`
- `TRITON_CONTROLLER_MENU_BUTTONS`
- `TRITON_CONTROLLER_WIN_BUTTONS`

### Depth-hold and pilot gain defaults

- `TRITON_DEPTH_HOLD_TOGGLE`
- `TRITON_DEPTH_HOLD_DEFAULT`
- `TRITON_PILOT_MAX_GAIN_DEFAULT`
- `TRITON_PILOT_MAX_GAIN_MIN`
- `TRITON_PILOT_MAX_GAIN_MAX`
- `TRITON_PILOT_MAX_GAIN_STEP`
- `TRITON_DEPTH_HOLD_WALK_DEADBAND`
- `TRITON_DEPTH_HOLD_WALK_RATE_MPS`
- `TRITON_DEPTH_HOLD_SENSOR_STALE_S`

### Water correction

- `TRITON_WATER_ZOOM`
- `TRITON_WATER_K1`
- `TRITON_WATER_K2`
- `TRITON_WATER_K3`
- `TRITON_WATER_AIR_HFOV_DEG`
- `TRITON_WATER_TARGET_HFOV_DEG`

### Optional network diagnostics

- `TRITON_NETDIAG_PORT`

## Recommended setup steps

From the repo root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install PyQt6 pyzmq pygame numpy opencv-python pytest
```

If needed, point the app at the correct ROV host:

```powershell
$env:ROV_HOST="192.168.1.4"
```

If needed, point the app at a specific GStreamer executable:

```powershell
$env:GST_LAUNCH="C:\Program Files\GStreamer\1.0\msvc_x86_64\bin\gst-launch-1.0.exe"
```

Then launch:

```powershell
python .\main_topside.py
```

## What "properly running" means

A fully working setup should provide all of the following:

- the PyQt6 window opens
- the controller is detected
- telemetry updates arrive from the ROV
- enabled video tabs connect and show live frames
- recordings can be written into the repo's `recordings` directory

If the GUI opens but video is missing, controller input is unavailable, or telemetry never arrives, the app is only partially operational.

## Notes for maintainers

- There is currently no checked-in `requirements.txt` or `pyproject.toml`, so the Python dependency list above was inferred from the current imports in the repo.
- The topside video receiver implementation is Windows-specific because it shells out to `gst-launch-1.0.exe` and includes Windows process handling.
