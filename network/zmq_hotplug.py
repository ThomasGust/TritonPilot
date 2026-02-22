"""network/zmq_hotplug.py

Helpers to make ZeroMQ links *hotpluggable*.

ROV ↔︎ topside power cycles can create TCP half-open connections. A SUB socket
that only receives may not detect the dead peer quickly (default TCP keepalive
can be hours), so it may not reconnect promptly when the ROV comes back.

We apply conservative best-effort options:
  - fast reconnect backoff
  - TCP keepalive (short idle/interval/count)
  - ZMQ heartbeats (if supported by the libzmq build)

All options are guarded so older libzmq/pyzmq builds keep working.
"""

from __future__ import annotations

from typing import Optional

import zmq


def _set(sock: zmq.Socket, opt: int, val) -> None:
    try:
        sock.setsockopt(opt, val)
    except Exception:
        pass


def apply_hotplug_opts(
    sock: zmq.Socket,
    *,
    linger_ms: int = 0,
    rcv_hwm: Optional[int] = None,
    snd_hwm: Optional[int] = None,
    conflate: Optional[bool] = None,
    rcv_timeout_ms: Optional[int] = None,
    snd_timeout_ms: Optional[int] = None,
    reconnect_ivl_ms: int = 250,
    reconnect_ivl_max_ms: int = 2000,
    heartbeat_ivl_ms: int = 1000,
    heartbeat_timeout_ms: int = 3000,
    heartbeat_ttl_ms: int = 6000,
    tcp_keepalive: bool = True,
    tcp_keepalive_idle_s: int = 10,
    tcp_keepalive_intvl_s: int = 5,
    tcp_keepalive_cnt: int = 3,
    immediate: Optional[bool] = None,
    tcp_nodelay: Optional[bool] = True,
    tos: Optional[int] = None,
    priority: Optional[int] = None,
) -> None:
    """Apply best-effort hotplug/reconnect options to a socket."""

    _set(sock, zmq.LINGER, int(linger_ms))

    if rcv_hwm is not None:
        _set(sock, zmq.RCVHWM, int(rcv_hwm))
    if snd_hwm is not None:
        _set(sock, zmq.SNDHWM, int(snd_hwm))

    if conflate is not None:
        try:
            _set(sock, zmq.CONFLATE, 1 if conflate else 0)
        except Exception:
            pass

    if rcv_timeout_ms is not None:
        _set(sock, zmq.RCVTIMEO, int(rcv_timeout_ms))
    if snd_timeout_ms is not None:
        _set(sock, zmq.SNDTIMEO, int(snd_timeout_ms))

    # Faster reconnect behavior (best-effort)
    try:
        _set(sock, getattr(zmq, "RECONNECT_IVL"), int(reconnect_ivl_ms))
        _set(sock, getattr(zmq, "RECONNECT_IVL_MAX"), int(reconnect_ivl_max_ms))
    except Exception:
        pass

    # Heartbeats (libzmq >= 4.1, best-effort)
    try:
        _set(sock, getattr(zmq, "HEARTBEAT_IVL"), int(heartbeat_ivl_ms))
        _set(sock, getattr(zmq, "HEARTBEAT_TIMEOUT"), int(heartbeat_timeout_ms))
        _set(sock, getattr(zmq, "HEARTBEAT_TTL"), int(heartbeat_ttl_ms))
    except Exception:
        pass

    # TCP keepalive (short settings so power cycles are detected quickly)
    if tcp_keepalive:
        try:
            _set(sock, getattr(zmq, "TCP_KEEPALIVE"), 1)
            _set(sock, getattr(zmq, "TCP_KEEPALIVE_IDLE"), int(tcp_keepalive_idle_s))
            _set(sock, getattr(zmq, "TCP_KEEPALIVE_INTVL"), int(tcp_keepalive_intvl_s))
            _set(sock, getattr(zmq, "TCP_KEEPALIVE_CNT"), int(tcp_keepalive_cnt))
        except Exception:
            pass

    if immediate is not None:
        try:
            _set(sock, getattr(zmq, "IMMEDIATE"), 1 if immediate else 0)
        except Exception:
            pass

    # Reduce latency for tiny control/telemetry frames (best-effort)
    if tcp_nodelay is not None:
        try:
            _set(sock, getattr(zmq, "TCP_NODELAY"), 1 if tcp_nodelay else 0)
        except Exception:
            pass

    # QoS hints (best-effort): TOS/DSCP and socket priority
    if tos is not None:
        try:
            _set(sock, getattr(zmq, "TOS"), int(tos))
        except Exception:
            pass
    if priority is not None:
        try:
            _set(sock, getattr(zmq, "PRIORITY"), int(priority))
        except Exception:
            pass
