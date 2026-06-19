"""Headless smoke tests for the transect overlay view widget.

Paints are driven via ``app.processEvents()`` (and torn down the same way),
matching the proven offscreen pattern in ``test_main_window_reverse_drive`` --
calling ``repaint()`` directly under the pytest host trips an offscreen-Qt
teardown crash. The actual conversion/paint correctness is asserted on the
QImage state here and exercised end to end by the demo tool.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QWidget

from gui.transect_overlay_view import TransectHudOverlayView, TransectOverlayView
from tracking.transect_policy import TransectModel, TransectObservation, TransectPolicy


def _app():
    return QApplication.instance() or QApplication([])


def test_view_starts_with_placeholder_and_no_image():
    app = _app()
    view = TransectOverlayView()
    try:
        view.resize(320, 240)
        view.show()
        app.processEvents()  # placeholder paint path must not raise
        assert view._qimage is None
    finally:
        view.hide()
        view.deleteLater()
        app.processEvents()


def test_submitting_a_frame_builds_a_qimage_and_paints():
    app = _app()
    view = TransectOverlayView()
    try:
        view.resize(200, 200)
        view.show()
        frame = np.zeros((48, 64, 3), np.uint8)
        frame[:, :, 2] = 255  # red in BGR
        view._on_frame(frame)  # call slot directly (no cross-thread timing)
        app.processEvents()    # image paint path
        assert view._qimage is not None and not view._qimage.isNull()
        assert view._qimage.width() == 64 and view._qimage.height() == 48
        # BGR red -> RGB red at pixel (0,0).
        px = view._qimage.pixelColor(0, 0)
        assert (px.red(), px.green(), px.blue()) == (255, 0, 0)
    finally:
        view.hide()
        view.deleteLater()
        app.processEvents()


def test_clear_and_bad_frame_are_safe():
    app = _app()
    view = TransectOverlayView()
    try:
        view._on_frame(np.zeros((10, 10, 3), np.uint8))
        view.clear()
        assert view._qimage is None
        view._on_frame(None)                        # ignored
        view._on_frame(np.zeros((5, 5), np.uint8))  # wrong ndim -> ignored
        assert view._qimage is None
    finally:
        view.deleteLater()
        app.processEvents()


def test_hud_overlay_accepts_estimates_and_clears():
    app = _app()
    view = TransectHudOverlayView()
    try:
        view.resize(320, 320)
        model = TransectModel()
        obs = TransectObservation(
            blue_found=True,
            blue_cx=model.target_cx,
            blue_cy=model.target_cy,
            blue_fraction=model.nominal_blue_fraction,
            fit_quality=0.95,
        )
        est = TransectPolicy(model).evaluate(obs)

        view._on_estimate(model, est, obs, (1080, 1920, 3))
        view.show()
        app.processEvents()

        assert view._estimate is est
        assert view._observation is obs
        assert view._source_shape == (1080, 1920)

        view.clear()
        assert view._estimate is None
        assert not view.isVisible()
    finally:
        view.deleteLater()
        app.processEvents()


def test_hud_overlay_hides_and_stays_hidden_when_app_inactive(monkeypatch):
    app = _app()
    anchor = QWidget()
    view = TransectHudOverlayView()
    try:
        anchor.resize(360, 360)
        anchor.show()
        app.processEvents()
        view.set_anchor_widget(anchor)

        monkeypatch.setattr(TransectHudOverlayView, "_application_is_active", staticmethod(lambda: True))
        view.show_for_anchor()
        app.processEvents()
        assert view.isVisible()

        view._on_application_state_changed(Qt.ApplicationState.ApplicationInactive)
        app.processEvents()
        assert not view.isVisible()

        monkeypatch.setattr(TransectHudOverlayView, "_application_is_active", staticmethod(lambda: False))
        model = TransectModel()
        obs = TransectObservation(
            blue_found=True,
            blue_cx=model.target_cx,
            blue_cy=model.target_cy,
            blue_fraction=model.nominal_blue_fraction,
            fit_quality=0.95,
        )
        est = TransectPolicy(model).evaluate(obs)
        view._on_estimate(model, est, obs, (1080, 1920, 3))
        app.processEvents()
        assert not view.isVisible()

        monkeypatch.setattr(TransectHudOverlayView, "_application_is_active", staticmethod(lambda: True))
        view._on_application_state_changed(Qt.ApplicationState.ApplicationActive)
        app.processEvents()
        assert view.isVisible()
    finally:
        view.deleteLater()
        anchor.deleteLater()
        app.processEvents()
