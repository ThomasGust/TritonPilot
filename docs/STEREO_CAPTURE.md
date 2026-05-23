# Stereo Capture

TritonPilot owns live stereo capture because it already receives decoded ROV
camera streams and records mission data. TritonAnalysis owns calibration,
rectification, disparity, and measurement from the saved capture sessions.

## Camera Reality

The current planned stereo rig uses DeepWater Exploration exploreHD cameras.
Treat them as best-effort stereo cameras:

- They are UVC/V4L2 USB cameras, so TritonOS can stream them through the
  existing GStreamer video service.
- They support 1920x1080 at 30 fps in MJPEG or H.264.
- They use a rolling shutter and do not provide an external trigger path in
  this codebase.

That means software can pair frames closely, but it cannot make the two sensors
expose at exactly the same instant. For accurate calibration and measurement,
keep the calibration board and ROV steady when capturing pairs.

Triton's shipped calibration board is ChArUco with 17 rows by 24 columns, 30 mm
square width, and 22 mm marker width. TritonAnalysis uses those values as its
default stereo calibration settings.

## Configuration

Stereo pairs live in `data/streams.json` under `stereo_pairs`:

```json
{
  "name": "Forward Stereo",
  "left": "Primary Camera",
  "right": "Aux Camera",
  "rig_id": "explorehd_forward_v1",
  "max_pair_delta_ms": 50,
  "apply_stream_rotation": true
}
```

The stream names must match entries in the normal `streams` list. The stereo
configuration does not change the normal pilot video layout.

## Capture Workflow

In TritonPilot, open the Stereo tab. The tab shows the selected stereo pair,
left/right stream settings, live frame age, pair delta, rig metadata, and
capture controls. When the tab is active, the shared video panel is temporarily
set to the left/right pair and the normal pilot layout is restored when leaving
the tab.

The command-line capture helper is also available on the pilot computer after
TritonOS video RPC is reachable:

```powershell
python -m tools.stereo_capture --list-pairs
python -m tools.stereo_capture --pair "Forward Stereo" --count 40 --interval-s 0.5
```

The output is a session folder under the selected recordings root:

```text
stereo_sessions/
  20260522-153012/
    left/
      pair_000001_left.png
    right/
      pair_000001_right.png
    manifest.json
```

`manifest.json` stores the pair configuration, stream definitions, receiver
timestamps, left/right frame sequence numbers, frame delta in milliseconds, and
relative image paths. Move the whole session folder to the analysis computer.

## Quality Checklist

- Use the final rigid mount before any final calibration.
- Calibrate underwater, at the same resolution, codec, focus, and camera
  settings expected in competition.
- Fill the image with the board at many positions, tilts, and distances.
- Capture enough pairs that TritonAnalysis can reject weak detections and still
  keep at least 20 to 40 good observations.
- Keep pair deltas low; for static calibration, 50 ms is acceptable, but lower
  is better.
- Do not change camera rotation, baseline, toe angle, lens cap, or stream
  resolution after calibration.
