import json
import socket
import numpy as np

from video.gst_receiver import ReceiverProcess, RxConfig
from video.rov_streams import ROVStreams  # your class above


class RemoteCv2Camera:
    """
    cv2-ish wrapper for: Pi camera -> RTP -> Windows GStreamer -> numpy frame
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
        port: int = 5000,
        codec: str = "jpeg",     # must match video_format or what you send
        latency_ms: int = 60,
        channel_order: str = "BGR",
        windows_host: str | None = None,
    ):
        self.rov = rov
        self.name = name
        self.device = device
        self.width = width
        self.height = height
        self.fps = fps
        self.video_format = video_format
        self.port = port
        self.codec = codec
        self.latency_ms = latency_ms
        self.channel_order = channel_order

        # detect our own IP on Windows if not provided
        if windows_host is None:
            # cheap local-ip trick
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(("8.8.8.8", 80))
                windows_host = s.getsockname()[0]
            finally:
                s.close()
        self.windows_host = windows_host

        # 1) tell Pi to start sending
        self.rov.start_stream(
            name=self.name,
            device=self.device,
            width=self.width,
            height=self.height,
            fps=self.fps,
            video_format=self.video_format,
            host=self.windows_host,
            port=self.port,
        )

        # 2) start local receiver in RAW mode so we can get numpy
        rx_cfg = RxConfig(
            name=self.name,
            codec="jpeg" if self.video_format == "mjpeg" else "h264",
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
        """
        cv2.VideoCapture-like: returns (ok, frame)
        """
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

        self.pi_endpoint = cfg.get("pi_endpoint", "tcp://192.168.1.2:5555")
        self.windows_host = cfg.get("windows_host")  # may be None -> auto-detect
        self.rov = ROVStreams(endpoint=self.pi_endpoint)
        self.stream_defs = {s["name"]: s for s in cfg.get("streams", [])}
        self._opened: dict[str, RemoteCv2Camera] = {}

    def list_available(self):
        return list(self.stream_defs.keys())

    def open(self, name: str) -> RemoteCv2Camera:
        if name in self._opened:
            return self._opened[name]

        s = self.stream_defs[name]
        cam = RemoteCv2Camera(
            rov=self.rov,
            name=s["name"],
            device=s["device"],
            width=s["width"],
            height=s["height"],
            fps=s["fps"],
            video_format=s.get("video_format", "mjpeg"),
            port=s.get("port", 5000),
            windows_host=self.windows_host,
        )
        self._opened[name] = cam
        return cam

    def close(self, name: str):
        cam = self._opened.pop(name, None)
        if cam:
            cam.release()