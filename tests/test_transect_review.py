import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6")
pytest.importorskip("matplotlib")

from tools.transect_review import (
    _blue_width_percent_sample,
    _format_blue_width_percent,
    estimate_from_visual,
)
from tracking.transect_policy import TransectModel, TransectObservation, TransectPolicy


def test_blue_width_metric_prefers_detected_observation():
    model = TransectModel()
    estimate = estimate_from_visual({"valid": True, "es": -0.55, "confidence": 0.9}, model)
    observation = TransectObservation(blue_found=True, blue_fraction=0.446, fit_quality=0.9)

    pct, source = _blue_width_percent_sample(model, estimate, observation)
    assert pct == pytest.approx(44.6)
    assert source == "detected"
    assert _format_blue_width_percent(model, estimate, observation) == "blue width 44.6% frame"


def test_blue_width_metric_estimates_from_recorded_es():
    model = TransectModel()
    estimate = estimate_from_visual({"valid": True, "es": -0.55, "confidence": 0.9}, model)

    pct, source = _blue_width_percent_sample(model, estimate, None)

    assert source == "estimated"
    assert pct == pytest.approx(100.0 * 50.0 / 112.0, abs=0.05)
    assert _format_blue_width_percent(model, estimate, None) == "blue width ~44.6% frame from es"


def test_blue_width_metric_uses_policy_footprint_when_available():
    model = TransectModel()
    policy = TransectPolicy(model)
    obs = TransectObservation(
        blue_found=True,
        blue_cx=model.target_cx,
        blue_cy=model.target_cy,
        blue_fraction=0.50,
        fit_quality=1.0,
    )
    estimate = None
    for _ in range(model.min_lock_frames + 2):
        estimate = policy.evaluate(obs)

    pct, source = _blue_width_percent_sample(model, estimate, None)

    assert source == "footprint"
    assert pct == pytest.approx(50.0)
