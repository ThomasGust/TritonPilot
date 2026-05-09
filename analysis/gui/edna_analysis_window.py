from __future__ import annotations

import re
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QBrush, QColor, QFont
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QAbstractSpinBox,
    QApplication,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from analysis.edna_analysis import (
    DEFAULT_SPECIES,
    SAMPLE_COUNTS,
    FrequencyRow,
    build_csv_text,
    build_judge_report,
    calculate_frequency_rows,
    format_percent,
    total_seen,
)
from gui.responsive import horizontal_scroll_area, resize_to_available_screen


class JudgeDisplayWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ednaJudgeDisplay")

        self.title_label = QLabel("eDNA Frequency Analysis")
        self.title_label.setObjectName("ednaJudgeTitle")
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title_label.setWordWrap(True)

        self.total_label = QLabel("Total sightings: 0")
        self.total_label.setObjectName("ednaJudgeTotal")
        self.total_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.total_label.setWordWrap(True)

        self.table = QTableWidget(0, 3)
        self.table.setObjectName("ednaJudgeTable")
        self.table.setHorizontalHeaderLabels(["Species", "Number Seen", "% Frequency"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setDefaultSectionSize(50)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)

        self.formula_label = QLabel("Percent frequency = number seen / total seen * 100")
        self.formula_label.setObjectName("ednaJudgeFormula")
        self.formula_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.formula_label.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 22, 22, 22)
        layout.setSpacing(12)
        layout.addWidget(self.title_label)
        layout.addWidget(self.total_label)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.formula_label)

        self._apply_style()
        self.set_results(calculate_frequency_rows([0] * len(DEFAULT_SPECIES)))

    def set_results(self, rows: list[FrequencyRow], precision: int = 2) -> None:
        total = total_seen(rows)
        nonzero_species = sum(1 for row in rows if row.count > 0)
        self.total_label.setText(
            f"Total sightings: {total}    Species observed: {nonzero_species}/{len(rows)}"
        )
        self.table.setRowCount(len(rows))

        top_count = max((row.count for row in rows), default=0)
        for row_index, row in enumerate(rows):
            species_item = self._make_item(row.species.display_name, align=Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
            count_item = self._make_item(str(row.count), align=Qt.AlignmentFlag.AlignCenter)
            percent_item = self._make_item(
                format_percent(row.percent_frequency, precision),
                align=Qt.AlignmentFlag.AlignCenter,
            )

            if total > 0 and row.count == top_count:
                for item in (species_item, count_item, percent_item):
                    item.setBackground(QBrush(QColor("#dff3f4")))
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)

            self.table.setItem(row_index, 0, species_item)
            self.table.setItem(row_index, 1, count_item)
            self.table.setItem(row_index, 2, percent_item)
        self.table.resizeRowsToContents()

    @staticmethod
    def _make_item(text: str, *, align: Qt.AlignmentFlag) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        item.setTextAlignment(align)
        item.setForeground(QBrush(QColor("#14202f")))
        return item

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QWidget#ednaJudgeDisplay {
                background: #f4fafb;
                color: #14202f;
            }
            QLabel#ednaJudgeTitle {
                color: #102233;
                font-size: 30px;
                font-weight: 900;
            }
            QLabel#ednaJudgeTotal {
                color: #1a5961;
                font-size: 18px;
                font-weight: 800;
                padding: 4px 0;
            }
            QLabel#ednaJudgeFormula {
                color: #4d6073;
                font-size: 13px;
                padding: 2px 0;
            }
            QTableWidget#ednaJudgeTable {
                background: #ffffff;
                alternate-background-color: #f0f7f8;
                color: #14202f;
                border: 1px solid #b7d0d4;
                border-radius: 8px;
                gridline-color: #c9dde1;
                font-size: 16px;
            }
            QTableWidget#ednaJudgeTable::item {
                color: #14202f;
                padding: 6px 8px;
            }
            QHeaderView::section {
                background: #18384c;
                color: #ffffff;
                padding: 8px 10px;
                border: none;
                font-weight: 800;
                font-size: 14px;
            }
            """
        )


class JudgeDisplayWindow(QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setWindowTitle("eDNA Judge Display")
        resize_to_available_screen(self, 1180, 780, min_width=760, min_height=560)
        self.display = JudgeDisplayWidget(self)
        self.setCentralWidget(self.display)
        resize_to_available_screen(self, 1180, 780, min_width=760, min_height=560)

    def set_results(self, rows: list[FrequencyRow], precision: int = 2) -> None:
        self.display.set_results(rows, precision)


class EDNAAnalysisWindow(QMainWindow):
    def __init__(self, *, use_sample: bool = False, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setWindowTitle("eDNA Frequency Analysis")
        resize_to_available_screen(self, 1320, 820, min_width=860, min_height=580)

        self._rows: list[FrequencyRow] = calculate_frequency_rows([0] * len(DEFAULT_SPECIES))
        self._judge_window: JudgeDisplayWindow | None = None
        self._count_spins: list[QSpinBox] = []
        self._percent_items: list[QTableWidgetItem] = []
        self._updating_counts = False

        self._build_ui()
        resize_to_available_screen(self, 1320, 820, min_width=860, min_height=580)
        self._apply_local_style()
        if use_sample:
            self._load_sample_counts()
        else:
            self._recalculate()

    def _build_ui(self) -> None:
        container = QWidget(self)
        container.setObjectName("ednaRoot")
        root = QVBoxLayout(container)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(10)

        header = QFrame()
        header.setObjectName("ednaHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 14, 16, 14)
        header_layout.setSpacing(14)

        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        title = QLabel("eDNA Frequency Analysis")
        title.setObjectName("ednaTitle")
        subtitle = QLabel("Enter the judge-provided species counts and show the percent-frequency table.")
        subtitle.setObjectName("ednaSubtitle")
        subtitle.setWordWrap(True)
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header_layout.addLayout(title_box, 1)

        self.total_card = self._make_summary_card("Total sightings", "0")
        self.observed_card = self._make_summary_card("Species observed", "0/10")
        self.top_card = self._make_summary_card("Top species", "-")
        card_row = QHBoxLayout()
        card_row.addWidget(self.total_card)
        card_row.addWidget(self.observed_card)
        card_row.addWidget(self.top_card)
        header_layout.addWidget(horizontal_scroll_area(card_row), 0)
        root.addWidget(header)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_input_panel())
        splitter.addWidget(self._build_preview_panel())
        splitter.setSizes([560, 760])
        root.addWidget(splitter, 1)

        self.setCentralWidget(container)
        self.statusBar().showMessage("Ready for eDNA count entry.")

    def _build_input_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("ednaPanel")
        panel.setMinimumWidth(420)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        panel_title = QLabel("Count Entry")
        panel_title.setObjectName("ednaPanelTitle")
        layout.addWidget(panel_title)

        self.input_table = QTableWidget(len(DEFAULT_SPECIES), 3)
        self.input_table.setObjectName("ednaInputTable")
        self.input_table.setHorizontalHeaderLabels(["Species", "Number Seen", "% Frequency"])
        self.input_table.verticalHeader().setVisible(False)
        self.input_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.input_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.input_table.setAlternatingRowColors(True)
        self.input_table.verticalHeader().setDefaultSectionSize(46)
        input_header = self.input_table.horizontalHeader()
        input_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        input_header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        input_header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)

        for row_index, species in enumerate(DEFAULT_SPECIES):
            species_item = self._readonly_item(species.display_name)
            species_item.setToolTip(species.display_name)
            self.input_table.setItem(row_index, 0, species_item)

            spin = QSpinBox()
            spin.setRange(0, 9999)
            spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
            spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.PlusMinus)
            spin.valueChanged.connect(self._count_changed)
            self._count_spins.append(spin)
            self.input_table.setCellWidget(row_index, 1, spin)

            percent_item = self._readonly_item("0.00%")
            percent_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._percent_items.append(percent_item)
            self.input_table.setItem(row_index, 2, percent_item)

        layout.addWidget(self.input_table, 1)

        precision_row = QHBoxLayout()
        precision_row.addWidget(QLabel("Judge display precision"))
        self.precision_spin = QSpinBox()
        self.precision_spin.setRange(0, 4)
        self.precision_spin.setValue(2)
        self.precision_spin.setSuffix(" decimals")
        self.precision_spin.valueChanged.connect(self._recalculate)
        precision_row.addWidget(self.precision_spin)
        precision_row.addStretch(1)
        layout.addLayout(precision_row)

        button_grid = QGridLayout()
        button_grid.setHorizontalSpacing(8)
        button_grid.setVerticalSpacing(8)

        self.sample_btn = QPushButton("Load Example")
        self.sample_btn.clicked.connect(self._load_sample_counts)
        self.paste_btn = QPushButton("Paste 10 Counts")
        self.paste_btn.clicked.connect(self._paste_counts)
        self.clear_btn = QPushButton("Clear Counts")
        self.clear_btn.clicked.connect(self._clear_counts)
        self.copy_btn = QPushButton("Copy Report")
        self.copy_btn.clicked.connect(self._copy_report)
        self.save_csv_btn = QPushButton("Save CSV")
        self.save_csv_btn.clicked.connect(self._save_csv)
        self.judge_btn = QPushButton("Open Judge Display")
        self.judge_btn.clicked.connect(self._open_judge_display)

        button_grid.addWidget(self.sample_btn, 0, 0)
        button_grid.addWidget(self.paste_btn, 0, 1)
        button_grid.addWidget(self.clear_btn, 1, 0)
        button_grid.addWidget(self.copy_btn, 1, 1)
        button_grid.addWidget(self.save_csv_btn, 2, 0)
        button_grid.addWidget(self.judge_btn, 2, 1)
        layout.addLayout(button_grid)
        return panel

    def _build_preview_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("ednaPreviewPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        label = QLabel("Judge Preview")
        label.setObjectName("ednaPanelTitle")
        layout.addWidget(label)

        self.judge_preview = JudgeDisplayWidget(panel)
        self.judge_preview.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.addWidget(self.judge_preview, 1)
        return panel

    @staticmethod
    def _make_summary_card(label_text: str, value_text: str) -> QLabel:
        label = QLabel(f"{label_text}\n{value_text}")
        label.setObjectName("ednaSummaryCard")
        label.setMinimumWidth(130)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setWordWrap(True)
        return label

    @staticmethod
    def _readonly_item(text: str) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        return item

    def _counts(self) -> list[int]:
        return [spin.value() for spin in self._count_spins]

    def _count_changed(self, *_args) -> None:
        if self._updating_counts:
            return
        self._recalculate()

    def _recalculate(self, *_args) -> None:
        self._rows = calculate_frequency_rows(self._counts())
        precision = self.precision_spin.value()
        total = total_seen(self._rows)
        observed = sum(1 for row in self._rows if row.count > 0)
        top_row = max(self._rows, key=lambda row: row.count) if total else None

        self.total_card.setText(f"Total sightings\n{total}")
        self.observed_card.setText(f"Species observed\n{observed}/{len(self._rows)}")
        if top_row is None:
            self.top_card.setText("Top species\n-")
        else:
            self.top_card.setText(
                "Top species\n"
                f"{top_row.species.common_name} ({format_percent(top_row.percent_frequency, precision)})"
            )

        for item, row in zip(self._percent_items, self._rows):
            item.setText(format_percent(row.percent_frequency, precision))
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if total and row.count == (top_row.count if top_row else -1):
                item.setBackground(QBrush(QColor("#274a55")))
            else:
                item.setBackground(QBrush())

        self.judge_preview.set_results(self._rows, precision)
        if self._judge_window is not None:
            self._judge_window.set_results(self._rows, precision)

    def _set_counts(self, counts: list[int] | tuple[int, ...]) -> None:
        if len(counts) != len(self._count_spins):
            raise ValueError("count list does not match species table")
        self._updating_counts = True
        try:
            for spin, count in zip(self._count_spins, counts):
                spin.setValue(max(0, int(count)))
        finally:
            self._updating_counts = False
        self._recalculate()

    def _load_sample_counts(self) -> None:
        self._set_counts(SAMPLE_COUNTS)
        self.statusBar().showMessage("Loaded the example counts from the task screenshot.", 4000)

    def _clear_counts(self) -> None:
        self._set_counts([0] * len(self._count_spins))
        self.statusBar().showMessage("Cleared eDNA counts.", 3000)

    def _paste_counts(self) -> None:
        text = QApplication.clipboard().text()
        numbers = [int(value) for value in re.findall(r"\d+", text)]
        if len(numbers) < len(self._count_spins):
            QMessageBox.information(
                self,
                "Paste eDNA Counts",
                f"Clipboard needs at least {len(self._count_spins)} whole-number counts.",
            )
            return
        if len(numbers) > len(self._count_spins):
            numbers = numbers[: len(self._count_spins)]
        self._set_counts(numbers)
        self.statusBar().showMessage("Pasted eDNA counts from clipboard.", 4000)

    def _report_text(self) -> str:
        return build_judge_report(self._rows, precision=self.precision_spin.value())

    def _copy_report(self) -> None:
        QApplication.clipboard().setText(self._report_text())
        self.statusBar().showMessage("eDNA judge report copied.", 4000)

    def _save_csv(self) -> None:
        results_dir = Path("results")
        try:
            results_dir.mkdir(exist_ok=True)
        except OSError:
            results_dir = Path.cwd()

        selected_path, _ = QFileDialog.getSaveFileName(
            self,
            "Save eDNA frequency CSV",
            str(results_dir / "edna_frequency.csv"),
            "CSV files (*.csv);;All files (*)",
        )
        if not selected_path:
            return
        path = Path(selected_path)
        try:
            path.write_text(build_csv_text(self._rows, precision=self.precision_spin.value()), encoding="utf-8")
        except OSError as exc:
            QMessageBox.critical(self, "eDNA Analysis", f"Could not save CSV:\n{exc}")
            return
        self.statusBar().showMessage(f"Saved eDNA CSV: {path}", 5000)

    def _open_judge_display(self) -> None:
        if self._judge_window is None:
            self._judge_window = JudgeDisplayWindow(parent=self)
            self._judge_window.destroyed.connect(self._judge_window_closed)
        self._judge_window.set_results(self._rows, self.precision_spin.value())
        self._judge_window.show()
        self._judge_window.raise_()
        self._judge_window.activateWindow()
        self.statusBar().showMessage("Judge display opened.", 3000)

    def _judge_window_closed(self, *_args) -> None:
        self._judge_window = None

    def _apply_local_style(self) -> None:
        self.setStyleSheet(
            """
            QWidget#ednaRoot {
                background: #18181c;
            }
            QFrame#ednaHeader,
            QFrame#ednaPanel,
            QFrame#ednaPreviewPanel {
                background: #1b1d24;
                border: 1px solid #303443;
                border-radius: 8px;
            }
            QLabel#ednaTitle {
                color: #f6f8fb;
                font-size: 24px;
                font-weight: 900;
            }
            QLabel#ednaSubtitle {
                color: #b9c2d2;
                font-size: 13px;
            }
            QLabel#ednaPanelTitle {
                color: #f6f8fb;
                font-size: 15px;
                font-weight: 800;
            }
            QLabel#ednaSummaryCard {
                background: #141821;
                border: 1px solid #2c3343;
                border-radius: 8px;
                padding: 9px 12px;
                color: #dfe8f5;
                font-size: 13px;
                font-weight: 800;
            }
            QTableWidget#ednaInputTable {
                background: #11151d;
                alternate-background-color: #161b25;
                border: 1px solid #2a3140;
                border-radius: 8px;
                gridline-color: #29303f;
            }
            QTableWidget#ednaInputTable::item {
                padding: 5px 7px;
            }
            QSpinBox {
                padding: 5px 8px;
                border: 1px solid #3b465c;
                border-radius: 6px;
                background: #0f131a;
                min-width: 84px;
            }
            QPushButton {
                padding: 8px 10px;
                border: 1px solid #40506b;
                border-radius: 7px;
                background: #26324a;
                font-weight: 800;
            }
            QPushButton:hover {
                background: #314061;
            }
            QPushButton:pressed {
                background: #1f2a40;
            }
            """
        )
