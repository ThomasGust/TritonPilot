# Media Capture Architecture

Media capture is being rebuilt on top of the Direct3D display baseline.

The checked-in baseline is now:

- Direct3D display for every configured video stream
- H.264 decode pinned to `openh264dec`
- still snapshots captured on the ROV through TritonOS video RPC
- Primary/Aux stereo stills use a small async TritonOS snapshot cache
- still stereo pairs captured on the ROV through a paired TritonOS video RPC
- no persistent top-side snapshot RTP mirrors by default
- one JPEG payload returned over RPC per operator snapshot request
- one left/right JPEG pair returned over RPC per stereo capture request
- no configured fixed capture mirror ports
- no MP4 writer or compressed RTP recorder
- no stereo capture page, continuous recorder, or TritonOS capture-ring dependency

The snapshot path avoids screen capture because Direct3D surfaces can read back
as black or include UI overlays. When the operator presses `X`, TritonPilot
asks TritonOS to capture the selected stream locally from the running camera
pipeline's `appsink` snapshot branch. The branch is built into the running
camera pipeline with a `tee`, so the normal RTP display sender does not need to
be stopped, restarted, or read back from the GUI. H.264 streams are decoded and
encoded to high-quality JPEG locally on the ROV; TritonPilot saves those bytes
under the app session folder. If the ROV does not support the RPC yet, Pilot can
fall back to the older top-side mirror tap and save a decoded source-frame PNG.

Stereo still capture asks TritonOS to capture the left and right streams
together. Primary and Aux keep their onboard snapshot branches warm in a small
async cache. At capture time, TritonOS waits for cached frames that arrived
after the request, chooses the closest left/right pair by source timestamp when
available, and rejects pairs outside the configured `max_pair_delta_ms`. The
checked-in forward stereo pair currently uses a `120` ms gate.
TritonPilot writes the returned images under
`stereo_sessions/<session>/left` and `right`, and keeps the
`tritonpilot.stereo_capture_manifest` schema used by older app sessions.
Keyboard `C` toggles between standard snapshot mode and stereo mode. Keyboard
`N` starts a new stereo session inside the active app session folder.

This path preserves the live display stream and avoids blocking at button time,
but the still image still comes from the live H.264 camera stream. Moderate
motion artifacts can remain. Without a shared hardware trigger or exposure
timestamp, TritonOS can minimize process-side delay and record honest deltas,
but it cannot guarantee exact simultaneous exposure.
