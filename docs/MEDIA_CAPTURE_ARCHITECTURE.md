# Media Capture Architecture

TritonPilot should treat display, media recording, and calibration capture as
separate planes. They have different latency, quality, and timing goals, so one
pipeline should not be forced to satisfy all three.

## Current State

The pilot display path uses `render_mode: "direct3d"` with GStreamer rendering
directly through the Windows Direct3D sink. Keep this path optimized for low
operator latency:

- low receiver latency
- leaky queues
- hardware decode when available
- no Python copy of every full-resolution frame

Capture now splits standard media and stereo calibration. Standard H.264
recording uses the Direct3D receiver's compressed RTP tee by default, snapshots
use a low-rate decoded tap from the active Direct3D receiver when available, and
stereo capture prefers a TritonOS-side capture-ring RPC before falling back to
topside receive/decode timestamps.

## Target Split

### 1. Pilot Display Plane

Purpose: drive the vehicle.

Owner: `gui/direct_gst_video_widget.py` and `video/rov_streams.py`.

Contract:

- start quickly and recover quickly
- prefer newest frame over complete frame history
- tolerate frame drops
- do not block on recording or disk I/O

The display plane can expose optional frame tees for snapshots or diagnostics,
but it should not become the authoritative stereo capture clock.

### 2. Mission Recording Plane

Purpose: save what the operator saw for review and analysis.

Owner: `recording/compressed_stream_recorder.py` for H.264 RTP streams, with
`recording/video_recorder.py` as a decoded-frame fallback.

Contract:

- record compressed H.264 transport when possible
- remux/finalize to MP4 after stop
- avoid per-frame PNG or BGR encode work in the UI thread
- keep partial files hidden until finalization succeeds

For `render_mode: "direct3d"` H.264 streams, compressed recording is default-on
unless `direct_compressed_recording`, `compressed_recording`, or
`receiver_compressed_recording` is set false. Decoded-frame recording remains
the fallback for non-H.264 streams, explicit opt-out, and compressed recorder
startup failure.

### Standard Snapshot Plane

Purpose: save ordinary still images from the active stream.

Owner: `gui/direct_gst_video_widget.py`.

Contract:

- prefer the Direct3D receiver snapshot pipe
- throttle snapshot frames with `direct_snapshot_frame_pipe_fps` or
  `TRITON_DIRECT_SNAPSHOT_FPS` (default 2 fps)
- keep frames at full camera resolution, independent of square display crop
- fall back to ROV `capture_frame` when available, then the legacy capture
  receiver
- use Direct3D screen capture only when `TRITON_DIRECT_SCREEN_SNAPSHOT` is
  explicitly enabled for diagnostics

### 3. Stereo Calibration Capture Plane

Purpose: save left/right image pairs with a defensible timing contract.

Owner: `stereo/capture.py` in TritonPilot plus the TritonOS video RPC service.

Implemented v1 contract:

- request a pair by left/right stream names with `capture_stereo_pair`
- read from optional per-stream TritonOS capture rings
- return base64 PNG payloads plus per-frame ROV monotonic and wall timestamps
- return pair delta, camera paths, stream settings, and any dropped/retried
  frame counts
- reject pairs outside the configured sync threshold
- save the returned pair into the normal TritonPilot `stereo_sessions` manifest
- mark manifest frame records with `timestamp_source` as either
  `rov_capture_ring` or `topside_receiver`

This still cannot make rolling-shutter USB cameras truly hardware-synchronized.
It does remove network jitter, independent Windows jitterbuffers, and separate
decode timing from the stereo sync decision.

## Configuration Knobs

Direct3D receiver options live on each TritonPilot stream entry:

- `direct_compressed_recording`: set false to force decoded recording fallback
- `direct_snapshot_frame_pipe`: set false to disable the direct snapshot tap
- `direct_snapshot_frame_pipe_fps`: low-rate still tap FPS, default 2

TritonOS sender options live under each stream `extra` object:

- `capture_ring`: enable the PNG appsink ring for ROV-side still capture
- `capture_ring_fps`: default 5 for stereo capture streams
- `capture_ring_history_size`: default 30 frames

The default `data/streams.json` enables the TritonOS capture ring on the
configured forward stereo pair streams only: `Primary Camera` and `Aux Camera`.
