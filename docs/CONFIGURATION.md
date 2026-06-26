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
- `enabled`

For native H.264 cameras, TritonOS applies `h264_bitrate` and `h264_gop` as
V4L2 camera encoder controls when the camera exposes matching controls. The
current checked-in four-camera pilot profile is Direct3D display at 1080p30
H.264, `8000000` bps per stream, and `latency_ms` set to `5` on the tether.
The profile pins H.264 receive decode to `openh264dec` to avoid Windows
hardware decoder selection through `decodebin`.

Display refresh can be capped separately from the camera stream rate:

```powershell
$env:TRITON_VIDEO_DISPLAY_FPS_SINGLE="30"
$env:TRITON_VIDEO_DISPLAY_FPS_DUAL="30"
$env:TRITON_VIDEO_DISPLAY_FPS_MULTI="30"
```

The multi-camera value applies to three- and four-pane layouts. Lowering it can
make quad view feel smoother on a loaded laptop because the UI stops trying to
scale and repaint every pane at full camera rate.

Still photos are captured on the ROV, not from the Direct3D viewport. TritonOS
keeps a low-rate local JPEG snapshot branch on each running camera pipeline.
When the operator presses `X`, TritonPilot calls the video RPC
`capture_snapshot` command for the selected stream and writes the returned JPEG
bytes into the active app session folder. This avoids black Direct3D readbacks,
UI-overlay captures, and extra always-on top-side RTP receivers. The old
top-side snapshot mirror path remains as a fallback for older TritonOS builds,
but the checked-in profile does not prewarm it. There are still no configured
fixed capture ports or MP4 writers in this profile.

Per-stream receiver options in `data/streams.json`:

- `render_mode`: set to `direct3d` for the Direct3D sink path
- `receiver_h264_decoder`: defaults to `openh264dec` because current DXVA/D3D
  hardware decoder selection can produce green/corrupt frames on the pilot
  laptop; set `decodebin` only when intentionally testing automatic decode
- `receiver_output_fps`: drops decoded frames before the Python pipe; the stream
  can stay 1080p30 while quad-view display work is capped
- `extra.udp_qos_dscp`: requests a DSCP marking from TritonOS' UDP sender;
  `34` is the current video-priority default
- `extra.sender_leaky_queues`: true for this low-latency Direct3D baseline
- `extra.sender_queue_max_buffers`: whole-frame sender queue depth; the
  low-latency queue depth is `1`
- `extra.sender_v4l2_do_timestamp`: timestamps captured frames on the Pi before
  RTP payloading; the stable default is `true`
- `extra.rov_snapshot_fps`: onboard JPEG snapshot branch rate; Primary and Aux
  use `12` in the checked-in profile so ROV-side stereo still captures keep a
  warm frame cache without the 30 FPS decode/JPEG branch overloading the ROV
- `extra.rov_snapshot_jpeg_quality`: onboard JPEG encoder quality for ROV-side
  snapshots. Primary and Aux use `98` so the still-save path adds minimal extra
  compression after the camera stream has already been decoded.
- `extra.rov_snapshot_cache_enabled`: keeps TritonOS pulling the onboard
  snapshot branch into a small async frame cache. Primary and Aux enable this so
  stereo capture can choose an already-arrived left/right pair instead of
  blocking two fresh pulls at button time.
- `extra.rov_snapshot_cache_frames`: max cached still frames per stream; Primary
  and Aux use `24`.
- `extra.v4l2_controls.exposure_dynamic_framerate`: set to `0` to prevent the
  camera from lowering frame rate for exposure, which can look like video lag

Top-level stream layout knobs:

- `default_layout_count`: initial visible camera count for this deployment
- `stop_hidden_streams`: stop ROV-side streams when they are no longer visible;
  set false to keep already-started streams warm for fast camera/layout changes
- `snapshot_prewarm_count`: number of default streams to keep ready through the
  legacy top-side RTP mirror snapshot path; current field value is `0` because
  onboard TritonOS snapshots are preferred
- `snapshot_persistent`: optional per-stream override for the legacy mirror
  fallback path
- `receiver_snapshot_output_fps`: optional legacy mirror raw receiver output
  rate; omitted in the default profile because no mirror receivers are normally
  prewarmed
- `stereo_pairs`: configured left/right still-capture pairs. The checked-in
  pair is `Forward Stereo`, with `Primary Camera` as left and `Aux Camera` as
  right. TritonPilot uses this for keyboard stereo capture mode and preserves
  the historical `stereo_sessions/<session>/manifest.json` schema. The default
  `max_pair_delta_ms` is `120`, which is a practical software-sync gate for the
  warmed 12 FPS onboard still cache. The actual pair delta is still written to
  each stereo manifest frame.

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

Arm gain and controller aiming:

```powershell
$env:TRITON_ARM_GAIN_DEFAULT="0.50"
$env:TRITON_ARM_GAIN_MIN="0.10"
$env:TRITON_ARM_GAIN_MAX="1.0"
$env:TRITON_ARM_GAIN_STEP="0.05"
$env:TRITON_ARM_RATE="2.5"
$env:TRITON_ARM_STICK_PITCH_INVERT="-1.0"
$env:TRITON_ARM_STICK_WRIST_INVERT="1.0"
$env:TRITON_ARM_PARK_SHORTCUT="A"
$env:TRITON_ARM_PARK_PITCH="-1.0"
$env:TRITON_ARM_PARK_WRIST="1.0"
$env:TRITON_ARM_PARK_RATE="0.80"
```

`TRITON_ARM_RATE` controls how quickly the modifier-held right stick walks the arm
target, in normalized command units per second at 100% ARM gain.
`TRITON_ARM_STICK_PITCH_INVERT` and `TRITON_ARM_STICK_WRIST_INVERT` affect only
the modifier-held controller stick path; TritonOS absolute pitch geometry is
unchanged. Keyboard `A` commands the Pilot-side park target. `TRITON_ARM_PARK_*`
sets the startup fallback for that shortcut; Vehicle Setup refreshes it from the
ROV's `GRIPPER_ARM_*` / `GRIPPER_DISARM_*` config when connected.
`TRITON_ARM_PARK_RATE` controls how slowly that explicit park command walks the
Pilot-side arm target. Keyboard `W`/`S`/`D` are not bound to manipulator motion.
Geometry knobs such as servo range, pitch span, and pitch neutral are onboard
TritonOS settings; the Vehicle Setup page can stream and save those values while
tuning. The same page also streams and saves normalized pitch and wrist-roll
limits (`GRIPPER_PITCH_MIN/MAX`, `GRIPPER_YAW_MIN/MAX`) so the ROV clamps arm
position commands before differential mixing.

The live gain values are transmitted in `PilotFrame.modes`. TritonOS decides how
they map to actuator output.

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
unavailable, `recording/save_location.py` falls back to the app default:

```text
source checkout: <repo>\recordings
packaged app:    %USERPROFILE%\Documents\TritonPilot\Recordings
```

Set `TRITON_RECORDINGS_DIR` to override the default without using the GUI.
Set `TRITON_STREAMS_FILE` to point a source run or packaged app at a different
camera stream configuration file.

Use clear session names and preserve original media for TritonAnalysis.

## UI Responsiveness Diagnostics

If the Qt shell feels sticky during a bench run, enable the event-loop lag
probe:

```powershell
$env:TRITON_UI_LAG_PROBE="1"
$env:TRITON_UI_LAG_WARN_MS="120"
```

The probe logs when the UI thread falls behind. Pair it with
`TRITON_CAPTURE_TRACE=1` when you want those lag samples in the capture trace.

## Startup Window Mode

TritonPilot opens maximized by default. Press `F11` or use
`View > Full Screen` to enter true full-screen mode; press `F11` or `Esc` to
return to maximized mode.

For development or bench runs:

```powershell
$env:TRITON_START_MAXIMIZED="0"
```

You can also pass `--windowed` when launching from a terminal.
Pass `--fullscreen` or set `TRITON_START_FULLSCREEN=1` only when you want true
borderless full-screen at startup.
