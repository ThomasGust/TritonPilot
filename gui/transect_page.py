"""Square single-camera view for the MATEROV transect task."""

from __future__ import annotations

from PyQt6.QtCore import QRect, Qt, pyqtSignal
from PyQt6.QtWidgets import QComboBox, QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget


class SquareVideoHost(QFrame):
    """Center one child in the largest square that fits this host."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("transectSquareHost")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._widget: QWidget | None = None
        self._placeholder = QLabel("Video unavailable.", self)
        self._placeholder.setObjectName("videoPanePlaceholder")
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._placeholder.setWordWrap(True)

    def current_widget(self) -> QWidget | None:
        return self._widget

    def set_placeholder_text(self, text: str) -> None:
        self._placeholder.setText(str(text or "Video unavailable."))
        self._layout_child()

    def set_widget(self, widget: QWidget | None) -> None:
        if widget is self._widget:
            self._layout_child()
            return
        if self._widget is not None and self._widget.parent() is self:
            self._widget.hide()
            self._widget.setParent(None)
        self._widget = widget
        if widget is None:
            self._placeholder.setParent(self)
            self._placeholder.show()
        else:
            self._placeholder.hide()
            widget.setParent(self)
            widget.show()
            widget.raise_()
        self._layout_child()

    def _square_rect(self) -> QRect:
        rect = self.contentsRect()
        side = max(1, min(rect.width(), rect.height()))
        x = rect.x() + max(0, (rect.width() - side) // 2)
        y = rect.y() + max(0, (rect.height() - side) // 2)
        return QRect(x, y, side, side)

    def _layout_child(self) -> None:
        rect = self._square_rect()
        widget = self._widget
        if widget is not None:
            widget.setGeometry(rect)
            refresher = getattr(widget, "refresh_layout_geometry", None)
            if callable(refresher):
                try:
                    refresher()
                except Exception:
                    pass
        self._placeholder.setGeometry(rect)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._layout_child()


class TransectPage(QWidget):
    """Operator page for a square, single-camera transect view."""

    cameraSelectionChanged = pyqtSignal(str)

    def __init__(self, stream_names: list[str], parent=None):
        super().__init__(parent)
        self.stream_names = list(stream_names)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)

        controls = QWidget()
        controls.setObjectName("transectControls")
        controls_lay = QHBoxLayout(controls)
        controls_lay.setContentsMargins(0, 0, 0, 0)
        controls_lay.setSpacing(6)

        label = QLabel("Camera")
        self.camera_combo = QComboBox()
        self.camera_combo.setObjectName("transectCameraSelector")
        self.camera_combo.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.camera_combo.addItems(self.stream_names)
        self.camera_combo.currentTextChanged.connect(self.cameraSelectionChanged.emit)

        controls_lay.addWidget(label, 0)
        controls_lay.addWidget(self.camera_combo, 0)
        controls_lay.addStretch(1)

        self.square_host = SquareVideoHost()

        outer.addWidget(controls, 0)
        outer.addWidget(self.square_host, 1)

    def current_stream_name(self) -> str | None:
        name = self.camera_combo.currentText().strip()
        if name:
            return name
        return self.stream_names[0] if self.stream_names else None

    def set_current_stream(self, name: str | None, *, emit: bool = False) -> None:
        if name not in self.stream_names:
            return
        prev = self.camera_combo.blockSignals(not emit)
        try:
            self.camera_combo.setCurrentText(str(name))
        finally:
            self.camera_combo.blockSignals(prev)

    def attach_video_panel(self, panel: QWidget) -> None:
        self.square_host.set_widget(panel)

    def detach_video_panel(self, panel: QWidget | None = None) -> None:
        if panel is None or self.square_host.current_widget() is panel:
            self.square_host.set_widget(None)

    def attach_video_placeholder(self, text: str) -> None:
        self.square_host.set_placeholder_text(text)
        self.square_host.set_widget(None)
