# TritonPilot Configuration Guide

Most TritonPilot configuration flows through `config.py`, environment
variables, and `data/streams.json`. Prefer environment variables for field
overrides and code changes for durable defaults.

## ROV Host And Endpoints

Default host:

```text
192.168.1.4
```

Common overrides:

```powershell
$env:ROV_HOST="192.168.1.4"
$env:TRITON_ROV_DEFAULT_HOST="192.168.1.4"
$env:TRITON_ROV_AUTO_DETECT="1"
$env:TRITON_ROV_HOSTS="192.168.1.4,tritonpi.local"
```

Endpoint overrides:

```powershell
$env:ROV_PILOT_EP="tcp://192.168.1.4:6000"
$env:ROV_SENSOR_EP="tcp://192.168.1.4:6001"
$env:ROV_VIDEO_RPC="tcp://192.168.1.4:5555"
$env:ROV_MANAGEMENT_RPC="tcp://192.168.1.4:5556"
```

Use endpoint overrides only when you need nonstandard ports or a tunnel.

## Video Timeouts

Four 1080p streams can take a few seconds to settle:

```powershell
$env:TRITON_VIDEO_STALL_TIMEOUT_S="8.0"
$env:TRITON_VIDEO_FIRST_FRAME_TIMEOUT_S="14.0"
$env:TRITON_VIDEO_DEFAULT_LAYOUT_COUNT="4"
$env:TRITON_VIDEO_STOP_HIDDEN_STREAMS="0"
```

Increase these values only after confirming the network and GStreamer runtime
are healthy.

## Stream Definitions

`data/streams.json` controls the pilot-side camera layout. Each stream can
define:

- `name`
- `device`
- `width`
- `height`
- `fps`
- `rotation_deg`
- `video_format`
- `h264_bitrate`
- `h264_gop`
- `rtp_mtu`
- `latency_ms`
- `port`
- `enabled`

For native H.264 cameras, TritonOS applies `h264_bitrate` and `h264_gop` as
V4L2 camera encoder controls when the camera exposes matching controls. The
current deployment target is `16000000` bps per 1080p30 stream with
`latency_ms` set to `60` for a little more jitter tolerance across four live
views.

Top-level stream layout knobs:

- `default_layout_count`: initial visible camera count for this deployment
- `stop_hidden_streams`: stop ROV-side streams when they are no longer visible;
  set false to keep already-started streams warm for fast camera/layout changes

The current default streams are:

| Camera | UDP port |
| --- | --- |
| Primary Camera | `5000` |
| Arm Camera | `5001` |
| Aux Camera | `5002` |
| Back Gripper Camera | `5003` |

`default_pane_order` controls the initial GUI layout. Keep camera names stable
because reverse-drive camera matching uses names and keywords.

## Controller Selection

```powershell
$env:TRITON_CONTROLLER_INDEX="0"
$env:TRITON_CONTROLLER_DEADZONE="0.15"
$env:TRITON_CONTROLLER_DEBUG="1"
$env:TRITON_CONTROLLER_DUMP_RAW_EVERY="1.0"
```

Axis order is `lx,ly,rx,ry,lt,rt`:

```powershell
$env:TRITON_CONTROLLER_AXIS_MAP="0,1,3,4,2,5"
```

D-pad and special button overrides:

```powershell
$env:TRITON_CONTROLLER_HAT_INDEX="0"
$env:TRITON_CONTROLLER_MENU_BUTTONS="7,8"
$env:TRITON_CONTROLLER_WIN_BUTTONS="10"
```

Run `python .\tools\controller_probe.py` after changing controller settings.

## Pilot Modes And Shortcuts

Depth hold:

```powershell
$env:TRITON_DEPTH_HOLD_TOGGLE="rstick"
$env:TRITON_DEPTH_HOLD_DEFAULT="0"
```

Roll/pitch leveling and yaw hold requests:

```powershell
$env:TRITON_RP_LEVEL_TOGGLE=""
$env:TRITON_RP_LEVEL_DEFAULT="0"
$env:TRITON_YAW_HOLD_TOGGLE="lstick"
$env:TRITON_YAW_HOLD_DEFAULT="0"
```

Lights and arm/disarm backup controls:

```powershell
$env:TRITON_LIGHTS_TOGGLE_SHORTCUT="L"
$env:TRITON_LIGHTS_TOGGLE_BUTTON=""
$env:TRITON_LIGHTS_TOGGLE_EDGE="lights"
$env:TRITON_ARM_DISARM_SHORTCUT="O"
$env:TRITON_ARM_DISARM_EDGE="menu"
```

Reverse drive:

```powershell
$env:TRITON_REVERSE_MODE_DEFAULT="0"
$env:TRITON_REVERSE_TOGGLE="lb"
$env:TRITON_REVERSE_SHORTCUT="R"
$env:TRITON_REVERSE_CAMERA_NAMES="Reverse Camera,Rear Camera,Back Camera"
$env:TRITON_REVERSE_CAMERA_KEYWORDS="reverse,rear,back"
$env:TRITON_FORWARD_CAMERA_KEYWORDS="front,forward"
```

## Gains

Main pilot gain:

```powershell
$env:TRITON_PILOT_MAX_GAIN_DEFAULT="1.0"
$env:TRITON_PILOT_MAX_GAIN_MIN="0.05"
$env:TRITON_PILOT_MAX_GAIN_MAX="1.0"
$env:TRITON_PILOT_MAX_GAIN_STEP="0.05"
```

T200 wrist gain:

```powershell
$env:TRITON_T200_WRIST_GAIN_DEFAULT="0.50"
$env:TRITON_T200_WRIST_GAIN_MIN="0.10"
$env:TRITON_T200_WRIST_GAIN_MAX="1.0"
$env:TRITON_T200_WRIST_GAIN_STEP="0.05"
```

These values are transmitted in `PilotFrame.modes`. TritonOS decides how they
map to actuator output.

## Depth-Hold Display Helpers

These settings only affect topside display. Manual-heave override and release
latching are controlled onboard in TritonOS:

```powershell
$env:TRITON_DEPTH_HOLD_SENSOR_STALE_S="2.0"
```

Change onboard depth-hold behavior in TritonOS, not in TritonPilot.

Yaw-hold display freshness:

```powershell
$env:TRITON_YAW_HOLD_ATTITUDE_STALE_S="1.0"
```

Manual yaw override and release latching are controlled onboard in TritonOS.
TritonPilot displays the ROV-reported runtime yaw target and controller reason.

## Attitude Display Convention

Topside fallback attitude settings:

```powershell
$env:TRITON_ATTITUDE_VEHICLE_ROLL_AXIS="z"
$env:TRITON_ATTITUDE_ROLL_SIGN="1.0"
$env:TRITON_ATTITUDE_PITCH_SIGN="1.0"
```

These keep local replay/display aligned with the current sensor mount. Onboard
attitude telemetry remains authoritative.

## Water Correction

Optional underwater lens correction can be enabled in the GUI. Its defaults are
configured with:

```powershell
$env:TRITON_WATER_ZOOM="1.0"
$env:TRITON_WATER_K1="0.0"
$env:TRITON_WATER_K2="0.0"
$env:TRITON_WATER_K3="0.0"
$env:TRITON_WATER_AIR_HFOV_DEG="138.0"
$env:TRITON_WATER_TARGET_HFOV_DEG="96.0"
```

Use `tools/preview_water_correction.py` to inspect still-image behavior before
using a new correction setting during a run.

## Save Locations

The GUI lets the operator choose a recording directory. If that directory is
unavailable, `recording/save_location.py` falls back to the repository's default
recordings directory.

Use clear session names and preserve original media for TritonAnalysis.
