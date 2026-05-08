import os

import pytest

from analysis.edna_analysis import (
    SAMPLE_COUNTS,
    build_csv_text,
    build_judge_report,
    calculate_frequency_rows,
    format_percent,
    total_seen,
)


def test_sample_counts_match_task_statement_percentages():
    rows = calculate_frequency_rows(SAMPLE_COUNTS)

    assert total_seen(rows) == 84
    assert rows[0].species.common_name == "Snow crab"
    assert rows[0].percent_frequency == pytest.approx(19 / 84 * 100.0)
    assert rows[8].percent_frequency == pytest.approx(12 / 84 * 100.0)
    assert format_percent(rows[0].percent_frequency) == "22.62%"

    report = build_judge_report(rows)
    assert "Total organisms seen: 84" in report
    assert "Snow crab (Chionoecetes opilio)" in report
    assert "22.62%" in report


def test_zero_counts_do_not_divide_by_zero():
    rows = calculate_frequency_rows([0] * 10)

    assert total_seen(rows) == 0
    assert all(row.percent_frequency == 0.0 for row in rows)
    assert "0.00%" in build_csv_text(rows)


def test_frequency_input_validation():
    with pytest.raises(ValueError):
        calculate_frequency_rows([1, 2, 3])

    with pytest.raises(ValueError):
        calculate_frequency_rows([0, 0, 0, 0, 0, -1, 0, 0, 0, 0])


def test_edna_window_updates_sample_counts():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PyQt6")

    from PyQt6.QtWidgets import QApplication

    from analysis.gui.edna_analysis_window import EDNAAnalysisWindow

    app = QApplication.instance()
    if app is None:
        app = QApplication([])

    window = EDNAAnalysisWindow(use_sample=True)
    try:
        app.processEvents()
        assert window._count_spins[0].value() == 19
        assert window._percent_items[0].text() == "22.62%"
        assert "84" in window.total_card.text()

        window._count_spins[0].setValue(20)
        app.processEvents()

        assert window._percent_items[0].text() == format_percent(20 / 85 * 100.0)
        assert "85" in window.total_card.text()
    finally:
        window.close()
        app.processEvents()
