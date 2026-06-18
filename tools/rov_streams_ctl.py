#!/usr/bin/env python3
"""Headless start/stop/status for ROV video streams (no GUI, no local receiver).

Starts the ROV-side GStreamer pipelines straight from ``streams.json`` so the
onboard snapshot tee/cache runs and the capture benchmark has something to pull.
It does NOT open a local UDP receiver, so display is not consumed -- this is for
bench capture work, not for viewing video.

Examples (PowerShell):
    python .\tools\rov_streams_ctl.py status
    python .\tools\rov_streams_ctl.py start "Primary Camera" "Aux Camera"
    python .\tools\rov_streams_ctl.py start-all
    python .\tools\rov_streams_ctl.py stop "Primary Camera"
    python .\tools\rov_streams_ctl.py stop-all
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from video.cam import RemoteCameraManager

_PASS_THROUGH = (
    "encode",
    "h264_bitrate",
    "h264_gop",
    "transport",
    "rtp_pt_jpeg",
    "rtp_pt_h264",
    "rtp_mtu",
    "latency_ms",
    "sync",
    "extra",
)


def _start_kwargs(manager: RemoteCameraManager, name: str) -> dict:
    opts = manager._merged_stream_options(name)
    kwargs = dict(
        name=opts["name"],
        device=opts["device"],
        width=opts["width"],
        height=opts["height"],
        fps=opts["fps"],
        video_format=opts.get("video_format", "mjpeg"),
        host=str(manager.windows_host or "192.168.1.1"),
        port=opts.get("port", 5000),
    )
    for key in _PASS_THROUGH:
        if key in opts and opts[key] is not None:
            kwargs[key] = opts[key]
    return kwargs


def _print_status(manager: RemoteCameraManager) -> None:
    try:
        status = manager.rov.list_stream_status()
    except Exception as exc:
        print(f"status failed: {exc}", file=sys.stderr)
        return
    if not isinstance(status, dict) or not status:
        print("(no streams running)")
        return
    for name, st in status.items():
        if not isinstance(st, dict):
            print(f"{name}: {st}")
            continue
        print(
            f"{name}: running={st.get('running')} "
            f"snapshot_ready={st.get('snapshot_ready')} "
            f"cache_enabled={st.get('snapshot_cache_enabled')} "
            f"cache_frames={st.get('snapshot_cache_frames')} "
            f"last_error={st.get('last_error')}"
        )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Headless ROV stream control")
    ap.add_argument("action", choices=["status", "start", "start-all", "stop", "stop-all"])
    ap.add_argument("names", nargs="*", help="stream names (for start/stop)")
    ap.add_argument("--streams", default=str(config.STREAMS_FILE))
    ap.add_argument("--endpoint", default=None)
    args = ap.parse_args(argv)

    manager = RemoteCameraManager(args.streams)
    if args.endpoint:
        manager.set_rpc_endpoint(args.endpoint)
    print(f"ROV video RPC: {getattr(manager.rov, 'endpoint', config.VIDEO_RPC_ENDPOINT)}")

    if args.action == "status":
        _print_status(manager)
        return 0

    if args.action == "stop-all":
        try:
            status = manager.rov.list_stream_status() or {}
            names = list(status.keys())
        except Exception as exc:
            print(f"could not list streams: {exc}", file=sys.stderr)
            names = []
        for name in names:
            try:
                manager.rov.stop_stream(name=name)
                print(f"stopped {name}")
            except Exception as exc:
                print(f"stop {name} failed: {exc}", file=sys.stderr)
        return 0

    if args.action == "stop":
        for name in args.names:
            try:
                manager.rov.stop_stream(name=name)
                print(f"stopped {name}")
            except Exception as exc:
                print(f"stop {name} failed: {exc}", file=sys.stderr)
        return 0

    # start / start-all
    names = manager.list_available() if args.action == "start-all" else args.names
    if not names:
        print("no stream names given", file=sys.stderr)
        return 2
    for name in names:
        try:
            kwargs = _start_kwargs(manager, name)
            manager.rov.start_stream(**kwargs)
            print(f"started {name} (port={kwargs['port']}, {kwargs['video_format']})")
        except Exception as exc:
            print(f"start {name} failed: {exc}", file=sys.stderr)
    print("---")
    _print_status(manager)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
