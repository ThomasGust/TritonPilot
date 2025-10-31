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
        return self._call("start_stream", **cfg)

    def stop_stream(self, name):
        return self._call("stop_stream", name=name)

    def update_stream(self, name, **updates):
        return self._call("update_stream", name=name, **updates)

    def list_streams(self):
        return self._call("list_streams")

    def list_devices(self):
        return self._call("list_devices")

    def get_device_caps(self, device="/dev/video0"):
        return self._call("get_device_caps", device=device)

rov = ROVStreams()

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
        port=5000,
    )