from __future__ import annotations

from PyQt6.QtCore import QSettings, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from gui.video_widget import VideoWidget
from video.cam import RemoteCameraManager


class _VideoPane(QFrame):
    activated = pyqtSignal(int)

    def __init__(self, index: int, parent=None):
        super().__init__(parent)
        self.index = int(index)
        self.setObjectName("videoPane")
        self.setProperty("active", False)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Click to make this the active pane for B/X, snapshots, and recording.")

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.activated.emit(self.index)
        super().mousePressEvent(event)

    def set_active(self, active: bool) -> None:
        self.setProperty("active", bool(active))
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def attach_widget(self, widget: QWidget | None, placeholder: str) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            child = item.widget()
            if child is not None:
                child.setParent(None)
                if child.objectName() == "videoPanePlaceholder":
                    child.deleteLater()

        if widget is not None:
            self._layout.addWidget(widget)
            return

        lbl = QLabel(placeholder)
        lbl.setObjectName("videoPanePlaceholder")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setWordWrap(True)
        self._layout.addWidget(lbl)


class VideoTabs(QWidget):
    selectionChanged = pyqtSignal()
    LAYOUT_OPTIONS: tuple[tuple[str, int], ...] = (
        ("Single", 1),
        ("Stacked", 2),
        ("Quad", 4),
    )

    def __init__(self, manager: RemoteCameraManager, stream_names: list[str], parent=None):
        super().__init__(parent)
        self.manager = manager
        self.stream_names = list(stream_names)
        self._water_correction_enabled: bool = False
        self._settings = QSettings("TritonPilot", "ROVTopside")

        self._containers: dict[str, QWidget] = {}
        self._widgets: dict[str, VideoWidget | None] = {}
        self._pane_streams: list[str | None] = [None, None, None, None]
        self._pane_count: int = 1
        self._active_pane_index: int = 0
        self._warmup_index: int = 0
        self._warmup_timer = QTimer(self)
        self._warmup_timer.setSingleShot(True)
        self._warmup_timer.timeout.connect(self._warmup_next)

        self._layout_combo = QComboBox()
        self._layout_combo.setObjectName("videoLayoutCombo")
        for label, count in self.LAYOUT_OPTIONS:
            self._layout_combo.addItem(label, count)
        self._layout_combo.currentIndexChanged.connect(self._on_layout_changed)

        controls = QWidget()
        controls.setObjectName("videoLayoutBar")
        controls_lay = QHBoxLayout(controls)
        controls_lay.setContentsMargins(4, 2, 4, 2)
        controls_lay.setSpacing(6)
        controls_lay.addWidget(QLabel("View"), 0)
        controls_lay.addWidget(self._layout_combo, 0)

        self._pane_controls: list[QWidget] = []
        self._pane_control_labels: list[QLabel] = []
        self._pane_selectors: list[QComboBox] = []
        for idx in range(4):
            group = QWidget()
            group.setObjectName("videoControlGroup")
            group_lay = QHBoxLayout(group)
            group_lay.setContentsMargins(0, 0, 0, 0)
            group_lay.setSpacing(4)

            badge = QLabel(str(idx + 1))
            badge.setObjectName("videoControlLabel")
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)

            selector = QComboBox()
            selector.setObjectName("videoPaneSelector")
            selector.addItems(self.stream_names)
            selector.currentTextChanged.connect(lambda name, pane_index=idx: self._on_pane_stream_changed(pane_index, name))

            group_lay.addWidget(badge, 0)
            group_lay.addWidget(selector, 1)

            self._pane_controls.append(group)
            self._pane_control_labels.append(badge)
            self._pane_selectors.append(selector)
            controls_lay.addWidget(group, 1)

        controls_lay.addStretch(1)

        self._panes: list[_VideoPane] = []
        self._grid = QGridLayout()
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(2)

        for idx in range(4):
            pane = _VideoPane(idx)
            pane.activated.connect(self._on_pane_activated)
            self._panes.append(pane)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)
        outer.addWidget(controls, 0)
        outer.addLayout(self._grid, 1)

        for name in self.stream_names:
            cont = QWidget()
            lay = QVBoxLayout(cont)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(0)
            placeholder = QLabel(f"{name}\n(starting when needed)")
            placeholder.setObjectName("videoPanePlaceholder")
            placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            placeholder.setWordWrap(True)
            lay.addWidget(placeholder)
            self._containers[name] = cont
            self._widgets[name] = None

        self._load_preferences()
        self._refresh_layout(save=False, emit=False)

        for name in self.visible_stream_names():
            self._ensure_stream_started(name)

        if self.stream_names:
            self._warmup_timer.start(700)

    def _allowed_layout_count(self, requested: int | None) -> int:
        if requested == 4:
            return 4
        if requested == 2:
            return 2
        return 1

    def _visible_pane_count(self) -> int:
        if not self.stream_names:
            return 0
        return min(len(self.stream_names), self._allowed_layout_count(self._pane_count))

    def _find_layout_combo_index(self, count: int) -> int:
        for idx in range(self._layout_combo.count()):
            if int(self._layout_combo.itemData(idx)) == int(count):
                return idx
        return 0

    def _load_preferences(self) -> None:
        saved_count = self._settings.value("video/layout_count", 1)
        try:
            self._pane_count = self._allowed_layout_count(int(saved_count))
        except Exception:
            self._pane_count = 1

        for idx in range(len(self._pane_streams)):
            raw = self._settings.value(f"video/pane_stream_{idx}", None)
            name = str(raw).strip() if raw is not None else ""
            self._pane_streams[idx] = name if name in self.stream_names else None

        try:
            self._active_pane_index = max(0, min(3, int(self._settings.value("video/active_pane", 0))))
        except Exception:
            self._active_pane_index = 0

    def _save_preferences(self) -> None:
        try:
            self._settings.setValue("video/layout_count", int(self._pane_count))
            self._settings.setValue("video/active_pane", int(self._active_pane_index))
            for idx, name in enumerate(self._pane_streams):
                self._settings.setValue(f"video/pane_stream_{idx}", name or "")
        except Exception:
            pass

    def _normalized_assignments(self) -> list[str | None]:
        visible_count = self._visible_pane_count()
        used: set[str] = set()
        normalized: list[str | None] = [None, None, None, None]

        for idx in range(visible_count):
            name = self._pane_streams[idx]
            if name in self.stream_names and name not in used:
                normalized[idx] = name
                used.add(name)

        next_names = [name for name in self.stream_names if name not in used]
        for idx in range(visible_count):
            if normalized[idx] is None and next_names:
                normalized[idx] = next_names.pop(0)

        for idx in range(visible_count, len(normalized)):
            normalized[idx] = self._pane_streams[idx]

        return normalized

    def _rebuild_grid(self, visible_count: int) -> None:
        while self._grid.count():
            self._grid.takeAt(0)

        positions = {
            1: [(0, 0)],
            2: [(0, 0), (1, 0)],
            4: [(0, 0), (0, 1), (1, 0), (1, 1)],
        }.get(visible_count, [])

        for idx in range(4):
            self._panes[idx].setVisible(idx < visible_count)

        for row in range(2):
            self._grid.setRowStretch(row, 0)
        for col in range(2):
            self._grid.setColumnStretch(col, 0)

        for idx, (row, col) in enumerate(positions):
            self._grid.addWidget(self._panes[idx], row, col)
            self._grid.setRowStretch(row, 1)
            self._grid.setColumnStretch(col, 1)

    def _refresh_layout(self, *, save: bool, emit: bool) -> None:
        self._pane_streams = self._normalized_assignments()
        visible_count = self._visible_pane_count()
        if visible_count <= 0:
            self._active_pane_index = 0
        else:
            self._active_pane_index = max(0, min(self._active_pane_index, visible_count - 1))

        combo_prev = self._layout_combo.blockSignals(True)
        try:
            self._layout_combo.setCurrentIndex(self._find_layout_combo_index(self._pane_count))
        finally:
            self._layout_combo.blockSignals(combo_prev)

        self._rebuild_grid(visible_count)

        for idx, pane in enumerate(self._panes):
            if idx >= visible_count:
                pane.attach_widget(None, "Pane hidden")
                continue

            name = self._pane_streams[idx]
            selector = self._pane_selectors[idx]
            label = self._pane_control_labels[idx]
            control = self._pane_controls[idx]

            control.setVisible(True)
            prev = selector.blockSignals(True)
            try:
                selector.setCurrentText(name or self.stream_names[0])
            finally:
                selector.blockSignals(prev)

            pane.set_active(idx == self._active_pane_index)
            label.setText(f"[{idx + 1}]" if idx == self._active_pane_index else str(idx + 1))
            label.setProperty("active", idx == self._active_pane_index)
            label.style().unpolish(label)
            label.style().polish(label)
            label.update()
            selector.setProperty("active", idx == self._active_pane_index)
            selector.style().unpolish(selector)
            selector.style().polish(selector)
            selector.update()

            pane.attach_widget(
                self._containers.get(name) if name else None,
                f"{name or 'Camera'}\n(starting when needed)",
            )

        for idx in range(visible_count, len(self._pane_controls)):
            self._pane_controls[idx].setVisible(False)
            self._panes[idx].set_active(False)

        if save:
            self._save_preferences()
        if emit:
            self.selectionChanged.emit()

    def _clear_container(self, cont: QWidget) -> None:
        lay = cont.layout()
        if lay is None:
            return
        while lay.count():
            item = lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                if not isinstance(w, VideoWidget):
                    w.deleteLater()

    def _ensure_stream_started(self, name: str) -> None:
        cont = self._containers.get(name)
        if cont is None:
            return
        existing = self._widgets.get(name)
        if existing is not None:
            return

        self._clear_container(cont)
        try:
            vw = VideoWidget(self.manager, stream_name=name)
        except Exception as e:
            lay = cont.layout()
            if lay is not None:
                lbl = QLabel(f"Failed to start stream '{name}':\n{e}")
                lbl.setObjectName("videoPanePlaceholder")
                lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                lbl.setWordWrap(True)
                lay.addWidget(lbl)
            self._widgets[name] = None
            return

        lay = cont.layout()
        if lay is not None:
            lay.addWidget(vw)
        self._widgets[name] = vw
        if self._water_correction_enabled:
            vw.set_water_correction(True)

    def _warmup_next(self) -> None:
        if not self.stream_names:
            return
        for _ in range(len(self.stream_names)):
            idx = self._warmup_index % len(self.stream_names)
            self._warmup_index += 1
            name = self.stream_names[idx]
            if self._widgets.get(name) is None:
                self._ensure_stream_started(name)
                self._warmup_timer.start(700)
                return

    def _assign_stream_to_pane(self, pane_index: int, name: str, *, save: bool, emit: bool) -> bool:
        if name not in self.stream_names:
            return False
        visible_count = self._visible_pane_count()
        if pane_index < 0 or pane_index >= visible_count:
            return False

        current = self._pane_streams[pane_index]
        other_idx = None
        for idx in range(visible_count):
            if idx != pane_index and self._pane_streams[idx] == name:
                other_idx = idx
                break

        if other_idx is not None:
            self._pane_streams[other_idx] = current
        self._pane_streams[pane_index] = name
        self._active_pane_index = pane_index

        self._ensure_stream_started(name)
        if current:
            self._ensure_stream_started(current)
        self._refresh_layout(save=save, emit=emit)
        return True

    def _on_layout_changed(self, _index: int) -> None:
        try:
            count = int(self._layout_combo.currentData())
        except Exception:
            count = 1
        self.set_layout_count(count)

    def _on_pane_activated(self, pane_index: int) -> None:
        if pane_index == self._active_pane_index:
            return
        self._active_pane_index = pane_index
        self._refresh_layout(save=True, emit=True)

    def _on_pane_stream_changed(self, pane_index: int, name: str) -> None:
        if name:
            self._assign_stream_to_pane(pane_index, name, save=True, emit=True)

    def set_layout_count(self, count: int) -> None:
        count = self._allowed_layout_count(count)
        if count == self._pane_count and self._visible_pane_count() > 0:
            return
        self._pane_count = count
        self._refresh_layout(save=True, emit=True)
        for name in self.visible_stream_names():
            self._ensure_stream_started(name)

    def set_active_pane(self, pane_index: int) -> bool:
        if pane_index < 0 or pane_index >= self._visible_pane_count():
            return False
        self._active_pane_index = pane_index
        self._refresh_layout(save=True, emit=True)
        return True

    def visible_stream_names(self) -> list[str]:
        names: list[str] = []
        for idx in range(self._visible_pane_count()):
            name = self._pane_streams[idx]
            if name:
                names.append(name)
        return names

    def current_stream_name(self) -> str | None:
        visible_count = self._visible_pane_count()
        if visible_count <= 0:
            return None
        idx = max(0, min(self._active_pane_index, visible_count - 1))
        return self._pane_streams[idx]

    def current_video_widget(self) -> VideoWidget | None:
        name = self.current_stream_name()
        if name is None:
            return None
        return self._widgets.get(name)

    def has_stream(self, name: str | None) -> bool:
        if not name:
            return False
        return name in self.stream_names

    def set_current_stream(self, name: str) -> bool:
        if name not in self.stream_names:
            return False

        visible_count = self._visible_pane_count()
        for idx in range(visible_count):
            if self._pane_streams[idx] == name:
                return self.set_active_pane(idx)

        return self._assign_stream_to_pane(self._active_pane_index, name, save=True, emit=True)

    def save_snapshot(self, out_dir: str | None = None, basename: str | None = None) -> str | None:
        vw = self.current_video_widget()
        if vw is None:
            return None
        return vw.save_snapshot(out_dir=out_dir, basename=basename)

    def start_recording(self, out_dir: str | None = None, basename: str | None = None, fps: float = 30.0) -> str | None:
        vw = self.current_video_widget()
        if vw is None:
            return None
        return vw.start_recording(out_dir=out_dir, basename=basename, fps=fps)

    def stop_recording(self) -> None:
        vw = self.current_video_widget()
        if vw is None:
            return
        vw.stop_recording()

    def set_water_correction(self, enabled: bool) -> None:
        self._water_correction_enabled = bool(enabled)
        for widget in self._widgets.values():
            if widget is not None:
                widget.set_water_correction(enabled)

    def cycle_stream(self, step: int) -> None:
        if not self.stream_names:
            return
        cur = self.current_stream_name()
        if cur is None:
            return
        if len(self.stream_names) == 1:
            return

        direction = 1 if int(step) >= 0 else -1
        start_idx = self.stream_names.index(cur)
        for offset in range(1, len(self.stream_names) + 1):
            nxt = self.stream_names[(start_idx + (direction * offset)) % len(self.stream_names)]
            if nxt != cur or len(self.stream_names) == 1:
                self._assign_stream_to_pane(self._active_pane_index, nxt, save=True, emit=True)
                return

    def next_stream(self) -> None:
        self.cycle_stream(+1)

    def prev_stream(self) -> None:
        self.cycle_stream(-1)

    def stop_all(self) -> None:
        try:
            self._warmup_timer.stop()
        except Exception:
            pass
        for name, widget in list(self._widgets.items()):
            if widget is None:
                continue
            try:
                widget.shutdown()
            except Exception:
                pass
            try:
                widget.deleteLater()
            except Exception:
                pass
            self._widgets[name] = None

            cont = self._containers.get(name)
            if cont is not None:
                self._clear_container(cont)
                lay = cont.layout()
                if lay is not None:
                    placeholder = QLabel(f"{name}\n(stopped)")
                    placeholder.setObjectName("videoPanePlaceholder")
                    placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
                    placeholder.setWordWrap(True)
                    lay.addWidget(placeholder)

    def closeEvent(self, event) -> None:
        try:
            self.stop_all()
        except Exception:
            pass
        super().closeEvent(event)
