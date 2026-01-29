#!/usr/bin/env python3
"""Minimal network diagnostics client (pilot-side).

Talks to TritonOS/tools/netdiag_server.py.

Examples:
  # UDP RTT / loss
  python -m tools.netdiag_client --host 192.168.1.4 udp

  # Uplink throughput (pilot -> ROV)
  python -m tools.netdiag_client --host 192.168.1.4 tcp-rx --seconds 5

  # Downlink throughput (ROV -> pilot)
  python -m tools.netdiag_client --host 192.168.1.4 tcp-tx --seconds 5
"""

from __future__ import annotations

import argparse
import json
import math
import socket
import statistics
import time
from typing import Optional, Tuple


META_MARKER = b"\n__NETDIAG_META__\n"


def _human_bps(bytes_per_s: float) -> str:
    bps = float(bytes_per_s) * 8.0
    if bps >= 1e9:
        return f"{bps/1e9:.2f} Gb/s"
    if bps >= 1e6:
        return f"{bps/1e6:.2f} Mb/s"
    if bps >= 1e3:
        return f"{bps/1e3:.1f} Kb/s"
    return f"{bps:.0f} b/s"


def _tcp_connect(host: str, port: int, timeout_s: float = 3.0) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout_s)
    s.connect((host, int(port)))
    s.settimeout(1.0)
    return s


def _tcp_send_req(s: socket.socket, req: dict) -> None:
    line = (json.dumps(req, separators=(",", ":")) + "\n").encode("utf-8")
    s.sendall(line)


def udp_rtt_loss(host: str, port: int, count: int, interval_s: float, payload_size: int, timeout_s: float) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout_s)
    addr = (host, int(port))

    payload = b"x" * max(0, int(payload_size) - 16)

    rtts_ms = []
    sent = 0
    recv = 0

    for seq in range(int(count)):
        sent += 1
        t0 = time.time()
        pkt = (f"{t0:.6f}|{seq}".encode("ascii") + b"|" + payload)
        try:
            sock.sendto(pkt, addr)
        except Exception:
            time.sleep(interval_s)
            continue

        try:
            data, _ = sock.recvfrom(65535)
            if not data:
                raise RuntimeError("empty")
            t1 = time.time()
            recv += 1
            rtts_ms.append((t1 - t0) * 1000.0)
        except Exception:
            # timeout / loss
            pass

        time.sleep(max(0.0, float(interval_s)))

    loss = 0.0 if sent == 0 else (1.0 - (recv / sent))

    print("UDP RTT / loss")
    print(f"  host={host}:{port} sent={sent} recv={recv} loss={loss*100:.1f}%")
    if not rtts_ms:
        return

    rtts_ms.sort()
    avg = statistics.mean(rtts_ms)
    p50 = rtts_ms[int(0.50 * (len(rtts_ms) - 1))]
    p95 = rtts_ms[int(0.95 * (len(rtts_ms) - 1))]
    print(
        "  rtt_ms: "
        f"min={rtts_ms[0]:.2f} avg={avg:.2f} p50={p50:.2f} p95={p95:.2f} max={rtts_ms[-1]:.2f}"
    )
    if len(rtts_ms) >= 2:
        jitter = statistics.pstdev(rtts_ms)
        print(f"  jitter_ms (stddev)={jitter:.2f}")


def tcp_uplink(host: str, port: int, seconds: float, chunk_size: int) -> None:
    s = _tcp_connect(host, port)
    req = {"mode": "rx", "seconds": float(seconds), "chunk_size": int(chunk_size)}
    _tcp_send_req(s, req)

    payload = b"\x00" * int(chunk_size)
    end_at = time.time() + float(seconds)
    sent = 0
    try:
        while time.time() < end_at:
            s.sendall(payload)
            sent += len(payload)
    except Exception:
        pass
    try:
        s.shutdown(socket.SHUT_WR)
    except Exception:
        pass

    # Read JSON response line.
    buf = b""
    start = time.time()
    while b"\n" not in buf and time.time() - start < 5.0:
        try:
            chunk = s.recv(4096)
        except Exception:
            break
        if not chunk:
            break
        buf += chunk
    try:
        line = buf.split(b"\n", 1)[0]
        resp = json.loads(line.decode("utf-8", errors="replace") or "{}")
    except Exception:
        resp = {}
    try:
        s.close()
    except Exception:
        pass

    bytes_meas = resp.get("bytes")
    dur = resp.get("duration_s")
    if isinstance(bytes_meas, (int, float)) and isinstance(dur, (int, float)) and dur > 0:
        thr = float(bytes_meas) / float(dur)
        print("TCP uplink (pilot → ROV)")
        print(f"  sent≈{sent}B server_counted={int(bytes_meas)}B duration={dur:.2f}s")
        print(f"  throughput={_human_bps(thr)}")
    else:
        # Fallback to local timing (less accurate)
        print("TCP uplink (pilot → ROV)")
        print(f"  sent≈{sent}B")


def _split_meta(payload: bytes) -> Tuple[bytes, Optional[dict]]:
    idx = payload.rfind(META_MARKER)
    if idx < 0:
        return payload, None
    data = payload[:idx]
    meta_blob = payload[idx + len(META_MARKER) :]
    line = meta_blob.split(b"\n", 1)[0]
    try:
        meta = json.loads(line.decode("utf-8", errors="replace") or "{}")
    except Exception:
        meta = None
    return data, meta


def tcp_downlink(host: str, port: int, seconds: float, chunk_size: int) -> None:
    s = _tcp_connect(host, port)
    req = {"mode": "tx", "seconds": float(seconds), "chunk_size": int(chunk_size)}
    _tcp_send_req(s, req)

    t0 = time.time()
    buf = bytearray()
    try:
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    except Exception:
        pass
    t1 = time.time()
    try:
        s.close()
    except Exception:
        pass

    data, meta = _split_meta(bytes(buf))
    bytes_rx = len(data)
    dur = max(1e-6, t1 - t0)
    thr = bytes_rx / dur

    print("TCP downlink (ROV → pilot)")
    print(f"  received={bytes_rx}B duration={dur:.2f}s")
    print(f"  throughput={_human_bps(thr)}")
    if isinstance(meta, dict) and meta.get("ok"):
        try:
            ms = f" server_sent={int(meta.get('bytes', 0))}B server_dur={float(meta.get('duration_s', 0.0)):.2f}s"
        except Exception:
            ms = ""
        if ms:
            print(f"  (server:{ms.strip()})")


def main() -> None:
    ap = argparse.ArgumentParser(description="Tether network diagnostics client")
    ap.add_argument("mode", choices=["udp", "tcp-rx", "tcp-tx"], help="test type")
    ap.add_argument("--host", default=None, help="ROV host/IP (default: from config.py ROV_HOST)")
    ap.add_argument("--port", type=int, default=7700, help="netdiag port (default: 7700)")
    ap.add_argument("--seconds", type=float, default=5.0, help="tcp test duration (default: 5)")
    ap.add_argument("--chunk", type=int, default=65536, help="tcp chunk size bytes (default: 65536)")
    ap.add_argument("--count", type=int, default=50, help="udp packet count (default: 50)")
    ap.add_argument("--interval", type=float, default=0.05, help="udp interval seconds (default: 0.05)")
    ap.add_argument("--size", type=int, default=256, help="udp packet size bytes (default: 256)")
    ap.add_argument("--timeout", type=float, default=0.25, help="udp timeout seconds (default: 0.25)")
    args = ap.parse_args()

    host = args.host
    if not host:
        try:
            from config import ROV_HOST as _ROV_HOST

            host = str(_ROV_HOST)
        except Exception:
            host = "192.168.1.4"

    if args.mode == "udp":
        udp_rtt_loss(host, args.port, args.count, args.interval, args.size, args.timeout)
    elif args.mode == "tcp-rx":
        tcp_uplink(host, args.port, args.seconds, args.chunk)
    else:
        tcp_downlink(host, args.port, args.seconds, args.chunk)


if __name__ == "__main__":
    main()
