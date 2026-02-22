# telemetry/sensor_service.py
from __future__ import annotations

import json
import threading
import time
from typing import Callable, Optional

import zmq

from network.zmq_hotplug import apply_hotplug_opts


class SensorSubscriberService:
    """Background ZMQ SUB that calls a callback for every sensor message.

    Hotplug goals:
      - Pilot app can start before the ROV (no blocking / no crash)
      - If the ROV power-cycles, the subscriber should recover quickly
      - Avoid TCP half-open stalls by recreating the socket when no messages
        arrive for a while

    Note: ZMQ sockets are *thread-affine*. We create/use/close the socket in
    the background receiver thread.
    """

    def __init__(
        self,
        endpoint: str,
        on_message: Optional[Callable[[dict], None]] = None,
        debug: bool = False,
        *,
        poll_ms: int = 200,
        stale_reconnect_s: float = 3.0,
        initial_reconnect_s: float = 5.0,
    ):
        self.endpoint = endpoint
        self.on_message = on_message
        self.debug = bool(debug)

        self.poll_ms = int(poll_ms)
        self.stale_reconnect_s = float(stale_reconnect_s)
        self.initial_reconnect_s = float(initial_reconnect_s)

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Created inside the receiver thread
        self._sock: Optional[zmq.Socket] = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        self._close_sock()

    # --- internals -----------------------------------------------------

    def _close_sock(self):
        s = self._sock
        self._sock = None
        if s is not None:
            try:
                s.close(0)
            except Exception:
                pass

    def _make_sock(self) -> zmq.Socket:
        ctx = zmq.Context.instance()
        sock = ctx.socket(zmq.SUB)

        # We only care about the most recent telemetry.
        apply_hotplug_opts(
            sock,
            linger_ms=0,
            rcv_hwm=1,
            conflate=True,
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
            tos=0x88,  # DSCP AF41 for telemetry (best-effort)
            priority=5,
        )

        sock.setsockopt_string(zmq.SUBSCRIBE, "")
        sock.connect(self.endpoint)
        return sock

    def _reset_sock(self):
        if self.debug:
            print(f"[sensor] reset SUB -> {self.endpoint}")
        self._close_sock()
        # Avoid tight reset loops.
        time.sleep(0.05)
        self._sock = self._make_sock()

    def _run(self):
        self._sock = self._make_sock()
        poller = zmq.Poller()
        poller.register(self._sock, zmq.POLLIN)

        start_ts = time.time()
        last_rx = 0.0
        saw_any = False

        while not self._stop.is_set():
            try:
                events = dict(poller.poll(self.poll_ms))
                if self._sock not in events:
                    # No message this tick; detect staleness and force a reconnect.
                    now = time.time()
                    if saw_any:
                        if (now - last_rx) > self.stale_reconnect_s:
                            old = self._sock
                            self._reset_sock()
                            try:
                                poller.unregister(old)
                            except Exception:
                                pass
                            poller.register(self._sock, zmq.POLLIN)
                    else:
                        # If we've never seen data, periodically recreate the socket.
                        if (now - start_ts) > self.initial_reconnect_s:
                            start_ts = now
                            old = self._sock
                            self._reset_sock()
                            try:
                                poller.unregister(old)
                            except Exception:
                                pass
                            poller.register(self._sock, zmq.POLLIN)
                    continue

                # Drain backlog (keep latest). CONFLATE already helps when supported.
                while True:
                    try:
                        raw = self._sock.recv_string(flags=zmq.NOBLOCK)
                    except zmq.Again:
                        break
                    except zmq.ZMQError as e:
                        if self.debug:
                            print(f"[sensor] recv ZMQError: {e}")
                        old = self._sock
                        self._reset_sock()
                        try:
                            poller.unregister(old)
                        except Exception:
                            pass
                        poller.register(self._sock, zmq.POLLIN)
                        break

                    try:
                        msg = json.loads(raw)
                    except Exception:
                        if self.debug:
                            print("[sensor] bad json:", raw)
                        continue

                    saw_any = True
                    last_rx = time.time()

                    if self.on_message:
                        try:
                            self.on_message(msg)
                        except Exception:
                            # Never crash the subscriber thread due to UI callbacks
                            pass
                    elif self.debug:
                        sensor = msg.get("sensor")
                        msg_type = msg.get("type")
                        print(f"[sensor] {sensor}/{msg_type}: {msg}")

            except zmq.ZMQError as e:
                # Anything unexpected: recreate the socket.
                if self.debug:
                    print(f"[sensor] loop ZMQError: {e}")
                old = self._sock
                self._reset_sock()
                try:
                    poller.unregister(old)
                except Exception:
                    pass
                poller.register(self._sock, zmq.POLLIN)
            except Exception as e:
                if self.debug:
                    print(f"[sensor] unexpected error: {e}")
                time.sleep(0.05)

        self._close_sock()
