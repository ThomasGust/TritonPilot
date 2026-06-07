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
$env:TRITON_VIDEO_DISPLAY_FPS_MULTI="30"
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
- `render_mode`
- `h264_bitrate`
- `h264_gop`
- `rtp_mtu`
- `latency_ms`
- `port`
- `capture_port`
- `enabled`

For native H.264 cameras, TritonOS applies `h264_bitrate` and `h264_gop` as
V4L2 camera encoder controls when the camera exposes matching controls. The
current full-resolution four-camera pilot profile is 1080p30 H.264 at
`8000000` bps per stream with `latency_ms` set to `5` on the tether. The pilot
view uses `render_mode: "direct3d"` so GStreamer decodes and renders directly
through the Windows Direct3D sink. This avoids copying full-resolution BGR
frames through Python/Qt for live piloting.

Direct3D streams can still save snapshots, single-camera video, and stereo
pairs by using a second mirrored H.264 UDP destination. The normal display
ports remain `5000-5003`; `capture_port` values such as `6000-6003` tell
TritonOS to mirror the same compressed stream for capture-only receivers.
Those receivers start only when capture or stereo tooling needs CPU frames, so
the pilot display path stays smooth.

The older raw-frame widget is still available by omitting `render_mode` or
setting it to anything other than `direct3d`. Use that path for debugging or
experiments that intentionally need every displayed frame inside Python.

Display refresh can be capped separately from the camera stream rate:

```powershell
$env:TRITON_VIDEO_DISPLAY_FPS_SINGLE="30"
$env:TRITON_VIDEO_DISPLAY_FPS_DUAL="30"
$env:TRITON_VIDEO_DISPLAY_FPS_MULTI="30"
```

The multi-camera value applies to three- and four-pane layouts. Lowering it can
make quad view feel smoother on a loaded laptop because the UI stops trying to
scale and repaint every pane at full camera rate.

Per-stream receiver options in `data/streams.json`:

- `render_mode`: set to `direct3d` for low-latency pilot viewing
- `capture_port`: optional mirror UDP port for direct-mode snapshots,
  recording, and stereo capture
- `receiver_h264_decoder`: defaults to `decodebin`; set `avdec_h264` only when
  debugging software decode behavior; direct mode also uses this decoder choice
- `receiver_output_fps`: drops decoded frames before the Python pipe; the stream
  can stay 1080p30 while quad-view display work is capped; this only affects
  the legacy raw-frame widget
- `extra.udp_qos_dscp`: requests a DSCP marking from TritonOS' UDP sender;
  `34` is the current video-priority default
- `extra.sender_leaky_queues`: keeps TritonOS from buffering stale camera frames
  before RTP packetization; the low-latency default is `true`
- `extra.sender_queue_max_buffers`: whole-frame sender queue depth; the
  low-latency default is `1`
- `extra.sender_v4l2_do_timestamp`: timestamps captured frames on the Pi before
  RTP payloading; the low-latency default is `true`
- `extra.v4l2_controls.exposure_dynamic_framerate`: set to `0` to prevent the
  camera from lowering frame rate for exposure, which can look like video lag

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

Back rotating gripper gain:

```powershell
$env:TRITON_BACK_GRIPPER_GAIN_DEFAULT="0.50"
$env:TRITON_BACK_GRIPPER_GAIN_MIN="0.10"
$env:TRITON_BACK_GRIPPER_GAIN_MAX="1.0"
$env:TRITON_BACK_GRIPPER_GAIN_STEP="0.05"
```

The older `TRITON_T200_WRIST_GAIN_*` names are still accepted as fallbacks.

Keyboard arm gain:

```powershell
$env:TRITON_ARM_GAIN_DEFAULT="0.50"
$env:TRITON_ARM_GAIN_MIN="0.10"
$env:TRITON_ARM_GAIN_MAX="1.0"
$env:TRITON_ARM_GAIN_STEP="0.05"
$env:TRITON_ARM_KEYBOARD_RAMP_RATE="0.35"
```

`TRITON_ARM_KEYBOARD_RAMP_RATE` controls how quickly WASD walks the arm target
while a key is held, in normalized command units per second at 100% ARM gain.

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
