import os
import subprocess
import sys

from video.gst_receiver import _suppress_gst_stderr_line


def test_controller_import_suppresses_pygame_startup_chatter():
    env = dict(os.environ)
    env.pop("PYGAME_HIDE_SUPPORT_PROMPT", None)

    proc = subprocess.run(
        [sys.executable, "-c", "import input.controller"],
        cwd=os.getcwd(),
        env=env,
        text=True,
        capture_output=True,
        timeout=10.0,
    )

    combined = f"{proc.stdout}\n{proc.stderr}"
    assert proc.returncode == 0
    assert "Hello from the pygame community" not in combined
    assert "pkg_resources is deprecated as an API" not in combined


def test_gstpython_plugin_scan_warning_is_filtered():
    assert _suppress_gst_stderr_line("")
    assert _suppress_gst_stderr_line(
        "GStreamer-WARNING **: Failed to load plugin 'C:\\gstreamer\\lib\\gstreamer-1.0\\gstpython.dll'"
    )
    assert _suppress_gst_stderr_line("This usually means Windows was unable to find a DLL dependency of the plugin.")
    assert _suppress_gst_stderr_line("Please check that PATH is correct.")
    assert _suppress_gst_stderr_line("You can run 'dumpbin -dependents' to list DLL deps.")
    assert not _suppress_gst_stderr_line("ERROR: from element udpsrc0: Internal data stream error.")
