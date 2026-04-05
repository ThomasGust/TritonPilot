from __future__ import annotations

import queue
import threading
from typing import Callable, Optional

import zmq

from config import MANAGEMENT_RPC_ENDPOINT
from network.zmq_hotplug import apply_hotplug_opts


class ROVManagementRPC:
    """Small REQ/REP client for the ROV management service."""

    def __init__(self, endpoint: str = MANAGEMENT_RPC_ENDPOINT, timeout_ms: int = 8000):
        self.ctx = zmq.Context.instance()
        self.endpoint = str(endpoint)
        self.timeout_ms = int(timeout_ms)
        self._rpc_lock = threading.Lock()
        self.sock = self._make_sock()

    def _make_sock(self) -> zmq.Socket:
        sock = self.ctx.socket(zmq.REQ)
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
            tcp_nodelay=True,
            tos=0x88,
            priority=5,
        )
        try:
            sock.setsockopt(zmq.REQ_RELAXED, 1)  # type: ignore[attr-defined]
            sock.setsockopt(zmq.REQ_CORRELATE, 1)  # type: ignore[attr-defined]
        except Exception:
            pass
        sock.connect(self.endpoint)
        return sock

    def close(self) -> None:
        try:
            self.sock.close(0)
        except Exception:
            pass

    def _reset_sock(self) -> None:
        try:
            self.sock.close(0)
        except Exception:
            pass
        self.sock = self._make_sock()

    def call(self, cmd: str, args: Optional[dict] = None):
        payload = {"cmd": str(cmd)}
        if args:
            payload["args"] = dict(args)

        with self._rpc_lock:
            try:
                self.sock.send_json(payload)
                reply = self.sock.recv_json()
            except zmq.Again as e:
                self._reset_sock()
                raise TimeoutError(
                    f"ROV management RPC timed out calling '{cmd}' "
                    f"at {self.endpoint} (is TritonOS running?)"
                ) from e
            except zmq.ZMQError as e:
                self._reset_sock()
                raise ConnectionError(f"ROV management RPC error calling '{cmd}': {e}") from e

        if not isinstance(reply, dict):
            raise RuntimeError("ROV management RPC returned a non-JSON object")

        if not reply.get("ok"):
            raise RuntimeError(str(reply.get("error") or "unknown management RPC error"))

        return reply.get("data")


class ManagementRpcService:
    """Serializes management RPC calls onto a single background worker thread."""

    def __init__(
        self,
        endpoint: str = MANAGEMENT_RPC_ENDPOINT,
        on_result: Optional[Callable[[dict], None]] = None,
        *,
        timeout_ms: int = 8000,
    ):
        self.endpoint = str(endpoint)
        self.on_result = on_result
        self.timeout_ms = int(timeout_ms)

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._queue: queue.Queue[dict] = queue.Queue()
        self._next_request_id = 1
        self._id_lock = threading.Lock()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._queue.put({"kind": "stop"})
        if self._thread:
            self._thread.join(timeout=1.0)

    def request(self, cmd: str, args: Optional[dict] = None, meta: Optional[dict] = None) -> int:
        with self._id_lock:
            request_id = int(self._next_request_id)
            self._next_request_id += 1
        self._queue.put(
            {
                "kind": "request",
                "request_id": request_id,
                "cmd": str(cmd),
                "args": dict(args or {}),
                "meta": dict(meta or {}),
            }
        )
        return request_id

    def _emit(self, result: dict) -> None:
        if not self.on_result:
            return
        try:
            self.on_result(result)
        except Exception:
            pass

    def _run(self) -> None:
        client = ROVManagementRPC(endpoint=self.endpoint, timeout_ms=self.timeout_ms)
        try:
            while not self._stop.is_set():
                try:
                    item = self._queue.get(timeout=0.1)
                except queue.Empty:
                    continue

                if item.get("kind") != "request":
                    continue

                request_id = int(item.get("request_id", 0) or 0)
                cmd = str(item.get("cmd") or "")
                args = dict(item.get("args") or {})
                meta = dict(item.get("meta") or {})

                try:
                    data = client.call(cmd, args)
                    self._emit(
                        {
                            "request_id": request_id,
                            "cmd": cmd,
                            "meta": meta,
                            "ok": True,
                            "data": data,
                        }
                    )
                except Exception as e:
                    self._emit(
                        {
                            "request_id": request_id,
                            "cmd": cmd,
                            "meta": meta,
                            "ok": False,
                            "error": str(e),
                        }
                    )
        finally:
            client.close()
