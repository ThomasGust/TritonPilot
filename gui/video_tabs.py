# gui/video_tabs.py
from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import QWidget, QTabWidget, QVBoxLayout, QLabel

from gui.video_widget import VideoWidget
from video.cam import RemoteCameraManager


class VideoTabs(QWidget):
    """
    Multi-stream video panel.

    Multi-cam warm mode behavior:
      - Keep the current tabbed UI/"views".
      - Start the selected stream immediately.
      - Pre-warm all other streams in the background (staggered) so switching tabs
        is effectively instant and future multi-view layouts can reuse live widgets.
      - Do NOT stop streams when switching tabs.

    Notes:
      - Warm-up is serialized/staggered to avoid hammering the current VideoRPC
        implementation with several simultaneous start_stream calls.
      - Hidden tabs still run their stream widgets (intended for instant switching).
    """

    def __init__(self, manager: RemoteCameraManager, stream_names: list[str], parent=None):
        super().__init__(parent)
        self.manager = manager
        self.stream_names = stream_names

        self.tabs = QTabWidget()
        try:
            self.tabs.setElideMode(Qt.TextElideMode.ElideRight)
            self.tabs.setUsesScrollButtons(True)
        except Exception:
            pass
        self.tabs.currentChanged.connect(self._on_tab_changed)

        self._containers: dict[str, QWidget] = {}
        self._widgets: dict[str, VideoWidget | None] = {}
        self._warmup_index: int = 0
        self._warmup_timer = QTimer(self)
        self._warmup_timer.setSingleShot(True)
        self._warmup_timer.timeout.connect(self._warmup_next)

        for name in self.stream_names:
            cont = QWidget()
            lay = QVBoxLayout(cont)
            lay.setContentsMargins(0, 0, 0, 0)
            placeholder = QLabel(f"{name}\n(starting when needed)")
            placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            placeholder.setWordWrap(True)
            lay.addWidget(placeholder)
            self._containers[name] = cont
            self._widgets[name] = None
            self.tabs.addTab(cont, name)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.tabs)

        # Start selected tab immediately, then prewarm the rest one-by-one.
        if self.stream_names:
            cur = self.tabs.currentIndex()
            if cur < 0:
                cur = 0
            self._ensure_stream_started(self.stream_names[cur])
            self._warmup_index = 0
            self._warmup_timer.start(700)

    def current_video_widget(self) -> VideoWidget | None:
        name = self.current_stream_name()
        if name is None:
            return None
        return self._widgets.get(name)

    def current_stream_name(self) -> str | None:
        try:
            idx = int(self.tabs.currentIndex())
        except Exception:
            idx = -1
        if idx < 0 or idx >= len(self.stream_names):
            return None
        return self.stream_names[idx]

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
                # Do not delete if this is a live VideoWidget we are preserving.
                if not isinstance(w, VideoWidget):
                    w.deleteLater()

    def _ensure_stream_started(self, name: str):
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
                lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                lbl.setWordWrap(True)
                lay.addWidget(lbl)
            self._widgets[name] = None
            return

        lay = cont.layout()
        if lay is not None:
            lay.addWidget(vw)
        self._widgets[name] = vw

    def _warmup_next(self):
        if not self.stream_names:
            return
        n = len(self.stream_names)
        # Walk the list once, starting after the current tab, and start the first missing stream.
        cur_name = self.current_stream_name()
        for _ in range(n):
            idx = self._warmup_index % n
            self._warmup_index += 1
            name = self.stream_names[idx]
            if name == cur_name:
                continue
            if self._widgets.get(name) is None:
                self._ensure_stream_started(name)
                # Continue warming the remaining streams, staggered.
                self._warmup_timer.start(700)
                return

    def _on_tab_changed(self, idx: int):
        if idx < 0 or idx >= len(self.stream_names):
            return
        new_name = self.stream_names[idx]
        # Ensure stream exists (covers cases where warmup hasn't reached it yet).
        self._ensure_stream_started(new_name)

    def stop_all(self):
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
                    placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
                    placeholder.setWordWrap(True)
                    lay.addWidget(placeholder)

    def closeEvent(self, event):
        try:
            self.stop_all()
        except Exception:
            pass
        super().closeEvent(event)
