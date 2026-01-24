import json
import socket
import numpy as np

from config import VIDEO_RPC_ENDPOINT

from video.gst_receiver import ReceiverProcess, RxConfig
from video.rov_streams import ROVStreams


def _infer_wire_codec(video_format: str, encode: str | None) -> str:
    """Determine what arrives on the wire (RTP payload), for receiver selection."""
    vf = (video_format or "").lower()
    enc = (encode or "").lower() if encode else None

    if vf == "h264" or enc == "h264":
        return "h264"
    return "jpeg"


class RemoteCv2Camera:
    """cv2-ish wrapper:
        ROV-side camera -> RTP over UDP -> topside GStreamer -> numpy frame

    Notes:
      - video_format describes the camera's output format on the ROV (mjpeg/raw/h264)
      - encode describes what the ROV should send on the wire (None/h264/mjpeg)

    The ROV video service may auto-adjust width/height/fps and/or switch camera
    input format (e.g. raw -> mjpeg) to avoid negotiation failures. When it does,
    we adopt the effective settings so frame reshaping stays correct.
    """

    def __init__(
        self,
        rov: ROVStreams,
        name: str,
        device: str,
        width: int,
        height: int,
        fps: int,
        video_format: str = "mjpeg",
        encode: str | None = None,
        h264_bitrate: int = 2_000_000,
        h264_gop: int = 30,
        rtp_mtu: int = 1200,
        transport: str = "udp",
        port: int = 5000,
        latency_ms: int = 60,
        channel_order: str = "BGR",
        windows_host: str | None = None,
        # Optional overrides
        rtp_pt_h264: int = 96,
        rtp_pt_jpeg: int = 26,
        udpsink_buffer_size: int = 0,
        leaky_queue: bool = True,
        max_queue_buffers: int = 1,
        sync: bool = False,
    ):
        self.rov = rov
        self.name = name
        self.device = device
        self.width = int(width)
        self.height = int(height)
        self.fps = int(fps)
        self.video_format = video_format
        self.encode = encode
        self.port = int(port)
        self.latency_ms = int(latency_ms)
        self.channel_order = channel_order

        # Detect our own IP on topside if not provided
        if windows_host is None:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(("8.8.8.8", 80))
                windows_host = s.getsockname()[0]
            finally:
                s.close()
        self.windows_host = windows_host

        # 1) Tell ROV to start sending. Pass through H.264 knobs (ROV ignores if not applicable).
        reply = self.rov.start_stream(
            name=self.name,
            device=self.device,
            width=self.width,
            height=self.height,
            fps=self.fps,
            video_format=self.video_format,
            encode=self.encode,
            h264_bitrate=h264_bitrate,
            h264_gop=h264_gop,
            rtp_mtu=rtp_mtu,
            transport=transport,
            host=self.windows_host,
            port=self.port,
            rtp_pt_h264=rtp_pt_h264,
            rtp_pt_jpeg=rtp_pt_jpeg,
            udpsink_buffer_size=udpsink_buffer_size,
            leaky_queue=leaky_queue,
            max_queue_buffers=max_queue_buffers,
            sync=sync,
        )

        # 1b) If the ROV adjusted to a supported mode, adopt it (critical for correct reshaping).
        if isinstance(reply, dict):
            eff = reply.get("effective")
            if isinstance(eff, dict):
                self.video_format = eff.get("video_format", self.video_format)
                self.encode = eff.get("encode", self.encode)
                try:
                    self.width = int(eff.get("width", self.width))
                    self.height = int(eff.get("height", self.height))
                    self.fps = int(eff.get("fps", self.fps))
                except Exception:
                    pass

        wire_codec = _infer_wire_codec(self.video_format, self.encode)

        # 2) Start local receiver in RAW mode so we can get numpy frames
        rx_cfg = RxConfig(
            name=self.name,
            codec=wire_codec,
            port=self.port,
            latency_ms=self.latency_ms,
            mode="raw",
            width=self.width,
            height=self.height,
            channel_order=self.channel_order,
        )
        self.rx = ReceiverProcess(rx_cfg)
        self.rx.start()

    def read(self):
        """cv2.VideoCapture-like: returns (ok, frame)."""
        fr = self.rx.read_frame()
        if fr is None:
            return False, None
        img = np.frombuffer(fr, dtype=np.uint8).reshape((self.height, self.width, 3))
        return True, img

    def release(self):
        try:
            self.rx.stop()
        except Exception:
            pass
        try:
            self.rov.stop_stream(self.name)
        except Exception:
            pass


class RemoteCameraManager:
    def __init__(self, config_path: str):
        with open(config_path, "r") as f:
            cfg = json.load(f)

        self.rov = ROVStreams(endpoint=VIDEO_RPC_ENDPOINT)
        self.windows_host = cfg.get("windows_host")

        self.stream_defs = {s["name"]: s for s in cfg.get("streams", [])}
        self._opened: dict[str, RemoteCv2Camera] = {}

    def list_available(self):
        return [name for name, s in self.stream_defs.items() if s.get("enabled", True)]

    def open(self, name: str) -> RemoteCv2Camera:
        if name in self._opened:
            return self._opened[name]

        if name not in self.stream_defs:
            raise KeyError(f"Unknown stream '{name}'")
        s = self.stream_defs[name]
        if not s.get("enabled", True):
            raise ValueError(f"Stream '{name}' is disabled in config")

        cam = RemoteCv2Camera(
            rov=self.rov,
            name=s["name"],
            device=s["device"],
            width=s["width"],
            height=s["height"],
            fps=s["fps"],
            video_format=s.get("video_format", "mjpeg"),
            encode=s.get("encode"),
            h264_bitrate=s.get("h264_bitrate", 2_000_000),
            h264_gop=s.get("h264_gop", 30),
            rtp_mtu=s.get("rtp_mtu", 1200),
            transport=s.get("transport", "udp"),
            port=s.get("port", 5000),
            latency_ms=s.get("latency_ms", 60),
            channel_order=s.get("channel_order", "BGR"),
            windows_host=self.windows_host,
            rtp_pt_h264=s.get("rtp_pt_h264", 96),
            rtp_pt_jpeg=s.get("rtp_pt_jpeg", 26),
            udpsink_buffer_size=s.get("udpsink_buffer_size", 0),
            leaky_queue=s.get("leaky_queue", True),
            max_queue_buffers=s.get("max_queue_buffers", 1),
            sync=s.get("sync", False),
        )
        self._opened[name] = cam
        return cam

    def close(self, name: str):
        cam = self._opened.pop(name, None)
        if cam:
            cam.release()
