"""Tests for the transect station-keeping policy/model (geometry -> error)."""

import math

import pytest

from tracking.transect_policy import (
    TransectEstimate,
    TransectModel,
    TransectObservation,
    TransectPolicy,
)


def _on_station(model: TransectModel, **over) -> TransectObservation:
    """A clean, perfectly-centered observation at the target footprint."""
    base = dict(
        blue_found=True,
        blue_cx=model.target_cx,
        blue_cy=model.target_cy,
        blue_fraction=model.nominal_blue_fraction,
        fit_quality=1.0,
    )
    base.update(over)
    return TransectObservation(**base)


def _lock(policy: TransectPolicy, obs: TransectObservation, n: int = 5) -> TransectEstimate:
    est = None
    for i in range(n):
        est = policy.evaluate(TransectObservation(**{**obs.__dict__, "ts": float(i)}))
    return est


# -- geometry ---------------------------------------------------------------
def test_derived_setpoints_match_inscribed_square_geometry():
    m = TransectModel(blue_cm=50.0, red_cm=130.0)
    assert m.footprint_target_cm == pytest.approx(90.0)   # (50+130)/2
    assert m.position_tol_cm == pytest.approx(20.0)       # (130-50)/4
    assert m.size_tol_cm == pytest.approx(40.0)           # (130-50)/2
    assert m.nominal_blue_fraction == pytest.approx(50.0 / 90.0)
    assert m.image_pos_tol == pytest.approx(20.0 / 90.0)


def test_on_station_is_zero_error_once_locked():
    m = TransectModel()
    p = TransectPolicy(m)
    est = _lock(p, _on_station(m))
    assert est.lock_state == "lock"
    assert est.error.valid is True
    assert est.error.ex == pytest.approx(0.0, abs=1e-6)
    assert est.error.ey == pytest.approx(0.0, abs=1e-6)
    assert est.error.es == pytest.approx(0.0, abs=1e-3)
    assert est.violation == 0.0
    assert est.clean is True
    assert est.margin_cm == pytest.approx(20.0, abs=0.5)   # full margin at center


def test_position_error_hits_unity_at_geometric_tolerance():
    m = TransectModel()
    p = TransectPolicy(m)
    # Offset the centroid by exactly the position tolerance (in frame fraction).
    obs = _on_station(m, blue_cx=m.target_cx + m.image_pos_tol)
    # Settle the EMA so the smoothed value reaches the offset, then read.
    est = _lock(p, obs, n=15)
    assert est.error.ex == pytest.approx(1.0, abs=0.05)
    assert est.error.ey == pytest.approx(0.0, abs=1e-6)
    # 20 cm offset at the sweet spot => zero margin.
    assert est.margin_cm == pytest.approx(0.0, abs=1.0)


def test_es_sign_and_unity_bounds():
    m = TransectModel()
    p = TransectPolicy(m)
    # Blue fills the frame (fraction 1.0) => footprint 50 cm => too close => es=+1.
    too_close = _lock(p, _on_station(m, blue_fraction=1.0), n=15)
    assert too_close.error.es == pytest.approx(1.0, abs=0.05)
    assert too_close.footprint_cm == pytest.approx(50.0, abs=1.0)

    p.reset()
    # Footprint 130 cm (red at the frame edge) => too far => es=-1.
    too_far = _lock(p, _on_station(m, blue_fraction=50.0 / 130.0), n=15)
    assert too_far.error.es == pytest.approx(-1.0, abs=0.05)


# -- lock / confidence -------------------------------------------------------
def test_no_target_reports_no_lock_and_holds():
    p = TransectPolicy()
    est = p.evaluate(TransectObservation.no_target())
    assert est.lock_state == "no_target"
    assert est.error.valid is False
    assert est.error.to_visual_payload()["valid"] is False


def test_lock_requires_consecutive_high_confidence_frames():
    m = TransectModel(min_lock_frames=3)
    p = TransectPolicy(m)
    obs = _on_station(m)
    assert p.evaluate(obs).lock_state == "acquiring"
    assert p.evaluate(obs).lock_state == "acquiring"
    assert p.evaluate(obs).lock_state == "lock"


def test_lock_drops_after_sustained_low_confidence():
    m = TransectModel(lock_drop_frames=3)
    p = TransectPolicy(m)
    _lock(p, _on_station(m))
    # Implausible size kills confidence; lock should drop after the grace window.
    bad = _on_station(m, blue_fraction=0.05)
    states = [p.evaluate(bad).lock_state for _ in range(4)]
    assert states[-1] == "lost" or not p._locked


def test_size_implausible_zeroes_confidence():
    m = TransectModel()
    p = TransectPolicy(m)
    # Blue apparently larger than max_blue_fraction -> not a believable detection.
    big = p.evaluate(_on_station(m, blue_fraction=0.99))
    assert big.confidence == 0.0
    assert "size_implausible" in big.reasons


def test_occlusion_lowers_confidence():
    m = TransectModel()
    p = TransectPolicy(m)
    clear = p.evaluate(_on_station(m)).confidence
    p.reset()
    occ = p.evaluate(_on_station(m, occlusion=0.5)).confidence
    assert occ < clear


def test_near_edge_lowers_confidence():
    m = TransectModel()
    p = TransectPolicy(m)
    center = p.evaluate(_on_station(m)).confidence
    p.reset()
    edge = p.evaluate(_on_station(m, blue_cx=0.01)).confidence
    assert edge < center
    assert "near_edge" in p.evaluate(_on_station(m, blue_cx=0.01)).reasons


# -- violation / red ---------------------------------------------------------
def test_violation_is_worst_edge_and_breaks_clean():
    m = TransectModel()
    p = TransectPolicy(m)
    est = _lock(p, _on_station(m, red_right=0.4, red_top=0.1))
    assert est.violation == pytest.approx(0.4)
    assert est.clean is False
    assert "red_visible" in est.reasons


def test_red_bias_off_by_default_does_not_move_target():
    m = TransectModel()
    p = TransectPolicy(m)
    est = _lock(p, _on_station(m, red_right=0.5))
    assert est.target_center == pytest.approx((m.target_cx, m.target_cy))
    assert est.error.ex == pytest.approx(0.0, abs=1e-6)


def test_red_bias_retreats_from_red_when_enabled():
    m = TransectModel(red_bias_gain=0.3)
    p = TransectPolicy(m)
    est = _lock(p, _on_station(m, red_right=0.5))
    # Red on the right pushes the demand the same way as "blue is left" (move away).
    assert est.error.ex < 0.0


# -- oblique-camera setpoint -------------------------------------------------
def test_calibrated_setpoint_makes_offcenter_on_station():
    # Oblique cam: on-station blue rides high (cy=0.4) and a bit smaller.
    m = TransectModel(target_cx=0.5, target_cy=0.4, target_blue_fraction=0.45)
    p = TransectPolicy(m)
    est = _lock(p, _on_station(m), n=15)
    assert est.error.ex == pytest.approx(0.0, abs=1e-6)
    assert est.error.ey == pytest.approx(0.0, abs=1e-6)
    assert est.error.es == pytest.approx(0.0, abs=1e-3)


def test_reset_clears_lock_and_smoothing():
    m = TransectModel()
    p = TransectPolicy(m)
    _lock(p, _on_station(m))
    assert p._locked is True
    p.reset()
    assert p._locked is False
    assert p._cx is None
    assert p.evaluate(TransectObservation.no_target()).lock_state == "no_target"


def test_payload_round_trips_to_controller_schema():
    m = TransectModel()
    p = TransectPolicy(m)
    est = _lock(p, _on_station(m, blue_cx=m.target_cx + 0.05, red_top=0.2))
    payload = est.error.to_visual_payload()
    assert payload["valid"] is True
    for k in ("ex", "ey", "es", "violation", "confidence", "ts"):
        assert k in payload
        assert math.isfinite(payload[k])
    assert 0.0 <= payload["violation"] <= 1.0
