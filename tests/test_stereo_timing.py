import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6")

from gui import stereo_page as stereo_module
from gui.stereo_page import _CaptureWorker


def test_capture_worker_uses_absolute_interval_deadlines(monkeypatch, tmp_path):
    worker = _CaptureWorker(
        manager=object(),
        pair=SimpleNamespace(name="pair"),
        output_root=tmp_path,
        session_name=None,
        count=None,
        interval_s=0.5,
        wait_s=0.0,
        continuous=True,
    )

    monkeypatch.setattr(stereo_module.time, "monotonic", lambda: 10.2)
    assert worker._advance_deadline(10.0) == pytest.approx(10.5)

    monkeypatch.setattr(stereo_module.time, "monotonic", lambda: 10.8)
    assert worker._advance_deadline(10.0) == pytest.approx(10.8)
