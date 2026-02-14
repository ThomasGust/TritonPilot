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

    def _stop_stream(self):
        if self._active_widget is None or self._active_name is None:
            return
        try:
            self._active_widget.shutdown()
        except Exception:
            pass

        # Replace with placeholder
        cont = self._containers.get(self._active_name)
        if cont is not None:
            self._clear_container(cont)
            lay = cont.layout()
            if lay is not None:
                placeholder = QLabel(f"{self._active_name}\n(select tab to start stream)")
                placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
                placeholder.setWordWrap(True)
                lay.addWidget(placeholder)

        self._active_widget.deleteLater()
        self._active_widget = None
        self._active_name = None

    def _on_tab_changed(self, idx: int):
        if idx < 0 or idx >= len(self.stream_names):
            return
        new_name = self.stream_names[idx]
        if new_name == self._active_name:
            return
        self._stop_stream()
        self._start_stream(new_name)

    def stop_all(self):
        self._stop_stream()

    def closeEvent(self, event):
        try:
            self.stop_all()
        except Exception:
            pass
        super().closeEvent(event)
