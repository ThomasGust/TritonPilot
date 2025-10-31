import json
import zmq

class ROVStreams:
    def __init__(self, endpoint="tcp://192.168.1.2:5555"):
        self.ctx = zmq.Context.instance()
        self.sock = self.ctx.socket(zmq.REQ)
        self.sock.connect(endpoint)

    def _call(self, cmd, **args):
        self.sock.send_json({"cmd": cmd, "args": args})
        reply = self.sock.recv_json()
        if not reply.get("ok"):
            raise RuntimeError(f"ROV error: {reply.get('error')}")
        return reply.get("data")

    def ping(self):
        return self._call("ping")

    def start_stream(self, **cfg):
        name = cfg.get("name")
        try:
            return self._call("start_stream", **cfg)
        except RuntimeError as e:
            # fall back to "restart" behavior if the Pi hasn't been updated yet
            if "already exists" in str(e).lower():
                # try stop + start
                if name:
                    self.stop_stream(name)
                return self._call("start_stream", **cfg)
            raise

    def stop_stream(self, name):
        return self._call("stop_stream", name=name)

    def update_stream(self, name, **updates):
        return self._call("update_stream", name=name, **updates)

    def list_streams(self):
        return self._call("list_streams")

    def ensure_stream(self, **cfg):
        return self._call("ensure_stream", **cfg)

    def list_devices(self):
        return self._call("list_devices")

    def get_device_caps(self, device="/dev/video0"):
        return self._call("get_device_caps", device=device)

import re

# labels we know are *not* user cameras
_NON_CAMERA_LABEL_SNIPPETS = [
    "bcm2835-codec",
    "bcm2835-isp",
    "rpi-hevc",
    "image_fx",
    "isp-output",
    "isp-capture",
    "codec-encode",
    "codec-decode",
]

def is_probably_camera(dev: dict) -> bool:
    """
    Heuristic: true cameras usually
      - exist
      - have a friendly label (Logitech, C920, C922, USB Camera, etc.)
      - expose at least one mode with a sane resolution (>= 320x240)
      - are *not* bcm/ISP helper devices
    """
    if not dev.get("exists", False):
        return False

    label = (dev.get("label") or "").lower()

    # 1) blacklist labels
    for bad in _NON_CAMERA_LABEL_SNIPPETS:
        if bad in label:
            return False

    # 2) many helper nodes live at /dev/video1x or /dev/video2x
    #    we can be stricter here: if it's >= /dev/video10 and
    #    has no MJPG *and* no H.264, it's very likely not a camera
    path = dev.get("device", "")
    m = re.match(r"^/dev/video(\d+)$", path)
    if m:
        idx = int(m.group(1))
        caps = dev.get("caps_flags", {})
        if idx >= 10 and not (
            caps.get("supports_mjpeg")
            or caps.get("supports_h264")
        ):
            return False

    # 3) must have at least one usable mode
    modes = dev.get("modes") or dev.get("formats") or []
    if not modes:
        return False

    # check that at least one size is "camera-y"
    has_sane_size = False
    for fmt in modes:
        for sz in fmt.get("sizes", []):
            if sz.get("width", 0) >= 320 and sz.get("height", 0) >= 240:
                has_sane_size = True
                break
        if has_sane_size:
            break

    if not has_sane_size:
        return False

    return True


def list_real_cameras(rov: "ROVStreams") -> list[dict]:
    devices = rov.list_devices()
    return [d for d in devices if is_probably_camera(d)]

rov = ROVStreams()
real_devices = list_real_cameras(rov)
print(real_devices)
"""
# populate GUI
for dev in rov.list_devices():
    print(dev["device"], dev.get("label", ""))
    print("  caps:", dev.get("caps_flags", {}))

    # 👇 NEW: detailed modes
    for fmt in dev.get("modes", []):
        fmt_name = fmt.get("format", "?")
        desc = fmt.get("description") or ""
        print(f"  format: {fmt_name} {f'({desc})' if desc else ''}")
        for sz in fmt.get("sizes", []):
            w = sz.get("width")
            h = sz.get("height")
            fps_list = sz.get("fps", [])
            # turn [30.0, 15.0] into "30, 15"
            fps_str = ", ".join(str(int(f)) if f.is_integer() else str(f) for f in fps_list)
            print(f"    {w}x{h} @ {fps_str} fps")

caps = rov.get_device_caps("/dev/video0")
if caps["caps_flags"]["supports_h264"]:
    # tell Pi to start H.264 stream from that camera
    rov.start_stream(
        name="cam1",
        device="/dev/video0",
        video_format="h264",   # camera already outputs h264
        host="192.168.1.1",
        port=5002,
    )
elif caps["caps_flags"]["supports_mjpeg"]:
    # fallback
    rov.start_stream(
        name="cam1",
        device="/dev/video0",
        video_format="mjpeg",
        transport="udp",
        host="192.168.1.1",
        port=5002,
    )
"""