import json
from pathlib import Path

from recording.stream_recorder import StreamRecorder


def test_stream_recorder_writes_jsonl(tmp_path: Path):
    out = tmp_path / "streams.jsonl"
    rec = StreamRecorder(out)
    rec.start()
    rec.record("sensor", {"type": "heartbeat", "armed": False})
    rec.record("pilot", {"type": "pilot", "seq": 1})
    rec.stop()

    lines = out.read_text().strip().splitlines()
    assert len(lines) == 2
    a = json.loads(lines[0])
    assert set(a.keys()) == {"t", "stream", "msg"}
