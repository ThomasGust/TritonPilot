# Media Capture Reset

Media capture is intentionally removed from this branch.

The checked-in baseline is now:

- Direct3D display for every configured video stream
- H.264 decode pinned to `openh264dec`
- no capture mirror ports
- no snapshot/video recording controls
- no MP4 writer or compressed RTP recorder
- no stereo capture page, session writer, or TritonOS capture-ring dependency

This leaves a clean display-first surface for rebuilding media capture without
mixing it into the operator latency path.
