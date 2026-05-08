from __future__ import annotations

from pathlib import Path

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from analysis.coral_garden_model import (
    DEFAULT_CORAL_GARDEN_WIDTH_CM,
    RectangularPrism,
    build_coral_garden_prisms,
    export_obj,
    format_cm,
    model_bounds,
)


class CoralGardenViewport(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._length_cm = 150.0
        self._height_cm = 45.0
        self._width_cm = DEFAULT_CORAL_GARDEN_WIDTH_CM
        self._prisms = build_coral_garden_prisms(
            self._length_cm,
            self._height_cm,
            self._width_cm,
        )
        self._show_dimensions = True
        self._show_grid = True
        self._elev = 22.0
        self._azim = -55.0

        self.figure = Figure(figsize=(8.0, 5.0), constrained_layout=True)
        self.figure.patch.set_facecolor("#f7f8f6")
        self.canvas = FigureCanvas(self.figure)
        self.axes = self.figure.add_subplot(111, projection="3d")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.canvas)
        self.setMinimumSize(640, 460)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self._draw_scene(preserve_view=False)

    def set_dimensions(self, *, length_cm: float, height_cm: float, width_cm: float) -> None:
        self._length_cm = float(length_cm)
        self._height_cm = float(height_cm)
        self._width_cm = float(width_cm)
        self._prisms = build_coral_garden_prisms(
            self._length_cm,
            self._height_cm,
            self._width_cm,
        )
        self._draw_scene()

    def set_show_dimensions(self, show: bool) -> None:
        self._show_dimensions = bool(show)
        self._draw_scene()

    def set_show_grid(self, show: bool) -> None:
        self._show_grid = bool(show)
        self._draw_scene()

    def set_isometric_view(self) -> None:
        self._set_view(22.0, -55.0)

    def set_front_view(self) -> None:
        self._set_view(0.0, -90.0)

    def set_top_view(self) -> None:
        self._set_view(90.0, -90.0)

    def set_side_view(self) -> None:
        self._set_view(0.0, 0.0)

    def fit_view(self) -> None:
        self._draw_scene()

    def save_png(self, path: Path) -> None:
        self.canvas.draw()
        self.figure.savefig(path, dpi=160, facecolor=self.figure.get_facecolor())

    def prisms(self) -> tuple[RectangularPrism, ...]:
        return self._prisms

    def dimensions(self) -> tuple[float, float, float]:
        return self._length_cm, self._height_cm, self._width_cm

    def _set_view(self, elev: float, azim: float) -> None:
        self._elev = float(elev)
        self._azim = float(azim)
        self._draw_scene(preserve_view=False)

    def _draw_scene(self, *, preserve_view: bool = True) -> None:
        if preserve_view:
            self._elev = float(self.axes.elev)
            self._azim = float(self.axes.azim)

        self.axes.clear()
        try:
            self.axes.set_proj_type("ortho")
        except AttributeError:
            pass

        self._draw_prisms()
        if self._show_dimensions:
            self._draw_dimensions()

        self._fit_axes()
        self.axes.view_init(elev=self._elev, azim=self._azim)
        self.axes.set_xlabel("Length (cm)", labelpad=8)
        self.axes.set_ylabel("Width (cm)", labelpad=8)
        self.axes.set_zlabel("Height (cm)", labelpad=8)
        self.axes.grid(self._show_grid)
        self.axes.set_title(
            f"Length {format_cm(self._length_cm)}   Height {format_cm(self._height_cm)}",
            pad=16,
            fontsize=13,
            fontweight="bold",
        )
        self.axes.set_facecolor("#f7f8f6")
        for axis in (self.axes.xaxis, self.axes.yaxis, self.axes.zaxis):
            try:
                axis.pane.set_facecolor((0.94, 0.96, 0.97, 1.0))
                axis.pane.set_edgecolor((0.76, 0.80, 0.86, 1.0))
            except AttributeError:
                pass
        self.canvas.draw_idle()

    def _draw_prisms(self) -> None:
        colors = ["#9fb8cf", "#e0ad45", "#62bda8"]
        for prism, color in zip(self._prisms, colors):
            vertices = prism.vertices()
            faces = [[vertices[index] for index in face] for face in prism.faces()]
            collection = Poly3DCollection(
                faces,
                facecolors=color,
                edgecolors="#111827",
                linewidths=1.2,
                alpha=0.95,
            )
            self.axes.add_collection3d(collection)

    def _draw_dimensions(self) -> None:
        offset = self._dimension_offset()
        length_y = -offset
        height_x = self._length_cm + offset
        height_y = self._width_cm
        center_prism = self._prisms[1]

        self._line((0.0, 0.0, 0.0), (0.0, length_y, 0.0), style="--", width=1.0)
        self._line(
            (self._length_cm, 0.0, 0.0),
            (self._length_cm, length_y, 0.0),
            style="--",
            width=1.0,
        )
        self._line((0.0, length_y, 0.0), (self._length_cm, length_y, 0.0), width=2.0)
        self.axes.text(
            self._length_cm * 0.5,
            length_y,
            -self._height_cm * 0.08,
            f"Length: {format_cm(self._length_cm)}",
            color="#111827",
            fontsize=11,
            fontweight="bold",
            ha="center",
            va="top",
            bbox={"boxstyle": "round,pad=0.35", "fc": "#ffffff", "ec": "#94a3b8", "alpha": 0.92},
        )

        self._line(
            (self._length_cm, self._width_cm, 0.0),
            (height_x, height_y, 0.0),
            style="--",
            width=1.0,
        )
        self._line(
            (center_prism.x_max, self._width_cm, self._height_cm),
            (height_x, height_y, self._height_cm),
            style="--",
            width=1.0,
        )
        self._line((height_x, height_y, 0.0), (height_x, height_y, self._height_cm), width=2.0)
        self.axes.text(
            height_x,
            height_y,
            self._height_cm * 0.5,
            f"Height: {format_cm(self._height_cm)}",
            color="#111827",
            fontsize=11,
            fontweight="bold",
            ha="left",
            va="center",
            bbox={"boxstyle": "round,pad=0.35", "fc": "#ffffff", "ec": "#94a3b8", "alpha": 0.92},
        )

    def _line(
        self,
        start: tuple[float, float, float],
        end: tuple[float, float, float],
        *,
        style: str = "-",
        width: float = 1.4,
    ) -> None:
        self.axes.plot(
            [start[0], end[0]],
            [start[1], end[1]],
            [start[2], end[2]],
            linestyle=style,
            linewidth=width,
            color="#111827",
        )

    def _fit_axes(self) -> None:
        bounds_min, bounds_max = model_bounds(self._prisms)
        x_values = [bounds_min[0], bounds_max[0]]
        y_values = [bounds_min[1], bounds_max[1]]
        z_values = [bounds_min[2], bounds_max[2]]
        if self._show_dimensions:
            for x, y, z in self._dimension_fit_points():
                x_values.append(x)
                y_values.append(y)
                z_values.append(z)

        x_min, x_max = min(x_values), max(x_values)
        y_min, y_max = min(y_values), max(y_values)
        z_min, z_max = min(z_values), max(z_values)
        x_margin = max((x_max - x_min) * 0.04, 4.0)
        y_margin = max((y_max - y_min) * 0.10, 4.0)
        z_margin = max((z_max - z_min) * 0.08, 4.0)

        self.axes.set_xlim(x_min - x_margin, x_max + x_margin)
        self.axes.set_ylim(y_min - y_margin, y_max + y_margin)
        self.axes.set_zlim(max(0.0, z_min - z_margin), z_max + z_margin)

        x_span = max((x_max - x_min) + 2.0 * x_margin, 1.0)
        y_span = max((y_max - y_min) + 2.0 * y_margin, 1.0)
        z_span = max((z_max - z_min) + z_margin, 1.0)
        try:
            self.axes.set_box_aspect((x_span, y_span, z_span))
        except AttributeError:
            pass

    def _dimension_offset(self) -> float:
        return max(self._width_cm * 0.45, self._height_cm * 0.25, self._length_cm * 0.055, 12.0)

    def _dimension_fit_points(self) -> list[tuple[float, float, float]]:
        offset = self._dimension_offset()
        return [
            (0.0, -offset, 0.0),
            (self._length_cm, -offset, 0.0),
            (self._length_cm + offset, self._width_cm, 0.0),
            (self._length_cm + offset, self._width_cm, self._height_cm),
            (self._length_cm * 0.5, -offset, -self._height_cm * 0.08),
        ]


class CoralGardenModelWindow(QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Coral Garden CAD Model")
        self.resize(1180, 720)
        self._presentation_mode = False

        self.viewport = CoralGardenViewport()
        self.controls_panel = self._build_controls()

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.controls_panel)
        splitter.addWidget(self.viewport)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([330, 850])
        self.setCentralWidget(splitter)

        self._apply_window_styles()
        self._update_model()
        self.statusBar().showMessage("Coral garden model ready.")

    def _build_controls(self) -> QWidget:
        panel = QWidget()
        panel.setMinimumWidth(300)
        panel.setMaximumWidth(380)
        layout = QVBoxLayout(panel)

        title = QLabel("Coral Garden Model")
        title.setObjectName("coralModelTitle")
        layout.addWidget(title)

        input_group = QGroupBox("Measurements")
        input_layout = QFormLayout(input_group)

        self.length_spin = self._measurement_spin(1.0, 500.0, 150.0)
        self.height_spin = self._measurement_spin(1.0, 200.0, 45.0)
        self.width_spin = self._measurement_spin(1.0, 100.0, DEFAULT_CORAL_GARDEN_WIDTH_CM)
        input_layout.addRow("Length", self.length_spin)
        input_layout.addRow("Height", self.height_spin)
        input_layout.addRow("Width", self.width_spin)
        layout.addWidget(input_group)

        view_group = QGroupBox("View")
        view_layout = QGridLayout(view_group)
        self.isometric_btn = QPushButton("Isometric")
        self.front_btn = QPushButton("Front")
        self.top_btn = QPushButton("Top")
        self.side_btn = QPushButton("Side")
        self.fit_btn = QPushButton("Fit")
        view_layout.addWidget(self.isometric_btn, 0, 0)
        view_layout.addWidget(self.front_btn, 0, 1)
        view_layout.addWidget(self.top_btn, 1, 0)
        view_layout.addWidget(self.side_btn, 1, 1)
        view_layout.addWidget(self.fit_btn, 2, 0, 1, 2)
        layout.addWidget(view_group)

        display_group = QGroupBox("Display")
        display_layout = QVBoxLayout(display_group)
        self.dimensions_check = QCheckBox("Length and height callouts")
        self.dimensions_check.setChecked(True)
        self.grid_check = QCheckBox("Base grid")
        self.grid_check.setChecked(True)
        display_layout.addWidget(self.dimensions_check)
        display_layout.addWidget(self.grid_check)
        layout.addWidget(display_group)

        export_group = QGroupBox("Output")
        export_layout = QGridLayout(export_group)
        self.save_png_btn = QPushButton("Save PNG")
        self.save_obj_btn = QPushButton("Save OBJ")
        self.presentation_btn = QPushButton("Judge View")
        export_layout.addWidget(self.save_png_btn, 0, 0)
        export_layout.addWidget(self.save_obj_btn, 0, 1)
        export_layout.addWidget(self.presentation_btn, 1, 0, 1, 2)
        layout.addWidget(export_group)

        self.summary_label = QLabel()
        self.summary_label.setObjectName("summaryCard")
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)
        layout.addStretch(1)

        for spin in (self.length_spin, self.height_spin, self.width_spin):
            spin.valueChanged.connect(self._update_model)
        self.isometric_btn.clicked.connect(self.viewport.set_isometric_view)
        self.front_btn.clicked.connect(self.viewport.set_front_view)
        self.top_btn.clicked.connect(self.viewport.set_top_view)
        self.side_btn.clicked.connect(self.viewport.set_side_view)
        self.fit_btn.clicked.connect(self.viewport.fit_view)
        self.dimensions_check.toggled.connect(self.viewport.set_show_dimensions)
        self.grid_check.toggled.connect(self.viewport.set_show_grid)
        self.save_png_btn.clicked.connect(self._save_png)
        self.save_obj_btn.clicked.connect(self._save_obj)
        self.presentation_btn.clicked.connect(self._enter_presentation_mode)
        return panel

    @staticmethod
    def _measurement_spin(minimum: float, maximum: float, default: float) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(minimum, maximum)
        spin.setDecimals(1)
        spin.setSingleStep(1.0)
        spin.setSuffix(" cm")
        spin.setValue(default)
        spin.setAlignment(Qt.AlignmentFlag.AlignRight)
        return spin

    def _update_model(self) -> None:
        length_cm = self.length_spin.value()
        height_cm = self.height_spin.value()
        width_cm = self.width_spin.value()
        self.viewport.set_dimensions(
            length_cm=length_cm,
            height_cm=height_cm,
            width_cm=width_cm,
        )
        self.summary_label.setText(
            "Manual CAD model: "
            f"length {format_cm(length_cm)}, "
            f"height {format_cm(height_cm)}, "
            f"width {format_cm(width_cm)}."
        )

    def _save_png(self) -> None:
        default_path = self._default_output_path("coral_garden_model.png")
        path_text, _selected_filter = QFileDialog.getSaveFileName(
            self,
            "Save Coral Garden Model PNG",
            str(default_path),
            "PNG images (*.png)",
        )
        if not path_text:
            return

        path = Path(path_text)
        if path.suffix.lower() != ".png":
            path = path.with_suffix(".png")
        try:
            self.viewport.save_png(path)
        except OSError as exc:
            QMessageBox.warning(self, "Save PNG", f"Could not save image:\n{exc}")
            return
        self.statusBar().showMessage(f"Saved PNG: {path}", 5000)

    def _save_obj(self) -> None:
        default_path = self._default_output_path("coral_garden_model.obj")
        path_text, _selected_filter = QFileDialog.getSaveFileName(
            self,
            "Save Coral Garden Model OBJ",
            str(default_path),
            "Wavefront OBJ (*.obj)",
        )
        if not path_text:
            return

        path = Path(path_text)
        if path.suffix.lower() != ".obj":
            path = path.with_suffix(".obj")
        length_cm, height_cm, width_cm = self.viewport.dimensions()
        obj_text = export_obj(
            self.viewport.prisms(),
            length_cm=length_cm,
            height_cm=height_cm,
            width_cm=width_cm,
        )
        try:
            path.write_text(obj_text, encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(self, "Save OBJ", f"Could not save OBJ:\n{exc}")
            return
        self.statusBar().showMessage(f"Saved OBJ: {path}", 5000)

    @staticmethod
    def _default_output_path(filename: str) -> Path:
        results_dir = Path.cwd() / "results"
        try:
            results_dir.mkdir(exist_ok=True)
        except OSError:
            results_dir = Path.cwd()
        return results_dir / filename

    def _enter_presentation_mode(self) -> None:
        if self._presentation_mode:
            return
        self._presentation_mode = True
        self.controls_panel.hide()
        self.statusBar().hide()
        self.showFullScreen()

    def _exit_presentation_mode(self) -> None:
        if not self._presentation_mode:
            return
        self._presentation_mode = False
        self.controls_panel.show()
        self.statusBar().show()
        self.showNormal()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape and self._presentation_mode:
            self._exit_presentation_mode()
            event.accept()
            return
        super().keyPressEvent(event)

    def _apply_window_styles(self) -> None:
        self.setStyleSheet(
            """
            QLabel#coralModelTitle {
                font-size: 18px;
                font-weight: 700;
                padding: 4px 2px 8px 2px;
            }
            QGroupBox {
                border: 1px solid #333542;
                border-radius: 8px;
                margin-top: 10px;
                padding: 10px 8px 8px 8px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 4px;
                color: #d7dbe8;
                font-weight: 700;
            }
            QPushButton {
                min-height: 28px;
                border: 1px solid #3a3d4c;
                border-radius: 6px;
                padding: 4px 9px;
                background: #242633;
            }
            QPushButton:hover {
                background: #303447;
            }
            QDoubleSpinBox {
                padding: 4px 6px;
                border: 1px solid #343442;
                border-radius: 6px;
                background: #15161d;
            }
            """
        )
