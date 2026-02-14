import json
import socket
import logging
import numpy as np
from config import VIDEO_RPC_ENDPOINT

from network.net_select import parse_zmq_endpoint, choose_video_receive_ip

from video.gst_receiver import ReceiverProcess, RxConfig
from video.rov_streams import ROVStreams  # your class above

logger = logging.getLogger(__name__)

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
        stream_opts: dict | None = None,
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

        # Populated if the ROV had to perform recovery actions (e.g., USB rebind)
        self.start_messages: list[str] = []

        # Detect the best local IP to receive video if not provided.
        # IMPORTANT: the previous approach used 8.8.8.8 which tends to pick Wi‑Fi.
        # Here we select the local IP that can reach the ROV video RPC host,
        # preferring wired/tether when possible.
        if windows_host is None:
            try:
                rov_host, rov_port = parse_zmq_endpoint(VIDEO_RPC_ENDPOINT)
                prefer_wired = bool(stream_opts.get("tether_prefer_wired", True)) if stream_opts else True
                windows_host = choose_video_receive_ip(
                    remote_host=rov_host,
                    remote_port=int(rov_port),
                    prefer_wired=prefer_wired,
                    require_private=True,
                )
            except Exception:
                # fallback to the OS-chosen route (still better than 8.8.8.8)
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                try:
                    rov_host, _rov_port = parse_zmq_endpoint(VIDEO_RPC_ENDPOINT)
                    s.connect((rov_host, 9))
                    windows_host = s.getsockname()[0]
                finally:
                    s.close()
        self.windows_host = windows_host

        stream_opts = stream_opts or {}
        # Allow config to override receiver-side jitter buffer setting
        self.latency_ms = int(stream_opts.get('latency_ms', self.latency_ms))


        # 1) tell Pi to start sending


        start_kwargs = dict(


            name=self.name,


            device=self.device,


            width=self.width,


            height=self.height,


            fps=self.fps,


            video_format=self.video_format,


            host=self.windows_host,


            port=self.port,


        )



        # Pass-through optional transcoding / transport knobs from config


        for k in (


            "encode",


            "h264_bitrate",


            "h264_gop",


            "transport",


            "rtp_pt_jpeg",


            "rtp_pt_h264",


            "latency_ms",


            "sync",


            "extra",


        ):


            if k in stream_opts and stream_opts[k] is not None:


                start_kwargs[k] = stream_opts[k]



        resp = self.rov.start_stream(**start_kwargs)
        if isinstance(resp, dict) and resp.get("messages"):
            try:
                self.start_messages = [str(m) for m in (resp.get("messages") or [])]
                for m in self.start_messages:
                    logger.warning("ROV video start notice (%s): %s", self.name, m)
            except Exception:
                self.start_messages = []



        tx_is_h264 = (start_kwargs.get("video_format") == "h264") or (str(start_kwargs.get("encode", "")).lower() == "h264")

        # 2) start local receiver in RAW mode so we can get numpy
        # Bind receiver to the chosen host so we *only* accept video arriving on that interface.
        bind_rx = True
        if stream_opts and ("bind_receiver_to_host" in stream_opts):
            bind_rx = bool(stream_opts.get("bind_receiver_to_host"))

        rx_cfg = RxConfig(
            name=self.name,
            codec="h264" if tx_is_h264 else "jpeg",
            port=self.port,
            bind_address=self.windows_host if bind_rx else "0.0.0.0",
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
        # Stop local receiver first (tends to unblock quickly even if ROV is slow).
        try:
            self.rx.stop()
        except Exception as e:
            logger.warning("Failed to stop local receiver for '%s': %s", self.name, e)

        # IMPORTANT: stop the ROV-side stream. The RPC expects a keyword arg "name".
        # A previous positional call here would throw a TypeError and get swallowed,
        # leaving streams running on the ROV and slowly overloading CPU/bandwidth.
        try:
            self.rov.stop_stream(name=self.name)
        except Exception as e:
            logger.warning("Failed to stop ROV stream '%s': %s", self.name, e)

class RemoteCameraManager:
    def __init__(self, config_path: str):
        with open(config_path, "r") as f:
            cfg = json.load(f)

        # ROV RPC endpoint comes from config (single source of truth).
        self.rov = ROVStreams(endpoint=VIDEO_RPC_ENDPOINT)

        # Optional override for the topside host IP to receive UDP video.
        # If None, RemoteCv2Camera auto-detects the local IP.
        self.windows_host = cfg.get("windows_host")

        # Optional defaults that can be set at the top-level of streams.json.
        # Per-stream values can override these.
        self._defaults = {
            "tether_prefer_wired": bool(cfg.get("tether_prefer_wired", True)),
            "bind_receiver_to_host": bool(cfg.get("bind_receiver_to_host", True)),
        }

        self.stream_defs = {s["name"]: s for s in cfg.get("streams", [])}
        # If a stream def omits "enabled", assume True.
        self._opened: dict[str, RemoteCv2Camera] = {}

    def list_available(self):
        names = []
        for name, s in self.stream_defs.items():
            if s.get('enabled', True):
                names.append(name)
        return names

    def open(self, name: str) -> RemoteCv2Camera:
        if name in self._opened:
            return self._opened[name]

        if name not in self.stream_defs:
            raise KeyError(f"Unknown stream '{name}'")
        s = self.stream_defs[name]
        if not s.get('enabled', True):
            raise ValueError(f"Stream '{name}' is disabled in config")

        # Merge stream options with top-level defaults
        stream_opts = dict(self._defaults)
        stream_opts.update(s)

        cam = RemoteCv2Camera(
            rov=self.rov,
            name=s['name'],
            device=s['device'],
            width=s['width'],
            height=s['height'],
            fps=s['fps'],
            video_format=s.get('video_format', 'mjpeg'),
            port=s.get('port', 5000),
            windows_host=self.windows_host,
            stream_opts=stream_opts,
        )
        self._opened[name] = cam
        return cam

    def close(self, name: str):
        cam = self._opened.pop(name, None)
        if cam:
            cam.release()