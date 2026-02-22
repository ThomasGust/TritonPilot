# gui/video_tabs.py
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QWidget, QTabWidget, QVBoxLayout, QLabel

from gui.video_widget import VideoWidget
from video.cam import RemoteCameraManager


class VideoTabs(QWidget):
    """
    Multi-stream video panel.

    Rev-1 behavior:
      - Only the currently selected stream is active/decoded.
      - Switching tabs stops the previous stream and starts the newly selected one.
    """

    def __init__(self, manager: RemoteCameraManager, stream_names: list[str], parent=None):
        super().__init__(parent)
        self.manager = manager
        self.stream_names = stream_names

        self.tabs = QTabWidget()
        # Keep tab titles readable when space is tight.
        try:
            self.tabs.setElideMode(Qt.TextElideMode.ElideRight)
            self.tabs.setUsesScrollButtons(True)
        except Exception:
            pass
        self.tabs.currentChanged.connect(self._on_tab_changed)

        self._containers: dict[str, QWidget] = {}
        self._active_widget: VideoWidget | None = None
        self._active_name: str | None = None

        for name in self.stream_names:
            cont = QWidget()
            lay = QVBoxLayout(cont)
            lay.setContentsMargins(0, 0, 0, 0)
            placeholder = QLabel(f"{name}\n(select tab to start stream)")
            placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            placeholder.setWordWrap(True)
            lay.addWidget(placeholder)
            self._containers[name] = cont
            self.tabs.addTab(cont, name)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.tabs)

        # Start first stream by default (selected tab)
        if self.stream_names:
            self._on_tab_changed(self.tabs.currentIndex())

    def current_video_widget(self) -> VideoWidget | None:
        return self._active_widget

    def current_stream_name(self) -> str | None:
        return self._active_name

    # --- convenience proxies used by MainWindow ---
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

    def cycle_stream(self, step: int) -> None:
        """Move the selected stream tab left/right with wraparound."""
        try:
            count = int(self.tabs.count())
        except Exception:
            count = 0
        if count <= 0:
            return
        try:
            cur = int(self.tabs.currentIndex())
        except Exception:
            cur = 0
        if cur < 0:
            cur = 0
        nxt = (cur + int(step)) % count
        if nxt != cur:
            self.tabs.setCurrentIndex(nxt)

    def next_stream(self) -> None:
        self.cycle_stream(+1)

    def prev_stream(self) -> None:
        self.cycle_stream(-1)

    def _clear_container(self, cont: QWidget):
        lay = cont.layout()
        if lay is None:
            return
        while lay.count():
            item = lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

    def _start_stream(self, name: str):
        cont = self._containers.get(name)
        if cont is None:
            return
        self._clear_container(cont)

        try:
            vw = VideoWidget(self.manager, stream_name=name)
        except Exception as e:
            # Put placeholder back + error
            lay = cont.layout()
            if lay is not None:
                lbl = QLabel(f"Failed to start stream '{name}':\n{e}")
                lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                lbl.setWordWrap(True)
                lay.addWidget(lbl)
            return

        lay = cont.layout()
        if lay is not None:
            lay.addWidget(vw)

        self._active_widget = vw
        self._active_name = name

    def _stop_stream_widget(self, widget: VideoWidget | None, name: str | None, *, clear_active: bool = False):
        if widget is None or name is None:
            return
        try:
            widget.shutdown()
        except Exception:
            pass

        # Replace with placeholder
        cont = self._containers.get(name)
        if cont is not None:
            self._clear_container(cont)
            lay = cont.layout()
            if lay is not None:
                placeholder = QLabel(f"{name}\n(select tab to start stream)")
                placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
                placeholder.setWordWrap(True)
                lay.addWidget(placeholder)

        try:
            widget.deleteLater()
        except Exception:
            pass

        if clear_active and self._active_widget is widget:
            self._active_widget = None
            self._active_name = None

    def _stop_stream(self):
        self._stop_stream_widget(self._active_widget, self._active_name, clear_active=True)

    def _on_tab_changed(self, idx: int):
        if idx < 0 or idx >= len(self.stream_names):
            return
        new_name = self.stream_names[idx]
        if new_name == self._active_name:
            return
        old_widget = self._active_widget
        old_name = self._active_name
        # Start the new stream first so connect/setup can begin immediately.
        self._start_stream(new_name)
        # Then tear down the old stream. This avoids paying shutdown latency
        # before the selected stream even begins connecting.
        if old_widget is not None and old_name is not None:
            self._stop_stream_widget(old_widget, old_name, clear_active=False)

    def stop_all(self):
        self._stop_stream()

    def closeEvent(self, event):
        try:
            self.stop_all()
        except Exception:
            pass
        super().closeEvent(event)
