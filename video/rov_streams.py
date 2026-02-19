from __future__ import annotations

from config import VIDEO_RPC_ENDPOINT
import zmq
import logging

from network.zmq_hotplug import apply_hotplug_opts


logger = logging.getLogger(__name__)


class ROVStreams:
    """Small RPC client for the ROV-side video control service.

    Failsafe behavior:
      - uses send/recv timeouts so the GUI never hangs if the ROV isn't up yet
      - resets the REQ socket on timeout/state errors
    """

    def __init__(self, endpoint: str = VIDEO_RPC_ENDPOINT, timeout_ms: int = 3000):
        self.ctx = zmq.Context.instance()
        self.endpoint = endpoint
        self.timeout_ms = int(timeout_ms)
        self.sock = self._make_sock()

    def _make_sock(self):
        sock = self.ctx.socket(zmq.REQ)
        # Hotplug-friendly options:
        #  - no hang on close
        #  - timeouts so UI never blocks
        #  - keepalive/heartbeats so power-cycles are detected promptly
        apply_hotplug_opts(
            sock,
            linger_ms=0,
            rcv_timeout_ms=self.timeout_ms,
            snd_timeout_ms=self.timeout_ms,
            reconnect_ivl_ms=250,
            reconnect_ivl_max_ms=2000,
            heartbeat_ivl_ms=1000,
            heartbeat_timeout_ms=3000,
            heartbeat_ttl_ms=6000,
            tcp_keepalive=True,
            tcp_keepalive_idle_s=10,
            tcp_keepalive_intvl_s=5,
            tcp_keepalive_cnt=3,
        )
        # Allow send even if a previous recv timed out (best effort; not all libzmq expose these)
        try:
            sock.setsockopt(zmq.REQ_RELAXED, 1)  # type: ignore[attr-defined]
            sock.setsockopt(zmq.REQ_CORRELATE, 1)  # type: ignore[attr-defined]
        except Exception:
            pass
        sock.connect(self.endpoint)
        return sock

    def _reset_sock(self):
        try:
            self.sock.close(0)
        except Exception:
            pass
        self.sock = self._make_sock()

    def _call(self, cmd: str, **args):
        try:
            self.sock.send_json({"cmd": cmd, "args": args})
            reply = self.sock.recv_json()
        except zmq.Again as e:
            # Timeout (ROV down / not responding)
            self._reset_sock()
            raise TimeoutError(f"ROV video RPC timed out calling '{cmd}' (is TritonOS running?)") from e
        except zmq.ZMQError as e:
            # REQ state errors or disconnects
            self._reset_sock()
            raise ConnectionError(f"ROV video RPC error calling '{cmd}': {e}") from e

        data = reply.get("data")
        # Messages may appear either top-level (error path) or inside data (success path)
        msgs = reply.get("messages")
        if (not msgs) and isinstance(data, dict):
            msgs = data.get("messages")

        if not reply.get("ok"):
            err = str(reply.get("error") or "unknown error")
            if msgs:
                try:
                    err = err + "\n" + "\n".join([str(m) for m in msgs])
                except Exception:
                    pass
            raise RuntimeError(f"ROV error: {err}")

        if msgs:
            try:
                for m in msgs:
                    logger.warning("ROV video: %s", m)
            except Exception:
                pass

        return data

    def start_stream(self, **kwargs):
        return self._call("start_stream", **kwargs)

    def stop_stream(self, **kwargs):
        return self._call("stop_stream", **kwargs)

    def list_devices(self):
        return self._call("list_devices")

    def list_status(self):
        return self._call("status")

    def net_info(self):
        """Return ROV-side interface/IP info (best-effort)."""
        return self._call("net_info")


# --- compatibility helpers (used by some tooling / older code) ---

def normalize_device(dev: dict) -> dict:
    """Best-effort normalization of a device dict returned by the ROV video service."""
    if dev is None:
        return {}
    out = dict(dev)
    # common aliases
    if "device" in out and "path" not in out:
        out["path"] = out["device"]
    return out

def is_probably_camera(dev: dict) -> bool:
    """Heuristic: treat V4L2 devices (by-path/by-id or /dev/video*) as cameras by default."""
    d = normalize_device(dev)
    path = str(d.get("path") or d.get("device") or "")
    return path.startswith("/dev/v4l/by-path/") or path.startswith("/dev/v4l/by-id/") or path.startswith("/dev/video")

def list_real_cameras(devs: list[dict] | None) -> list[dict]:
    """Filter device list to likely cameras."""
    if not devs:
        return []
    return [normalize_device(d) for d in devs if is_probably_camera(d)]
