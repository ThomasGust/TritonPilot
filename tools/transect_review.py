"""Standalone session-review applet for the transect optical autopilot.

A PyQt6 desktop tool to *look at past holds* and *validate the vision model offline*
against the recorded video + telemetry -- no ROV, no water time. It is the
instrument we use to see the rotation tracker working before it ever drives yaw.

What it shows, time-synced on one scrubbable timeline:
  * the arm-camera video with the transect overlay drawn on top
    (``tracking.transect_overlay.draw_transect_overlay``);
  * the per-frame model error that ACTUALLY FLEW (the ``tracking`` stream's
    ``visual`` payload: er/ex/ey/es/violation/confidence) -- so you can see e.g.
    the phantom ``er`` the old detector produced;
  * the vehicle's rotation truth (raw IMU ``gyro.z`` + its integral) and the yaw
    thrust the ROV actually commanded (``cmd_final.yaw``);
  * optionally, a fresh RE-RUN of a chosen detector over the same frames, plotted
    next to the recorded signals -- the A/B that proves a new tracker is better.

Run:
    python -m tools.transect_review [recordings/<session>]      # or pick via the UI

Alignment: each mp4 frame ``i`` maps to wall time ``t0 + i/fps`` where ``t0`` is the
session video start (``capture_manifest.json``'s ``started_wall_ts``); the stream
records carry wall-clock ``t``. Secondary clips in the same session are offset by
their filename timestamp delta (timezone-independent).
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import os
import re
import sys
import threading
from bisect import bisect_right
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import cv2
import numpy as np

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QImage, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QHBoxLayout, QLabel, QMainWindow,
    QPushButton, QSizePolicy, QSlider, QVBoxLayout, QWidget,
)

import matplotlib
matplotlib.use("QtAgg")
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure

# Make this runnable both ways: `python -m tools.transect_review` AND clicking "Run"
# on this file in VSCode (which puts tools/ -- not the repo root -- on sys.path). Put
# the repo root first so the `tracking`/`video` packages always resolve.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from tracking.optical_tracker import VisualTargetError
from tracking.transect_overlay import draw_transect_overlay
from tracking.transect_policy import (
    TransectEstimate, TransectModel, TransectObservation, TransectPolicy,
)
from gui.transect_overlay_view import paint_transect_hud_overlay


# ===========================================================================
# Session data: video + telemetry, aligned to one timeline (seconds since t0).
# ===========================================================================
@dataclass
class Series:
    """A named telemetry channel sampled irregularly in time."""
    t: List[float] = field(default_factory=list)   # seconds since session t0
    v: List[float] = field(default_factory=list)

    def add(self, t: float, v: Optional[float]) -> None:
        if v is None:
            return
        self.t.append(t)
        self.v.append(float(v))

    def sample(self, t: float) -> Optional[float]:
        """Most recent value at or before ``t`` (zero-order hold)."""
        if not self.t:
            return None
        i = bisect_right(self.t, t) - 1
        return self.v[i] if i >= 0 else None


def _default_recordings_dir() -> str:
    """The repo's recordings/ folder, regardless of the launch working directory."""
    return os.path.join(_REPO_ROOT, "recordings")


def _discover_sessions(recordings_dir: Optional[str] = None) -> List[str]:
    """Return recordings/<session> dirs that hold a usable arm-cam clip, newest first.

    Session folder names are timestamps (YYYYMMDD-HHMMSS), so a reverse sort puts the
    most recent first -- which is what the picker auto-selects.
    """
    root = recordings_dir or _default_recordings_dir()
    if not os.path.isdir(root):
        return []
    sessions = []
    for name in sorted(os.listdir(root), reverse=True):
        d = os.path.join(root, name)
        if os.path.isdir(d) and glob.glob(os.path.join(d, "**", "*.mp4"), recursive=True):
            sessions.append(d)
    return sessions


_FNAME_TS = re.compile(r"(\d{8})-(\d{6})")


def _filename_epoch(path: str) -> Optional[float]:
    """Parse the YYYYMMDD-HHMMSS stamp in a clip name to a naive epoch (local).

    Only the *difference* between two clips is used, so the (missing) timezone
    cancels -- this just gives a per-clip start offset within a session.
    """
    m = _FNAME_TS.search(os.path.basename(path))
    if not m:
        return None
    import datetime as _dt
    try:
        dt = _dt.datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
        return dt.timestamp()
    except ValueError:
        return None


class SessionData:
    """Loads one ``recordings/<session>/`` (or a bare mp4) and exposes aligned series."""

    def __init__(self, path: str):
        self.path = path
        self.clips: List[str] = []
        self.streams_log: Optional[str] = None
        self.manifest: dict = {}
        self.t0_wall: Optional[float] = None        # wall ts of the primary clip's frame 0
        self.primary_clip: Optional[str] = None

        self._resolve(path)
        # Per-frame recorded model error (built from the 'tracking' stream), plus the
        # curated plot channels.
        self.visual: Series = Series()              # er, indexed via .v but we keep payloads:
        self.visual_payloads: List[Tuple[float, dict]] = []   # (t_since_t0, visual dict)
        self.series: Dict[str, Series] = {}
        if self.streams_log:
            self._parse_streams()

    # -- discovery -----------------------------------------------------------
    def _resolve(self, path: str) -> None:
        if os.path.isfile(path) and path.lower().endswith(".mp4"):
            self.clips = [path]
            self.primary_clip = path
            self.t0_wall = _filename_epoch(path)
            sess = os.path.dirname(path)
        else:
            sess = path
            vids = sorted(glob.glob(os.path.join(sess, "**", "*.mp4"), recursive=True))
            self.clips = [v for v in vids if "Arm_Camera" in v] or vids
            logs = sorted(glob.glob(os.path.join(sess, "*_streams.jsonl")))
            self.streams_log = max(logs, key=os.path.getsize) if logs else None
            mani = os.path.join(sess, "capture_manifest.json")
            if os.path.isfile(mani):
                with open(mani) as f:
                    self.manifest = json.load(f)
            self.t0_wall = self.manifest.get("started_wall_ts")
            # primary = the clip named in the manifest, else the largest.
            mpath = (self.manifest.get("video") or {}).get("path")
            if mpath:
                cand = os.path.join(sess, mpath.replace("/", os.sep))
                if os.path.isfile(cand):
                    self.primary_clip = cand
            if self.primary_clip is None and self.clips:
                self.primary_clip = max(self.clips, key=os.path.getsize)
        if not self.clips:
            raise SystemExit(f"No Arm_Camera .mp4 found under {path}")

    def clip_t0_wall(self, clip: str) -> Optional[float]:
        """Wall ts of frame 0 for any clip, offset from the primary by filename delta."""
        if clip == self.primary_clip or self.t0_wall is None:
            return self.t0_wall
        a, b = _filename_epoch(self.primary_clip or ""), _filename_epoch(clip)
        if a is None or b is None:
            return self.t0_wall
        return self.t0_wall + (b - a)

    # -- telemetry -----------------------------------------------------------
    def _parse_streams(self) -> None:
        t0 = self.t0_wall
        # Channels we plot. Each entry pulls one number out of a record's msg.
        s = self.series
        for name in ("er", "ex", "ey", "es", "violation", "confidence",
                     "gyro_z_dps", "heading_int", "cmd_yaw", "engaged"):
            s[name] = Series()
        gyro_t: List[float] = []
        gyro_w: List[float] = []   # deg/s for integration
        with open(self.streams_log) as f:
            for line in f:
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                tw = o.get("t")
                if tw is None or t0 is None:
                    continue
                t = tw - t0
                stream = o.get("stream")
                msg = o.get("msg") or {}
                if stream == "tracking":
                    vis = msg.get("visual") or {}
                    self.visual_payloads.append((t, vis))
                    if vis.get("valid"):
                        for k in ("er", "ex", "ey", "es", "violation", "confidence"):
                            if k in vis:
                                s[k].add(t, vis[k])
                elif stream == "sensors":
                    sub = msg.get("sensor") or msg.get("type")
                    if sub == "imu":
                        gz = (msg.get("gyro") or {}).get("z")
                        if gz is not None:
                            dps = math.degrees(float(gz))
                            s["gyro_z_dps"].add(t, dps)
                            gyro_t.append(t)
                            gyro_w.append(dps)
                    elif sub == "autopilot_status":
                        cf = (((msg.get("control") or {}).get("status") or {})
                              .get("cmd_final") or {})
                        if "yaw" in cf:
                            s["cmd_yaw"].add(t, cf["yaw"])
                elif stream == "pilot":
                    sk = ((msg.get("modes") or {}).get("station_keep"))
                    if sk is not None:
                        s["engaged"].add(t, 1.0 if sk else 0.0)
        # Integrate gyro.z to a heading-drift truth (deg), starting at 0.
        if gyro_t:
            order = sorted(range(len(gyro_t)), key=lambda i: gyro_t[i])
            acc = 0.0
            prev = gyro_t[order[0]]
            for i in order:
                dt = max(0.0, gyro_t[i] - prev)
                acc += gyro_w[i] * dt
                prev = gyro_t[i]
                s["heading_int"].add(gyro_t[i], acc)


# ===========================================================================
# Recorded-vs-rerun overlay reconstruction.
# ===========================================================================
def estimate_from_visual(vis: dict, model: TransectModel) -> TransectEstimate:
    """Rebuild a minimal TransectEstimate from a recorded ``visual`` payload.

    The observation (blue box geometry) was NOT logged live, so recorded mode draws
    the target reticle + the error HUD that flew, not a detected box.
    """
    valid = bool(vis.get("valid"))
    err = VisualTargetError(
        valid=valid,
        ex=float(vis.get("ex", 0.0)), ey=float(vis.get("ey", 0.0)),
        es=float(vis.get("es", 0.0)), er=float(vis.get("er", 0.0)),
        violation=float(vis.get("violation", 0.0)),
        confidence=float(vis.get("confidence", 0.0)),
        ts=vis.get("ts"),
    )
    return TransectEstimate(
        error=err,
        lock_state="lock" if valid else "no_target",
        confidence=err.confidence,
        violation=err.violation,
        clean=valid and err.violation <= model.violation_clean_eps,
        footprint_cm=None, offset_cm=None, margin_cm=None,
        target_center=(model.target_cx, model.target_cy),
        reasons=["recorded"],
    )


class RerunPass:
    """A cached run of a detector+policy over every frame of a clip (for A/B).

    Decoding the whole clip once means scrubbing is instant and the re-run error can
    be plotted alongside the recorded one. Runs in a worker thread.
    """

    def __init__(self, clip: str, model: TransectModel,
                 detector_factory: Callable[[], object]):
        self.clip = clip
        self.model = model
        self.detector_factory = detector_factory
        self.obs: List[TransectObservation] = []
        self.est: List[TransectEstimate] = []
        self.done = False
        self.progress = 0.0
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self, on_progress: Optional[Callable[[float], None]] = None) -> None:
        cap = cv2.VideoCapture(self.clip)
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
        detector = self.detector_factory()
        policy = TransectPolicy(self.model)
        i = 0
        while not self._cancel:
            ok, frame = cap.read()
            if not ok:
                break
            obs = detector.detect(frame)
            est = policy.evaluate(obs)
            self.obs.append(obs)
            self.est.append(est)
            i += 1
            if on_progress and (i % 30 == 0):
                self.progress = i / total
                on_progress(self.progress)
        cap.release()
        self.done = not self._cancel


# ===========================================================================
# Matplotlib strip-chart panel with a blitted playback cursor.
# ===========================================================================
class PlotPanel(FigureCanvasQTAgg):
    def __init__(self):
        self.fig = Figure(figsize=(5, 6), constrained_layout=True)
        super().__init__(self.fig)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.axes = []
        self.cursors = []
        self._bg = None
        self._duration = 1.0
        self.mpl_connect("resize_event", lambda _e: self._invalidate())

    def _invalidate(self) -> None:
        self._bg = None

    def configure(self, titles: List[str], duration: float) -> None:
        self.fig.clear()
        self.axes = []
        self.cursors = []
        self._duration = max(1.0, duration)
        n = len(titles)
        for i, title in enumerate(titles):
            ax = self.fig.add_subplot(n, 1, i + 1, sharex=self.axes[0] if self.axes else None)
            ax.set_ylabel(title, fontsize=8)
            ax.tick_params(labelsize=7)
            ax.grid(True, alpha=0.25)
            if i < n - 1:
                ax.tick_params(labelbottom=False)
            self.axes.append(ax)
            self.cursors.append(ax.axvline(0.0, color="k", lw=1.0, alpha=0.7))
        self.axes[-1].set_xlabel("time (s)", fontsize=8)
        self.axes[0].set_xlim(0, self._duration)

    def plot(self, ax_idx: int, series: Series, **kw) -> None:
        if 0 <= ax_idx < len(self.axes) and series.t:
            self.axes[ax_idx].plot(series.t, series.v, **kw)

    def shade(self, ax_idx: int, series: Series, color="#cfe8cf") -> None:
        """Shade time spans where a 0/1 series is 1 (e.g. station-keep engaged)."""
        if not (0 <= ax_idx < len(self.axes)) or not series.t:
            return
        ax = self.axes[ax_idx]
        on = None
        for t, v in zip(series.t, series.v):
            if v >= 0.5 and on is None:
                on = t
            elif v < 0.5 and on is not None:
                ax.axvspan(on, t, color=color, alpha=0.4, lw=0)
                on = None
        if on is not None:
            ax.axvspan(on, self._duration, color=color, alpha=0.4, lw=0)

    def finalize(self) -> None:
        for ax in self.axes:
            if ax.get_legend_handles_labels()[0]:
                ax.legend(fontsize=6, loc="upper right", ncol=2)
        self.draw()
        self._bg = self.copy_from_bbox(self.fig.bbox)

    def set_cursor(self, t: float) -> None:
        if not self.cursors:
            return
        if self._bg is None:
            self.draw()
            self._bg = self.copy_from_bbox(self.fig.bbox)
        self.restore_region(self._bg)
        for ax, ln in zip(self.axes, self.cursors):
            ln.set_xdata([t, t])
            ax.draw_artist(ln)
        self.blit(self.fig.bbox)


# ===========================================================================
# Main window.
# ===========================================================================
class ReviewWindow(QMainWindow):
    _rerun_progress = pyqtSignal(float)
    _rerun_done = pyqtSignal()

    def __init__(self, session_path: Optional[str] = None):
        super().__init__()
        self.setWindowTitle("Transect Session Review")
        self.resize(1500, 900)
        self.model = TransectModel()
        self.session: Optional[SessionData] = None
        self.cap: Optional[cv2.VideoCapture] = None
        self.clip: Optional[str] = None
        self.fps = 30.0
        self.frame_count = 0
        self.cur_frame = 0
        self.cur_bgr: Optional[np.ndarray] = None
        self.playing = False
        self.speed = 1.0
        self.overlay_mode = "recorded"     # recorded | rerun
        self.view_mode = "review"          # review | pilot
        self.rerun: Optional[RerunPass] = None

        self._build_ui()
        self._rerun_progress.connect(lambda p: self.status.setText(f"Re-run pass… {p*100:.0f}%"))
        self._rerun_done.connect(self._on_rerun_done)

        self.play_timer = QTimer(self)
        self.play_timer.timeout.connect(self._tick)

        # Populate the picker and open something immediately: the path given on the
        # command line, else the most recent recording, so the app is never empty.
        self.populate_sessions()
        if session_path:
            self.load_session(session_path)
        elif self.session_box.count() and self.session_box.itemData(0):
            self.load_session(self.session_box.itemData(0))

    # -- UI ------------------------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)

        # Left: session picker + video + transport.
        left = QVBoxLayout()

        # --- session picker: a dropdown of recordings/<session> (newest first) plus
        #     browse buttons for any folder or bare mp4 anywhere on disk ---
        sess_row = QHBoxLayout()
        sess_row.addWidget(QLabel("Session:"))
        self.session_box = QComboBox()
        self.session_box.setMinimumWidth(280)
        self.session_box.currentIndexChanged.connect(self._on_session_selected)
        sess_row.addWidget(self.session_box, 1)
        self.btn_open_folder = QPushButton("Open folder…")
        self.btn_open_folder.clicked.connect(self._open_dialog)
        self.btn_open_file = QPushButton("Open file…")
        self.btn_open_file.clicked.connect(self._open_file_dialog)
        self.btn_refresh = QPushButton("⟳")
        self.btn_refresh.setToolTip("Rescan recordings/ for new sessions")
        self.btn_refresh.setFixedWidth(34)
        self.btn_refresh.clicked.connect(self.populate_sessions)
        for w in (self.btn_open_folder, self.btn_open_file, self.btn_refresh):
            sess_row.addWidget(w)
        left.addLayout(sess_row)

        self.video = QLabel("Pick a session above — or Open folder… / Open file…")
        self.video.setObjectName("reviewVideo")
        self.video.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video.setMinimumSize(640, 360)
        self.video.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.video.setStyleSheet("background:#111;color:#aaa;")
        left.addWidget(self.video, 1)

        self.scrub = QSlider(Qt.Orientation.Horizontal)
        self.scrub.setMinimum(0)
        self.scrub.sliderMoved.connect(self._on_scrub)
        self.scrub.sliderPressed.connect(lambda: self._pause())
        left.addWidget(self.scrub)

        bar = QHBoxLayout()
        self.btn_play = QPushButton("▶ Play")
        self.btn_play.clicked.connect(self._toggle_play)
        self.btn_prev = QPushButton("◀ ⏴")
        self.btn_prev.clicked.connect(lambda: self._step(-1))
        self.btn_next = QPushButton("⏵ ▶")
        self.btn_next.clicked.connect(lambda: self._step(+1))
        self.clip_box = QComboBox()
        self.clip_box.currentIndexChanged.connect(self._on_clip_changed)
        self.speed_box = QComboBox()
        for s in ("0.25x", "0.5x", "1x", "2x"):
            self.speed_box.addItem(s)
        self.speed_box.setCurrentText("1x")
        self.speed_box.currentTextChanged.connect(self._on_speed)
        self.overlay_box = QComboBox()
        self.overlay_box.addItems(["Overlay: recorded", "Overlay: re-run detector"])
        self.overlay_box.setToolTip("Recorded uses logged model errors; re-run detector adds detected blue/red geometry.")
        self.overlay_box.currentIndexChanged.connect(self._on_overlay_mode)
        self.view_box = QComboBox()
        self.view_box.addItems(["View: full review frame", "View: pilot tab crop + HUD"])
        self.view_box.setToolTip("Matches the live Transect tab square crop and transparent HUD.")
        self.view_box.currentIndexChanged.connect(self._on_view_mode)
        self.btn_export = QPushButton("Save frame")
        self.btn_export.clicked.connect(self._export_frame)
        for w in (self.btn_prev, self.btn_play, self.btn_next,
                  QLabel("  clip:"), self.clip_box, QLabel("  speed:"), self.speed_box,
                  self.overlay_box, self.view_box, self.btn_export):
            bar.addWidget(w)
        bar.addStretch(1)
        left.addLayout(bar)

        self.status = QLabel("")
        self.status.setStyleSheet("color:#444;")
        left.addWidget(self.status)
        root.addLayout(left, 3)

        # Right: synced plots.
        self.plots = PlotPanel()
        root.addWidget(self.plots, 2)

    # -- loading -------------------------------------------------------------
    def populate_sessions(self) -> None:
        """Fill the session dropdown from recordings/ (newest first)."""
        sessions = _discover_sessions()
        self.session_box.blockSignals(True)
        self.session_box.clear()
        for d in sessions:
            self.session_box.addItem(os.path.basename(d.rstrip(os.sep)), d)
        if not sessions:
            self.session_box.addItem("(no recordings found)", None)
        self.session_box.setCurrentIndex(0)
        self.session_box.blockSignals(False)  # caller decides what to load

    def _on_session_selected(self, idx: int) -> None:
        path = self.session_box.itemData(idx)
        if path and path != getattr(self.session, "path", None):
            self.load_session(path)

    def _sync_session_box(self, path: str) -> None:
        """Reflect the loaded session in the dropdown (adding it if it's off-list)."""
        self.session_box.blockSignals(True)
        i = self.session_box.findData(path)
        if i < 0:
            self.session_box.insertItem(0, os.path.basename(path.rstrip(os.sep)), path)
            i = 0
        self.session_box.setCurrentIndex(i)
        self.session_box.blockSignals(False)

    def _open_dialog(self) -> None:
        d = QFileDialog.getExistingDirectory(
            self, "Pick a recordings/<session> folder", _default_recordings_dir())
        if d:
            self.load_session(d)

    def _open_file_dialog(self) -> None:
        f, _ = QFileDialog.getOpenFileName(
            self, "Open an arm-camera mp4", _default_recordings_dir(), "Video (*.mp4)")
        if f:
            self.load_session(f)

    def load_session(self, path: str) -> None:
        self.status.setText(f"Loading {path}…")
        QApplication.processEvents()
        self.session = SessionData(path)
        self.clip_box.blockSignals(True)
        self.clip_box.clear()
        for c in self.session.clips:
            self.clip_box.addItem(os.path.basename(c))
        # default to the primary (largest/manifest) clip
        if self.session.primary_clip in self.session.clips:
            self.clip_box.setCurrentIndex(self.session.clips.index(self.session.primary_clip))
        self.clip_box.blockSignals(False)
        self._load_clip(self.session.primary_clip or self.session.clips[0])
        self._build_plots()
        self._sync_session_box(self.session.path)
        self.setWindowTitle(f"Transect Session Review — {os.path.basename(path.rstrip(os.sep))}")

    def _load_clip(self, clip: str) -> None:
        self._cancel_rerun()
        if self.cap is not None:
            self.cap.release()
        self.clip = clip
        self.cap = cv2.VideoCapture(clip)
        self.fps = self.cap.get(cv2.CAP_PROP_FPS) or 30.0
        self.frame_count = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))
        self.scrub.setMaximum(max(0, self.frame_count - 1))
        self.cur_frame = 0
        self._seek(0)
        if self.overlay_mode == "rerun":
            self._start_rerun()

    # -- alignment helpers ---------------------------------------------------
    def _frame_time(self, frame_idx: int) -> float:
        """Seconds since session t0 for a frame of the current clip."""
        if self.session is None:
            return frame_idx / self.fps
        clip_t0 = self.session.clip_t0_wall(self.clip)
        base = 0.0
        if clip_t0 is not None and self.session.t0_wall is not None:
            base = clip_t0 - self.session.t0_wall
        return base + frame_idx / self.fps

    def _duration(self) -> float:
        return self._frame_time(self.frame_count)

    # -- plots ---------------------------------------------------------------
    def _build_plots(self) -> None:
        if self.session is None:
            return
        s = self.session.series
        titles = ["rotation er", "translation/size", "lock quality", "yaw truth"]
        self.plots.configure(titles, self._duration())
        # 0: rotation error (the key channel)
        self.plots.shade(0, s["engaged"])
        self.plots.plot(0, s["er"], color="#c0392b", lw=1.2, label="er (recorded)")
        self.plots.axes[0].set_ylim(-1.1, 1.1)
        self.plots.axes[0].axhline(0, color="#888", lw=0.6)
        # 1: ex/ey/es
        self.plots.plot(1, s["ex"], color="#2980b9", lw=1.0, label="ex")
        self.plots.plot(1, s["ey"], color="#27ae60", lw=1.0, label="ey")
        self.plots.plot(1, s["es"], color="#8e44ad", lw=1.0, label="es")
        self.plots.axes[1].set_ylim(-1.1, 1.1)
        # 2: confidence + violation
        self.plots.plot(2, s["confidence"], color="#16a085", lw=1.0, label="confidence")
        self.plots.plot(2, s["violation"], color="#e67e22", lw=1.0, label="violation")
        self.plots.axes[2].set_ylim(-0.05, 1.05)
        # 3: gyro truth + commanded yaw
        self.plots.plot(3, s["gyro_z_dps"], color="#7f8c8d", lw=0.8, label="gyro.z °/s")
        self.plots.plot(3, s["cmd_yaw"], color="#d35400", lw=1.0, label="cmd_final.yaw")
        self.plots.plot(3, s["heading_int"], color="#2c3e50", lw=1.0, label="∫gyro (°)", ls="--")
        self._refresh_plots()

    def _refresh_plots(self) -> None:
        # Re-draw with any re-run overlay series, then capture the blit background.
        if self.rerun is not None and self.rerun.done:
            self._plot_rerun_series()
        self.plots.finalize()
        self.plots.set_cursor(self._frame_time(self.cur_frame))

    def _plot_rerun_series(self) -> None:
        ts = [self._frame_time(i) for i in range(len(self.rerun.est))]
        er = [e.error.er for e in self.rerun.est]
        # plot only where valid so dropouts don't draw spurious zeros
        vt, vv = [], []
        for t, e in zip(ts, self.rerun.est):
            if e.error.valid:
                vt.append(t); vv.append(e.error.er)
        self.plots.axes[0].plot(vt, vv, color="#27ae60", lw=1.2, label="er (re-run)")

    # -- playback ------------------------------------------------------------
    def _toggle_play(self) -> None:
        self._play() if not self.playing else self._pause()

    def _play(self) -> None:
        if self.cap is None:
            return
        self.playing = True
        self.btn_play.setText("⏸ Pause")
        self.play_timer.start(int(1000 / (self.fps * self.speed)))

    def _pause(self) -> None:
        self.playing = False
        self.btn_play.setText("▶ Play")
        self.play_timer.stop()

    def _tick(self) -> None:
        if self.cur_frame + 1 >= self.frame_count:
            self._pause()
            return
        ok, frame = self.cap.read()
        if not ok:
            self._pause()
            return
        self.cur_frame += 1
        self.cur_bgr = frame
        self._render()

    def _step(self, d: int) -> None:
        self._pause()
        self._seek(self.cur_frame + d)

    def _seek(self, frame_idx: int) -> None:
        if self.cap is None:
            return
        frame_idx = max(0, min(self.frame_count - 1, frame_idx))
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
        ok, frame = self.cap.read()
        if not ok:
            return
        self.cur_frame = frame_idx
        self.cur_bgr = frame
        self._render()

    def _on_scrub(self, value: int) -> None:
        self._seek(value)

    def _on_speed(self, text: str) -> None:
        self.speed = float(text.rstrip("x"))
        if self.playing:
            self.play_timer.start(int(1000 / (self.fps * self.speed)))

    def _on_clip_changed(self, idx: int) -> None:
        if self.session and 0 <= idx < len(self.session.clips):
            self._load_clip(self.session.clips[idx])

    # -- overlay / re-run ----------------------------------------------------
    def _on_overlay_mode(self, idx: int) -> None:
        self.overlay_mode = "rerun" if idx == 1 else "recorded"
        if self.overlay_mode == "rerun" and (self.rerun is None or self.rerun.clip != self.clip):
            self._start_rerun()
        self._render()

    def _on_view_mode(self, idx: int) -> None:
        self.view_mode = "pilot" if idx == 1 else "review"
        self._render()

    def _start_rerun(self) -> None:
        self._cancel_rerun()
        from tracking.transect_cv import ClassicalTransectDetector
        self.rerun = RerunPass(self.clip, self.model, lambda: ClassicalTransectDetector())
        self.status.setText("Re-run pass… 0%")

        def worker():
            self.rerun.run(on_progress=lambda p: self._rerun_progress.emit(p))
            if self.rerun.done:
                self._rerun_done.emit()

        threading.Thread(target=worker, daemon=True).start()

    def _cancel_rerun(self) -> None:
        if self.rerun is not None:
            self.rerun.cancel()
            self.rerun = None

    def _on_rerun_done(self) -> None:
        self.status.setText(f"Re-run complete ({len(self.rerun.est)} frames).")
        self._build_plots()
        self._render()

    # -- rendering -----------------------------------------------------------
    def _render(self) -> None:
        if self.cur_bgr is None:
            return
        est, obs = self._current_overlay_state()
        if self.view_mode == "pilot":
            self._show_qimage(self._pilot_tab_qimage(self.cur_bgr, est, obs))
        else:
            frame = self.cur_bgr.copy()
            draw_transect_overlay(frame, self.model, est, obs)
            self._show_frame(frame)

        self.scrub.blockSignals(True)
        self.scrub.setValue(self.cur_frame)
        self.scrub.blockSignals(False)
        self.plots.set_cursor(self._frame_time(self.cur_frame))
        t = self._frame_time(self.cur_frame)
        self.status.setText(
            f"frame {self.cur_frame}/{self.frame_count}   t={t:6.2f}s   "
            f"{os.path.basename(self.clip or '')}   overlay={self.overlay_mode}   view={self.view_mode}")

    def _current_overlay_state(self) -> tuple[TransectEstimate, Optional[TransectObservation]]:
        if self.overlay_mode == "rerun" and self.rerun is not None and self.rerun.done \
                and self.cur_frame < len(self.rerun.est):
            return self.rerun.est[self.cur_frame], self.rerun.obs[self.cur_frame]
        vis = self._recorded_visual_at(self._frame_time(self.cur_frame))
        return estimate_from_visual(vis, self.model), None

    def _recorded_visual_at(self, t: float) -> dict:
        payloads = self.session.visual_payloads if self.session else []
        if not payloads:
            return {"valid": False}
        ts = [p[0] for p in payloads]
        i = bisect_right(ts, t) - 1
        return payloads[i][1] if i >= 0 else {"valid": False}

    def _show_frame(self, bgr: np.ndarray) -> None:
        self._show_qimage(self._qimage_from_bgr(bgr))

    def _show_qimage(self, img: QImage) -> None:
        pix = QPixmap.fromImage(img).scaled(
            self.video.size(), Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        self.video.setPixmap(pix)

    @staticmethod
    def _qimage_from_bgr(bgr: np.ndarray) -> QImage:
        rgb = cv2.cvtColor(np.ascontiguousarray(bgr), cv2.COLOR_BGR2RGB)
        h, w, _ = rgb.shape
        return QImage(rgb.data, w, h, 3 * w, QImage.Format.Format_RGB888).copy()

    @staticmethod
    def _square_crop_bgr(frame: np.ndarray) -> np.ndarray:
        h, w = frame.shape[:2]
        if h <= 0 or w <= 0 or h == w:
            return np.ascontiguousarray(frame)
        if w > h:
            left = max(0, (w - h) // 2)
            return np.ascontiguousarray(frame[:, left:left + h, :])
        top = max(0, (h - w) // 2)
        return np.ascontiguousarray(frame[top:top + w, :, :])

    def _pilot_tab_qimage(
        self,
        source_bgr: np.ndarray,
        estimate: TransectEstimate,
        observation: Optional[TransectObservation],
    ) -> QImage:
        display_bgr = self._square_crop_bgr(source_bgr)
        img = self._qimage_from_bgr(display_bgr)
        painter = QPainter(img)
        try:
            paint_transect_hud_overlay(
                painter,
                img.rect(),
                self.model,
                estimate,
                observation,
                source_bgr.shape,
            )
        finally:
            painter.end()
        return img

    def _export_frame(self) -> None:
        if self.cur_bgr is None:
            return
        out = f"transect_review_frame_{self.cur_frame:05d}.png"
        est, obs = self._current_overlay_state()
        if self.view_mode == "pilot":
            self._pilot_tab_qimage(self.cur_bgr, est, obs).save(out)
        else:
            frame = self.cur_bgr.copy()
            draw_transect_overlay(frame, self.model, est, obs)
            cv2.imwrite(out, frame)
        self.status.setText(f"saved {out}")

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.cur_bgr is not None:
            self._render()


def main() -> None:
    ap = argparse.ArgumentParser(description="Transect session review applet")
    ap.add_argument("session", nargs="?", help="recordings/<session> dir or an mp4")
    args = ap.parse_args()
    app = QApplication(sys.argv)
    win = ReviewWindow(args.session)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
