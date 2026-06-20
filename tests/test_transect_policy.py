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
    assert est.error.er == pytest.approx(0.0, abs=1e-6)
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


def test_rotation_drives_er_and_squares_up_at_zero():
    m = TransectModel(rot_norm_deg=45.0)
    p = TransectPolicy(m)
    # A 22.5deg-rotated square -> er ~ +0.5; squared-on -> er ~ 0.
    rot = _lock(p, _on_station(m, blue_rotation_deg=22.5), n=15)
    assert rot.error.er == pytest.approx(0.5, abs=0.05)
    p.reset()
    full = _lock(p, _on_station(m, blue_rotation_deg=-45.0), n=15)
    assert full.error.er == pytest.approx(-1.0, abs=0.05)
    p.reset()
    square = _lock(p, _on_station(m, blue_rotation_deg=0.0), n=15)
    assert square.error.er == pytest.approx(0.0, abs=1e-6)


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


# -- robustness: coast-hold + outlier reject --------------------------------
def test_coast_hold_bridges_brief_dropout_then_drops():
    m = TransectModel(min_lock_frames=3, coast_frames=3, lock_drop_frames=5)
    p = TransectPolicy(m)
    _lock(p, _on_station(m))                       # establish a solid lock
    valids = [p.evaluate(TransectObservation.no_target(ts=float(i))).error.valid
              for i in range(6)]
    # First coast_frames dropouts keep a valid error (coasting), then it gives up.
    assert valids[:3] == [True, True, True]
    assert valids[3] is False
    # During the coast the error mirrors the last good one (centered -> ~zero).
    p2 = TransectPolicy(m)
    _lock(p2, _on_station(m, blue_cx=m.target_cx + 0.5 * m.image_pos_tol), n=15)
    coasted = p2.evaluate(TransectObservation.no_target())
    assert coasted.error.valid is True
    assert "coasting" in coasted.reasons
    assert coasted.error.ex == pytest.approx(0.5, abs=0.1)   # held, not zeroed


def test_coast_budget_refreshes_after_a_real_detection():
    m = TransectModel(min_lock_frames=3, coast_frames=2, lock_drop_frames=8)
    p = TransectPolicy(m)
    _lock(p, _on_station(m))
    p.evaluate(TransectObservation.no_target())     # coast 1
    p.evaluate(_on_station(m))                       # real detection -> refresh budget
    assert p.evaluate(TransectObservation.no_target()).error.valid is True  # coasts again


def test_centroid_jump_is_rejected_and_does_not_fling_error():
    m = TransectModel(centroid_jump_reject=0.2)
    p = TransectPolicy(m)
    _lock(p, _on_station(m), n=15)                   # settled at center (ex ~ 0)
    # One frame the detector latches a blob far to the right (cx jump 0.45 >> 0.2).
    spike = p.evaluate(_on_station(m, blue_cx=0.95))
    assert "centroid_jump" in spike.reasons
    assert spike.error.ex == pytest.approx(0.0, abs=0.1)     # held, not flung to +1
    assert spike.error.valid is True                          # single outlier keeps lock


def test_sustained_centroid_shift_is_eventually_accepted():
    m = TransectModel(centroid_jump_reject=0.2)
    p = TransectPolicy(m)
    _lock(p, _on_station(m), n=15)
    moved = _on_station(m, blue_cx=0.95)
    exs = [p.evaluate(moved).error.ex for _ in range(6)]
    # First frames hold (reject), then it gives up and follows the target out to +1.
    assert exs[0] == pytest.approx(0.0, abs=0.1)
    assert exs[-1] == pytest.approx(1.0, abs=0.1)


def test_er_tracks_a_steady_low_variance_rotation():
    # A consistent (low-variance) rotation is reliable -> drives a square-up error near
    # its circular mean; the reliability gate stays open.
    m = TransectModel()
    p = TransectPolicy(m)
    seq = [18.0, 22.0, 16.0, 24.0, 20.0, 19.0, 21.0, 20.0, 18.0, 22.0, 20.0, 19.0]
    est = None
    for r in seq:
        est = p.evaluate(_on_station(m, blue_rotation_deg=r))
    assert est.error.er == pytest.approx(20.0 / 45.0, abs=0.12)
    assert "rot_unreliable" not in est.reasons


def test_er_collapses_when_rotation_reads_as_noise():
    # The pool failure mode: a 90deg-symmetric square's measured rotation wanders
    # ~uniformly across +/-45deg even when the square is squared up. The reliability
    # gate must SUPPRESS er (hold yaw) instead of chasing the noise -- chasing it is
    # what rocked the vehicle back and forth.
    m = TransectModel()
    p = TransectPolicy(m)
    noisy = [40.0, -38.0, 30.0, -44.0, 12.0, -25.0, 44.0, -33.0, 5.0, -41.0, 28.0, -36.0]
    est = None
    for r in noisy:
        est = p.evaluate(_on_station(m, blue_rotation_deg=r))
    assert abs(est.error.er) < 0.15
    assert "rot_unreliable" in est.reasons


def test_low_rotation_reliability_suppresses_er():
    # The detector reports a per-frame rotation_reliability (e.g. its structure tensor
    # could not concentrate on an orientation). A steady-but-untrusted rotation must
    # NOT drive yaw: the reliability-weighted window collapses the concentration so the
    # gate holds. Trusted, the same rotation drives a square-up error.
    m = TransectModel()
    untrusted = _lock(
        TransectPolicy(m),
        _on_station(m, blue_rotation_deg=30.0, rotation_reliability=0.0), n=15)
    assert abs(untrusted.error.er) < 0.15
    trusted = _lock(
        TransectPolicy(m),
        _on_station(m, blue_rotation_deg=30.0, rotation_reliability=1.0), n=15)
    assert trusted.error.er > 0.4


def test_er_does_not_flip_sign_across_the_symmetry_wrap():
    # +44deg and -44deg are the SAME orientation (the square is 90deg-symmetric). A
    # single -44 after a steady +44 must NOT snap er from +1 to -1 -- that sign-flip at
    # the wrap was a source of the back-and-forth yaw command. The 90deg-periodic mean
    # keeps er near +44/45.
    m = TransectModel()
    p = TransectPolicy(m)
    _lock(p, _on_station(m, blue_rotation_deg=44.0), n=10)
    after = p.evaluate(_on_station(m, blue_rotation_deg=-44.0))
    assert after.error.er > 0.6   # stayed near +1, did not flip negative


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
    for k in ("ex", "ey", "es", "er", "violation", "confidence", "ts"):
        assert k in payload
        assert math.isfinite(payload[k])
    assert 0.0 <= payload["violation"] <= 1.0
