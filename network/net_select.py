from __future__ import annotations

"""Topside network selection helpers.

Goal: keep Wi‑Fi enabled (SSH, internet, etc.) while *preferring the tether*
for high-bandwidth video.

We do this by choosing a local IP address that:
  1) Can reach the ROV video RPC host, and
  2) Is on a non‑Wi‑Fi interface when possible.

This module is best-effort across Windows/Linux/macOS and stays stdlib-only.
"""

import os
import re
import socket
import subprocess
from dataclasses import dataclass
from typing import Iterable, Optional


_IPV4_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")


def parse_zmq_endpoint(ep: str) -> tuple[str, int]:
    """Parse endpoints like tcp://192.168.2.2:5555 -> ("192.168.2.2", 5555)."""
    ep = (ep or "").strip()
    if ep.startswith("tcp://"):
        ep = ep[len("tcp://"):]
    # tolerate accidental http:// etc
    ep = ep.split("//")[-1]
    host_port = ep.rsplit(":", 1)
    if len(host_port) != 2:
        raise ValueError(f"Bad endpoint (expected host:port): {ep!r}")
    host = host_port[0].strip("[]")
    port = int(host_port[1])
    return host, port


def _udp_route_local_ip(remote_host: str, remote_port: int = 9) -> str:
    """Return the local source IP chosen by the OS route to remote_host."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((remote_host, int(remote_port)))
        return s.getsockname()[0]
    finally:
        s.close()


def _tcp_can_connect_from(local_ip: str, remote_host: str, remote_port: int, timeout_s: float = 0.6) -> bool:
    """Best-effort: can we connect to remote_host:remote_port when binding local_ip?"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(float(timeout_s))
        s.bind((local_ip, 0))
        s.connect((remote_host, int(remote_port)))
        s.close()
        return True
    except Exception:
        try:
            s.close()
        except Exception:
            pass
        return False


@dataclass
class LocalAddr:
    ip: str
    iface: str | None = None
    is_wifi: bool | None = None


def _is_private_v4(ip: str) -> bool:
    try:
        parts = [int(p) for p in ip.split(".")]
        if len(parts) != 4:
            return False
        if parts[0] == 10:
            return True
        if parts[0] == 172 and 16 <= parts[1] <= 31:
            return True
        if parts[0] == 192 and parts[1] == 168:
            return True
        if parts[0] == 169 and parts[1] == 254:
            # link-local (still useful for direct USB ethernet)
            return True
        return False
    except Exception:
        return False


def _list_local_ipv4_linux() -> list[LocalAddr]:
    out: list[LocalAddr] = []
    try:
        txt = subprocess.check_output(["ip", "-4", "-o", "addr"], text=True, stderr=subprocess.DEVNULL)
        # format: "2: eth0    inet 192.168.2.1/24 brd ..."
        for line in txt.splitlines():
            parts = line.split()
            if len(parts) < 4:
                continue
            iface = parts[1]
            if "inet" not in parts:
                continue
            i = parts.index("inet")
            if i + 1 >= len(parts):
                continue
            ip = parts[i + 1].split("/")[0]
            if ip.startswith("127."):
                continue
            is_wifi = os.path.exists(f"/sys/class/net/{iface}/wireless")
            out.append(LocalAddr(ip=ip, iface=iface, is_wifi=is_wifi))
    except Exception:
        # fallback: hostname resolution (often incomplete)
        try:
            for fam, _, _, _, sa in socket.getaddrinfo(socket.gethostname(), None):
                if fam == socket.AF_INET:
                    ip = sa[0]
                    if not ip.startswith("127."):
                        out.append(LocalAddr(ip=ip, iface=None, is_wifi=None))
        except Exception:
            pass
    return out


def _powershell(cmd: str) -> str:
    # hide PowerShell window if possible
    creationflags = 0
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        creationflags = subprocess.CREATE_NO_WINDOW
    return subprocess.check_output(
        ["powershell", "-NoProfile", "-Command", cmd],
        text=True,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )


def _list_local_ipv4_windows() -> list[LocalAddr]:
    out: list[LocalAddr] = []
    # Prefer PowerShell (accurate). Fall back to ipconfig parsing.
    try:
        ps = (
            "Get-NetIPAddress -AddressFamily IPv4 | "
            "Where-Object {$_.IPAddress -ne '127.0.0.1'} | "
            "Select-Object IPAddress,InterfaceAlias | "
            "Format-Table -HideTableHeaders"
        )
        txt = _powershell(ps)
        for line in txt.splitlines():
            line = line.strip()
            if not line:
                continue
            m = _IPV4_RE.search(line)
            if not m:
                continue
            ip = m.group(1)
            # interface alias is whatever remains
            alias = line.replace(ip, "").strip() or None
            is_wifi = None
            if alias:
                a = alias.lower()
                is_wifi = ("wi-fi" in a) or ("wifi" in a) or ("wlan" in a) or ("wireless" in a)
            out.append(LocalAddr(ip=ip, iface=alias, is_wifi=is_wifi))
        if out:
            return out
    except Exception:
        pass

    # fallback: ipconfig
    try:
        txt = subprocess.check_output(["ipconfig"], text=True, stderr=subprocess.DEVNULL)
        current_iface: str | None = None
        for line in txt.splitlines():
            if line and not line.startswith(" ") and ":" in line:
                # "Ethernet adapter Ethernet:" / "Wireless LAN adapter Wi-Fi:"
                current_iface = line.strip().rstrip(":")
                continue
            if "IPv4 Address" in line or "IPv4-adres" in line:
                m = _IPV4_RE.search(line)
                if m:
                    ip = m.group(1)
                    if ip.startswith("127."):
                        continue
                    alias = current_iface
                    is_wifi = None
                    if alias:
                        a = alias.lower()
                        is_wifi = ("wi-fi" in a) or ("wifi" in a) or ("wlan" in a) or ("wireless" in a)
                    out.append(LocalAddr(ip=ip, iface=alias, is_wifi=is_wifi))
    except Exception:
        pass
    return out


def list_local_ipv4_addrs() -> list[LocalAddr]:
    if os.name == "nt":
        return _list_local_ipv4_windows()
    return _list_local_ipv4_linux()


def choose_video_receive_ip(
    remote_host: str,
    remote_port: int,
    prefer_wired: bool = True,
    require_private: bool = True,
) -> str:
    """Pick the best local IP to receive video.

    We test candidates by attempting a TCP connect to the ROV video RPC port
    while binding to each local IP.

    This works well when Wi‑Fi and tether are both up:
      - If tether can reach the ROV host, it will be preferred.
      - Wi‑Fi stays enabled for SSH and other tasks.
    """

    # 1) Enumerate candidates
    cands = list_local_ipv4_addrs()
    if require_private:
        cands = [c for c in cands if _is_private_v4(c.ip)]

    # 2) Route-selected IP (fallback)
    try:
        route_ip = _udp_route_local_ip(remote_host)
    except Exception:
        route_ip = None

    # 3) Score candidates by connectivity + interface type
    scored: list[tuple[int, LocalAddr]] = []
    for c in cands:
        ok = _tcp_can_connect_from(c.ip, remote_host, remote_port)
        if not ok:
            continue
        score = 0
        if prefer_wired and (c.is_wifi is False):
            score += 10
        if prefer_wired and (c.is_wifi is True):
            score -= 3
        if route_ip and c.ip == route_ip:
            score += 2
        scored.append((score, c))

    if scored:
        scored.sort(key=lambda t: t[0], reverse=True)
        return scored[0][1].ip

    # 4) Fallback: route-selected or first non-loopback
    if route_ip:
        return route_ip
    for c in list_local_ipv4_addrs():
        if c.ip and not c.ip.startswith("127."):
            return c.ip
    return "127.0.0.1"
