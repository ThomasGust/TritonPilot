import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication, QScrollArea

from gui.style import apply_modern_style


def _app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
        apply_modern_style(app)
    return app


@pytest.mark.parametrize(
    ("window_path", "min_scroll_areas"),
    [
        ("analysis.gui.crab_detection_window.CrabDetectionWindow", 3),
        ("analysis.gui.edna_analysis_window.EDNAAnalysisWindow", 1),
        ("analysis.gui.iceberg_tracking_window.IcebergTrackingWindow", 1),
        ("analysis.gui.coral_garden_model_window.CoralGardenModelWindow", 0),
        ("analysis.gui.iceberg_measurement_window.IcebergMeasurementWindow", 2),
        ("analysis.gui.planar_height_measurement_window.PlanarHeightMeasurementWindow", 2),
        ("analysis.gui.multi_rect_length_measurement_window.MultiRectLengthMeasurementWindow", 2),
        ("analysis.color_corr.MainWindow", 3),
    ],
)
def test_analysis_windows_fit_available_screen(window_path: str, min_scroll_areas: int):
    app = _app()
    module_name, class_name = window_path.rsplit(".", 1)
    module = pytest.importorskip(module_name)
    window_cls = getattr(module, class_name)

    window = window_cls()
    try:
        window.show()
        app.processEvents()

        screen = window.screen() or app.primaryScreen()
        assert screen is not None
        available = screen.availableGeometry()
        assert window.width() <= available.width()
        assert window.height() <= available.height()
        assert len(window.findChildren(QScrollArea)) >= min_scroll_areas
    finally:
        window.close()
        window.deleteLater()
        app.processEvents()


def test_multi_rect_actions_and_anchor_canvases_are_visible():
    app = _app()
    from analysis.gui.multi_rect_length_measurement_window import MultiRectLengthMeasurementWindow

    window = MultiRectLengthMeasurementWindow()
    try:
        window.show()
        app.processEvents()

        for attr_name in (
            "add_measure_btn",
            "remove_measure_btn",
            "undo_btn",
            "delete_btn",
            "clear_btn",
        ):
            button = getattr(window, attr_name)
            top_left = button.mapTo(window, button.rect().topLeft())
            bottom_right = button.mapTo(window, button.rect().bottomRight())
            assert top_left.x() >= 0
            assert top_left.y() >= 0
            assert bottom_right.x() <= window.width()
            assert bottom_right.y() <= window.height()

        sizes = window.setup_splitter.sizes()
        assert len(sizes) == 2
        assert min(sizes) > 0
        assert min(sizes) / max(sizes) > 0.75
    finally:
        window.close()
        window.deleteLater()
        app.processEvents()
