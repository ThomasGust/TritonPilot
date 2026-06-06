"""Embedded SSH console for field diagnostics."""

from __future__ import annotations

import os
import queue
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtCore import QObject, Qt, QThread, pyqtSignal
from PyQt6.QtGui import QTextCursor
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
    QFileDialog,
)


ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


@dataclass(frozen=True)
class SshPreset:
    """A saved SSH target shown in the preset selector."""

    name: str
    host: str
    username: str
    port: int = 22


class SshSessionWorker(QObject):
    """Owns one Paramiko shell session in a background thread."""

    outputReady = pyqtSignal(str)
    statusChanged = pyqtSignal(str, str)
    connectedChanged = pyqtSignal(bool)
    finished = pyqtSignal()

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str = "",
        key_path: str = "",
        trust_new_hosts: bool = True,
        timeout_s: float = 8.0,
    ) -> None:
        super().__init__()
        self.host = str(host).strip()
        self.port = int(port)
        self.username = str(username).strip()
        self.password = str(password)
        self.key_path = str(key_path).strip()
        self.trust_new_hosts = bool(trust_new_hosts)
        self.timeout_s = float(timeout_s)
        self._send_queue: "queue.Queue[str]" = queue.Queue()
        self._stop = threading.Event()
        self._client = None
        self._channel = None

    def send_text(self, text: str) -> None:
        if not self._stop.is_set():
            self._send_queue.put(str(text))

    def stop(self) -> None:
        self._stop.set()
        try:
            self._send_queue.put_nowait("")
        except Exception:
            pass
        for attr in ("_channel", "_client"):
            obj = getattr(self, attr, None)
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass

    def run(self) -> None:
        try:
            import paramiko  # type: ignore

            client = paramiko.SSHClient()
            self._client = client
            try:
                client.load_system_host_keys()
            except Exception:
                pass
            if self.trust_new_hosts:
                client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            else:
                client.set_missing_host_key_policy(paramiko.RejectPolicy())

            kwargs = {
                "hostname": self.host,
                "port": self.port,
                "username": self.username,
                "timeout": self.timeout_s,
                "banner_timeout": self.timeout_s,
                "auth_timeout": self.timeout_s,
                "look_for_keys": not bool(self.password or self.key_path),
                "allow_agent": True,
            }
            if self.password:
                kwargs["password"] = self.password
            if self.key_path:
                kwargs["key_filename"] = self.key_path

            self.statusChanged.emit(f"Connecting to {self.username}@{self.host}:{self.port}", "warn")
            client.connect(**kwargs)
            transport = client.get_transport()
            if transport is not None:
                transport.set_keepalive(20)
            channel = client.invoke_shell(term="xterm", width=120, height=36)
            channel.settimeout(0.15)
            self._channel = channel
            self.connectedChanged.emit(True)
            self.statusChanged.emit(f"Connected to {self.username}@{self.host}:{self.port}", "ok")

            while not self._stop.is_set():
                while True:
                    try:
                        text = self._send_queue.get_nowait()
                    except queue.Empty:
                        break
                    if text and not channel.closed:
                        channel.send(text)

                try:
                    if channel.recv_ready():
                        data = channel.recv(8192)
                        if data:
                            self.outputReady.emit(data.decode("utf-8", errors="replace"))
                except TimeoutError:
                    pass
                except Exception as exc:
                    if not self._stop.is_set():
                        self.statusChanged.emit(f"SSH receive failed: {exc}", "alert")
                    break

                if channel.closed or channel.exit_status_ready():
                    break
                time.sleep(0.02)
        except Exception as exc:
            if not self._stop.is_set():
                self.statusChanged.emit(f"SSH connection failed: {exc}", "alert")
        finally:
            self.connectedChanged.emit(False)
            for attr in ("_channel", "_client"):
                obj = getattr(self, attr, None)
                if obj is not None:
                    try:
                        obj.close()
                    except Exception:
                        pass
                    setattr(self, attr, None)
            self.finished.emit()


class _HistoryLineEdit(QLineEdit):
    """Line edit with shell-style up/down command history."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._history: list[str] = []
        self._history_index: int | None = None

    def remember(self, text: str) -> None:
        command = str(text).strip()
        if not command:
            self._history_index = None
            return
        if not self._history or self._history[-1] != command:
            self._history.append(command)
            self._history = self._history[-200:]
        self._history_index = None

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down) and self._history:
            if event.key() == Qt.Key.Key_Up:
                if self._history_index is None:
                    self._history_index = len(self._history) - 1
                else:
                    self._history_index = max(0, self._history_index - 1)
            else:
                if self._history_index is None:
                    return
                self._history_index += 1
                if self._history_index >= len(self._history):
                    self._history_index = None
                    self.clear()
                    return
            self.setText(self._history[self._history_index])
            self.setCursorPosition(len(self.text()))
            return
        super().keyPressEvent(event)


class SshConsolePage(QWidget):
    """Small embedded SSH console for ROV and laptop maintenance."""

    def __init__(self, *, presets: list[SshPreset] | None = None, parent=None) -> None:
        super().__init__(parent)
        self._presets = list(presets or [])
        self._worker: SshSessionWorker | None = None
        self._thread: QThread | None = None
        self._connected = False

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        header = QFrame()
        header.setObjectName("sshHeader")
        header_lay = QGridLayout(header)
        header_lay.setContentsMargins(10, 8, 10, 8)
        header_lay.setHorizontalSpacing(8)
        header_lay.setVerticalSpacing(6)

        self.preset_combo = QComboBox()
        self.preset_combo.addItem("Custom", None)
        for index, preset in enumerate(self._presets):
            self.preset_combo.addItem(preset.name, index)
        self.preset_combo.currentIndexChanged.connect(self._apply_selected_preset)

        self.host_edit = QLineEdit()
        self.host_edit.setPlaceholderText("host")
        self.user_edit = QLineEdit()
        self.user_edit.setPlaceholderText("user")
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(22)
        self.password_edit = QLineEdit()
        self.password_edit.setPlaceholderText("password")
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit = QLineEdit()
        self.key_edit.setPlaceholderText("private key path")
        self.trust_hosts_check = QCheckBox("Trust host")
        self.trust_hosts_check.setChecked(True)

        self.key_browse_btn = QPushButton("Key...")
        self.key_browse_btn.clicked.connect(self._browse_key)
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.clicked.connect(self.connect_to_host)
        self.disconnect_btn = QPushButton("Disconnect")
        self.disconnect_btn.clicked.connect(self.disconnect_from_host)
        self.disconnect_btn.setEnabled(False)
        self.ctrl_c_btn = QPushButton("Ctrl-C")
        self.ctrl_c_btn.clicked.connect(lambda: self._send_raw("\x03"))
        self.ctrl_c_btn.setEnabled(False)
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.clicked.connect(self.clear_output)

        for field in (self.preset_combo, self.host_edit, self.user_edit, self.password_edit, self.key_edit):
            field.setMinimumWidth(70)
            field.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.port_spin.setMinimumWidth(58)
        for button in (self.key_browse_btn, self.connect_btn, self.disconnect_btn, self.ctrl_c_btn, self.clear_btn):
            button.setMinimumWidth(0)

        header_lay.addWidget(QLabel("Preset"), 0, 0)
        header_lay.addWidget(self.preset_combo, 0, 1)
        header_lay.addWidget(QLabel("Host"), 0, 2)
        header_lay.addWidget(self.host_edit, 0, 3)
        header_lay.addWidget(QLabel("Port"), 0, 4)
        header_lay.addWidget(self.port_spin, 0, 5)
        header_lay.addWidget(self.connect_btn, 0, 6)
        header_lay.addWidget(self.disconnect_btn, 0, 7)

        header_lay.addWidget(QLabel("User"), 1, 0)
        header_lay.addWidget(self.user_edit, 1, 1)
        header_lay.addWidget(QLabel("Password"), 1, 2)
        header_lay.addWidget(self.password_edit, 1, 3)
        header_lay.addWidget(QLabel("Key"), 1, 4)
        header_lay.addWidget(self.key_edit, 1, 5)
        header_lay.addWidget(self.key_browse_btn, 1, 6)
        header_lay.addWidget(self.trust_hosts_check, 1, 7)
        header_lay.setColumnStretch(3, 2)
        header_lay.setColumnStretch(5, 2)
        root.addWidget(header, 0)

        status_row = QHBoxLayout()
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.setSpacing(8)
        self.status_label = QLabel("SSH: disconnected")
        self.status_label.setObjectName("sshStatus")
        status_row.addWidget(self.status_label, 1)
        status_row.addWidget(self.ctrl_c_btn, 0)
        status_row.addWidget(self.clear_btn, 0)
        root.addLayout(status_row, 0)

        self.output = QPlainTextEdit()
        self.output.setObjectName("sshOutput")
        self.output.setReadOnly(True)
        self.output.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        root.addWidget(self.output, 1)

        input_row = QHBoxLayout()
        input_row.setContentsMargins(0, 0, 0, 0)
        input_row.setSpacing(8)
        self.command_edit = _HistoryLineEdit()
        self.command_edit.setPlaceholderText("type a shell command")
        self.command_edit.returnPressed.connect(self.send_command)
        self.send_btn = QPushButton("Send")
        self.send_btn.clicked.connect(self.send_command)
        self.send_btn.setEnabled(False)
        input_row.addWidget(QLabel("$"), 0)
        input_row.addWidget(self.command_edit, 1)
        input_row.addWidget(self.send_btn, 0)
        root.addLayout(input_row, 0)

        if self._presets:
            self.preset_combo.setCurrentIndex(1)
            self._apply_selected_preset()

        self._sync_connected_state(False)

    def _apply_selected_preset(self) -> None:
        data = self.preset_combo.currentData()
        if data is None:
            return
        try:
            preset = self._presets[int(data)]
        except Exception:
            return
        self.host_edit.setText(preset.host)
        self.user_edit.setText(preset.username)
        self.port_spin.setValue(int(preset.port))

    def _browse_key(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(self, "Choose SSH private key", str(Path.home()))
        if path:
            self.key_edit.setText(path)

    @staticmethod
    def _clean_output(text: str) -> str:
        cleaned = ANSI_RE.sub("", str(text))
        cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
        return cleaned

    def _append_output(self, text: str) -> None:
        cleaned = self._clean_output(text)
        if not cleaned:
            return
        cursor = self.output.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(cleaned)
        self.output.setTextCursor(cursor)
        self.output.ensureCursorVisible()

    def clear_output(self) -> None:
        self.output.clear()

    def _set_status(self, text: str, tone: str = "") -> None:
        self.status_label.setText(str(text or "SSH: -"))
        self.status_label.setProperty("tone", tone or "")
        try:
            self.status_label.style().unpolish(self.status_label)
            self.status_label.style().polish(self.status_label)
            self.status_label.update()
        except Exception:
            pass

    def _sync_connected_state(self, connected: bool) -> None:
        self._connected = bool(connected)
        self.connect_btn.setEnabled(not self._connected)
        self.disconnect_btn.setEnabled(self._connected)
        self.send_btn.setEnabled(self._connected)
        self.ctrl_c_btn.setEnabled(self._connected)
        self.command_edit.setEnabled(self._connected)

    def connect_to_host(self) -> None:
        if self._thread is not None:
            return
        host = self.host_edit.text().strip()
        username = self.user_edit.text().strip()
        if not host or not username:
            self._set_status("SSH: host and user are required", "alert")
            return
        worker = SshSessionWorker(
            host=host,
            port=int(self.port_spin.value()),
            username=username,
            password=self.password_edit.text(),
            key_path=self.key_edit.text(),
            trust_new_hosts=self.trust_hosts_check.isChecked(),
        )
        thread = QThread(self)
        self._worker = worker
        self._thread = thread
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.outputReady.connect(self._append_output)
        worker.statusChanged.connect(self._set_status)
        worker.connectedChanged.connect(self._sync_connected_state)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(lambda: self._clear_worker(thread))
        self._set_status("SSH: starting connection", "warn")
        thread.start()

    def _clear_worker(self, thread: QThread) -> None:
        if self._thread is thread:
            self._thread = None
            self._worker = None
        self._sync_connected_state(False)
        if "failed" not in self.status_label.text().lower():
            self._set_status("SSH: disconnected")

    def _send_raw(self, text: str) -> None:
        if self._worker is not None and self._connected:
            self._worker.send_text(text)

    def send_command(self) -> None:
        command = self.command_edit.text()
        if not self._connected:
            return
        self.command_edit.remember(command)
        self.command_edit.clear()
        self._send_raw(command + "\n")

    def disconnect_from_host(self) -> None:
        worker = self._worker
        if worker is not None:
            worker.stop()
        thread = self._thread
        if thread is not None:
            thread.quit()
            thread.wait(1500)
        self._clear_worker(thread) if thread is not None else self._sync_connected_state(False)

    def shutdown(self) -> None:
        self.disconnect_from_host()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self.shutdown()
        super().closeEvent(event)


def default_pilot_ssh_presets(rov_host: str, *, local_user: str | None = None) -> list[SshPreset]:
    """Return useful SSH presets for the pilot station."""
    user = local_user or os.environ.get("USERNAME") or os.environ.get("USER") or "TritonRobotics"
    return [
        SshPreset("ROV", str(rov_host or "192.168.1.4"), "triton", 22),
        SshPreset("Analysis Laptop", "10.77.0.2", user, 22),
        SshPreset("Localhost", "127.0.0.1", user, 22),
    ]
