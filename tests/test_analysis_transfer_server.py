import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from tools.analysis_transfer_server import create_server, file_url, start_server_in_thread


def _server_url(server) -> str:
    host, port = server.server_address
    return f"http://{host}:{port}"


def test_analysis_transfer_server_indexes_and_serves_files(tmp_path: Path):
    root = tmp_path / "recordings"
    run = root / "run_01"
    run.mkdir(parents=True)
    source = run / "frame one.txt"
    source.write_bytes(b"hello analysis\n")

    server = create_server(root=root, host="127.0.0.1", port=0, stable_seconds=0.0)
    thread = start_server_in_thread(server)
    try:
        base_url = _server_url(server)
        index = json.loads(urllib.request.urlopen(f"{base_url}/index.json", timeout=5).read().decode("utf-8"))

        assert index["type"] == "triton-analysis-transfer-index"
        assert index["file_count"] == 1
        assert index["files"][0]["path"] == "run_01/frame one.txt"
        assert server.request_snapshot()["request_count"] >= 1

        data = urllib.request.urlopen(file_url(base_url, "run_01/frame one.txt"), timeout=5).read()
        assert data == b"hello analysis\n"
        for _attempt in range(50):
            snapshot = server.request_snapshot()
            if snapshot["active_file_transfers"] == 0:
                break
            time.sleep(0.01)
        assert snapshot["request_count"] >= 2
        assert snapshot["last_request_path"] == "/files/run_01/frame%20one.txt"
        assert snapshot["active_file_transfers"] == 0
        assert snapshot["last_file_path"] == "run_01/frame one.txt"
        assert snapshot["last_file_bytes_sent"] == len(b"hello analysis\n")
        assert snapshot["last_file_completed_ts"] > 0.0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1.0)


def test_analysis_transfer_server_rejects_path_traversal(tmp_path: Path):
    root = tmp_path / "recordings"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("nope\n", encoding="utf-8")

    server = create_server(root=root, host="127.0.0.1", port=0, stable_seconds=0.0)
    thread = start_server_in_thread(server)
    try:
        base_url = _server_url(server)
        try:
            urllib.request.urlopen(f"{base_url}/files/../secret.txt", timeout=5).read()
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
        else:
            raise AssertionError("path traversal request should fail")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1.0)


def test_analysis_transfer_server_does_not_serve_hidden_or_temp_files(tmp_path: Path):
    root = tmp_path / "recordings"
    root.mkdir()
    hidden = root / ".hidden.txt"
    hidden.write_bytes(b"secret\n")
    partial = root / "capture.part"
    partial.write_bytes(b"incomplete\n")

    server = create_server(root=root, host="127.0.0.1", port=0, stable_seconds=0.0)
    thread = start_server_in_thread(server)
    try:
        base_url = _server_url(server)
        index = json.loads(urllib.request.urlopen(f"{base_url}/index.json", timeout=5).read().decode("utf-8"))
        assert index["file_count"] == 0

        for rel_path in (".hidden.txt", "capture.part"):
            try:
                urllib.request.urlopen(file_url(base_url, rel_path), timeout=5).read()
            except urllib.error.HTTPError as exc:
                assert exc.code == 404
            else:
                raise AssertionError(f"{rel_path} should not be directly served")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1.0)


def test_analysis_transfer_server_events_report_visible_index_changes(tmp_path: Path):
    root = tmp_path / "recordings"
    root.mkdir()
    source = root / "first.txt"
    source.write_text("first\n", encoding="utf-8")

    server = create_server(root=root, host="127.0.0.1", port=0, stable_seconds=0.0)
    thread = start_server_in_thread(server)
    try:
        base_url = _server_url(server)
        first = json.loads(urllib.request.urlopen(f"{base_url}/events?since=0&timeout=0", timeout=5).read())

        assert first["type"] == "triton-analysis-transfer-event"
        assert first["changed"] is True
        assert first["event_id"] >= 1
        assert first["file_count"] == 1

        unchanged = json.loads(
            urllib.request.urlopen(f"{base_url}/events?since={first['event_id']}&timeout=0", timeout=5).read()
        )
        assert unchanged["changed"] is False
        assert unchanged["event_id"] == first["event_id"]

        (root / "second.txt").write_text("second\n", encoding="utf-8")
        changed = json.loads(
            urllib.request.urlopen(f"{base_url}/events?since={first['event_id']}&timeout=2", timeout=5).read()
        )
        assert changed["changed"] is True
        assert changed["event_id"] > first["event_id"]
        assert changed["file_count"] == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1.0)


def test_analysis_transfer_server_events_long_poll_until_new_file(tmp_path: Path):
    root = tmp_path / "recordings"
    root.mkdir()
    (root / "first.txt").write_text("first\n", encoding="utf-8")

    server = create_server(root=root, host="127.0.0.1", port=0, stable_seconds=0.0)
    thread = start_server_in_thread(server)
    responses: list[dict] = []
    try:
        base_url = _server_url(server)
        first = json.loads(urllib.request.urlopen(f"{base_url}/events?since=0&timeout=0", timeout=5).read())

        def _wait_for_event() -> None:
            payload = urllib.request.urlopen(
                f"{base_url}/events?since={first['event_id']}&timeout=2",
                timeout=5,
            ).read()
            responses.append(json.loads(payload.decode("utf-8")))

        waiter = threading.Thread(target=_wait_for_event)
        started = time.time()
        waiter.start()
        time.sleep(0.2)
        (root / "second.txt").write_text("second\n", encoding="utf-8")
        waiter.join(timeout=3.0)

        assert responses
        assert responses[0]["changed"] is True
        assert responses[0]["event_id"] > first["event_id"]
        assert time.time() - started < 1.5
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1.0)
