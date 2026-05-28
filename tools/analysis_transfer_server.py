"""Read-only transfer server for TritonAnalysis media handoff."""

from __future__ import annotations

import argparse
import json
import mimetypes
import posixpath
import sys
import threading
import time
from dataclasses import asdict, dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlparse


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = REPO_ROOT / "recordings"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8765
SERVER_VERSION = 1


@dataclass(frozen=True)
class TransferFile:
    """One file advertised to the analysis computer."""

    path: str
    size: int
    mtime_ns: int


def _safe_relative_path(raw_path: str) -> PurePosixPath:
    decoded = unquote(str(raw_path or "")).replace("\\", "/")
    normalized = posixpath.normpath(decoded).lstrip("/")
    rel = PurePosixPath(normalized)
    if not normalized or normalized == "." or rel.is_absolute() or ".." in rel.parts:
        raise ValueError("unsafe transfer path")
    return rel


def _is_transfer_path_visible(rel: PurePosixPath, *, include_hidden: bool = False) -> bool:
    if not include_hidden and any(part.startswith(".") for part in rel.parts):
        return False
    return rel.suffix.lower() not in {".part", ".tmp"}


def resolve_transfer_path(root: Path, raw_path: str) -> Path:
    """Resolve a URL path safely under *root*."""
    rel = _safe_relative_path(raw_path)
    root = Path(root).expanduser().resolve()
    candidate = root.joinpath(*rel.parts).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("transfer path escapes root") from exc
    return candidate


def iter_transfer_files(
    root: Path,
    *,
    stable_seconds: float = 2.0,
    include_hidden: bool = False,
) -> list[TransferFile]:
    """Return files under *root* that are safe to copy."""
    root = Path(root).expanduser().resolve()
    if not root.exists():
        return []

    now = time.time()
    files: list[TransferFile] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        rel_posix = PurePosixPath(*rel.parts)
        if not _is_transfer_path_visible(rel_posix, include_hidden=include_hidden):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        if stable_seconds > 0.0 and now - stat.st_mtime < stable_seconds:
            continue
        rel_text = rel_posix.as_posix()
        files.append(TransferFile(path=rel_text, size=int(stat.st_size), mtime_ns=int(stat.st_mtime_ns)))
    files.sort(key=lambda item: item.path.lower())
    return files


def build_index(root: Path, *, stable_seconds: float = 2.0, include_hidden: bool = False) -> dict:
    """Build the JSON transfer index."""
    root = Path(root).expanduser().resolve()
    files = iter_transfer_files(root, stable_seconds=stable_seconds, include_hidden=include_hidden)
    total_bytes = sum(item.size for item in files)
    return {
        "type": "triton-analysis-transfer-index",
        "version": SERVER_VERSION,
        "generated_at": time.time(),
        "root_name": root.name,
        "stable_seconds": float(stable_seconds),
        "file_count": len(files),
        "total_bytes": total_bytes,
        "files": [asdict(item) for item in files],
    }


class AnalysisTransferServer(ThreadingHTTPServer):
    """HTTP server carrying transfer configuration."""

    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        root: Path,
        stable_seconds: float = 2.0,
        include_hidden: bool = False,
    ):
        super().__init__(server_address, AnalysisTransferRequestHandler)
        self.root = Path(root).expanduser().resolve()
        self.stable_seconds = float(stable_seconds)
        self.include_hidden = bool(include_hidden)
        self.request_count = 0
        self.last_request_ts = 0.0
        self.last_request_path = ""
        self.active_file_transfers = 0
        self.active_file_paths: list[str] = []
        self.last_file_path = ""
        self.last_file_bytes_sent = 0
        self.last_file_completed_ts = 0.0
        self._request_lock = threading.Lock()

    def note_request(self, path: str) -> None:
        """Record a lightweight heartbeat from the analysis computer."""
        with self._request_lock:
            self.request_count += 1
            self.last_request_ts = time.time()
            self.last_request_path = str(path or "")

    def begin_file_transfer(self, path: str, size: int) -> None:
        """Record that a file body is actively being sent to TritonAnalysis."""
        with self._request_lock:
            rel_path = str(path or "")
            self.active_file_transfers += 1
            self.active_file_paths.append(rel_path)
            self.last_file_path = rel_path
            self.last_file_bytes_sent = 0
            self.last_request_ts = time.time()

    def finish_file_transfer(self, path: str, bytes_sent: int) -> None:
        """Record the completion of a file body transfer."""
        with self._request_lock:
            rel_path = str(path or "")
            self.active_file_transfers = max(0, self.active_file_transfers - 1)
            try:
                self.active_file_paths.remove(rel_path)
            except ValueError:
                pass
            self.last_file_path = rel_path
            self.last_file_bytes_sent = int(bytes_sent)
            self.last_file_completed_ts = time.time()

    def request_snapshot(self) -> dict:
        """Return request stats without exposing the lock to callers."""
        with self._request_lock:
            return {
                "request_count": int(self.request_count),
                "last_request_ts": float(self.last_request_ts),
                "last_request_path": str(self.last_request_path),
                "active_file_transfers": int(self.active_file_transfers),
                "active_file_paths": list(self.active_file_paths),
                "last_file_path": str(self.last_file_path),
                "last_file_bytes_sent": int(self.last_file_bytes_sent),
                "last_file_completed_ts": float(self.last_file_completed_ts),
            }


class AnalysisTransferRequestHandler(BaseHTTPRequestHandler):
    """Serve a read-only JSON index and files from the recordings root."""

    server_version = "TritonAnalysisTransfer/1.0"

    @property
    def transfer_server(self) -> AnalysisTransferServer:
        return self.server  # type: ignore[return-value]

    def log_message(self, _format: str, *_args) -> None:
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        self.transfer_server.note_request(parsed.path)
        if parsed.path in {"/", "/health"}:
            self._send_json(
                {
                    "ok": True,
                    "service": "triton-analysis-transfer",
                    "version": SERVER_VERSION,
                    "root": str(self.transfer_server.root),
                }
            )
            return
        if parsed.path in {"/index.json", "/api/index"}:
            self._send_json(
                build_index(
                    self.transfer_server.root,
                    stable_seconds=self.transfer_server.stable_seconds,
                    include_hidden=self.transfer_server.include_hidden,
                )
            )
            return
        if parsed.path.startswith("/files/"):
            self._send_file(parsed.path[len("/files/") :])
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def _send_json(self, payload: dict) -> None:
        body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, raw_path: str) -> None:
        try:
            rel = _safe_relative_path(raw_path)
            if not _is_transfer_path_visible(rel, include_hidden=self.transfer_server.include_hidden):
                raise ValueError("transfer path is not visible")
            path = resolve_transfer_path(self.transfer_server.root, raw_path)
            stat = path.stat()
        except (OSError, ValueError):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(stat.st_size))
        self.send_header("Last-Modified", self.date_time_string(stat.st_mtime))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        rel_text = rel.as_posix()
        bytes_sent = 0
        self.transfer_server.begin_file_transfer(rel_text, int(stat.st_size))
        try:
            with path.open("rb") as handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    bytes_sent += len(chunk)
        finally:
            self.transfer_server.finish_file_transfer(rel_text, bytes_sent)


def file_url(base_url: str, rel_path: str) -> str:
    """Build the URL for a transfer file."""
    base = str(base_url).rstrip("/")
    rel = _safe_relative_path(rel_path)
    encoded = "/".join(quote(part) for part in rel.parts)
    return f"{base}/files/{encoded}"


def create_server(
    *,
    root: Path,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    stable_seconds: float = 2.0,
    include_hidden: bool = False,
) -> AnalysisTransferServer:
    """Create but do not start the transfer server."""
    return AnalysisTransferServer(
        (str(host), int(port)),
        root=root,
        stable_seconds=stable_seconds,
        include_hidden=include_hidden,
    )


def start_server_in_thread(server: AnalysisTransferServer) -> threading.Thread:
    """Start *server* in a daemon thread for tests or local simulation."""
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve TritonPilot recordings to TritonAnalysis over HTTP.")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="Recordings root to expose read-only.")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Bind host. Use 0.0.0.0 for the USB-Ethernet link.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Transfer server TCP port.")
    parser.add_argument(
        "--stable-seconds",
        type=float,
        default=2.0,
        help="Skip files modified more recently than this many seconds.",
    )
    parser.add_argument("--include-hidden", action="store_true", help="Include dotfiles and dot folders.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.root).expanduser().resolve()
    server = create_server(
        root=root,
        host=args.host,
        port=args.port,
        stable_seconds=args.stable_seconds,
        include_hidden=args.include_hidden,
    )
    host, port = server.server_address
    print(f"TritonPilot transfer server")
    print(f"Root: {root}")
    print(f"URL:  http://{host}:{port}/index.json")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping transfer server.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
