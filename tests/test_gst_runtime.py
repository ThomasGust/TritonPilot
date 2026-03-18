from pathlib import Path

from video.gst_runtime import bootstrap_gstreamer_env, find_gstreamer_runtime


def _make_fake_runtime(root: Path) -> Path:
    bin_dir = root / "bin"
    libexec_dir = root / "libexec" / "gstreamer-1.0"
    plugin_dir = root / "lib" / "gstreamer-1.0"

    bin_dir.mkdir(parents=True)
    libexec_dir.mkdir(parents=True)
    plugin_dir.mkdir(parents=True)

    (bin_dir / "gst-launch-1.0.exe").write_text("", encoding="ascii")
    (bin_dir / "gst-inspect-1.0.exe").write_text("", encoding="ascii")
    (libexec_dir / "gst-plugin-scanner.exe").write_text("", encoding="ascii")
    (plugin_dir / "gstcoreelements.dll").write_text("", encoding="ascii")
    return root


def test_find_gstreamer_runtime_from_root_env(tmp_path):
    root = _make_fake_runtime(tmp_path / "gst")
    env = {
        "GSTREAMER_1_0_ROOT_MSVC_X86_64": str(root),
        "PATH": r"C:\Windows\System32",
    }

    runtime = find_gstreamer_runtime(
        env=env,
        command_lookup=lambda _name: None,
        registry_roots=[],
    )

    assert runtime is not None
    assert runtime.root == root
    assert runtime.gst_launch == root / "bin" / "gst-launch-1.0.exe"
    assert runtime.gst_inspect == root / "bin" / "gst-inspect-1.0.exe"
    assert runtime.plugin_scanner == root / "libexec" / "gstreamer-1.0" / "gst-plugin-scanner.exe"


def test_bootstrap_gstreamer_env_sets_expected_values_once(tmp_path):
    root = _make_fake_runtime(tmp_path / "gst")
    env = {
        "GST_LAUNCH": str(root / "bin" / "gst-launch-1.0.exe"),
        "PATH": r"C:\Windows\System32",
    }

    runtime = bootstrap_gstreamer_env(env)
    assert runtime is not None
    assert env["GST_LAUNCH"] == str(root / "bin" / "gst-launch-1.0.exe")
    assert env["GSTREAMER_1_0_ROOT_MSVC_X86_64"] == str(root)
    assert env["GST_PLUGIN_SCANNER"] == str(root / "libexec" / "gstreamer-1.0" / "gst-plugin-scanner.exe")
    assert env["GST_PLUGIN_SYSTEM_PATH_1_0"] == str(root / "lib" / "gstreamer-1.0")

    first_path = env["PATH"]
    assert first_path.split(";")[0] == str(root / "bin")

    runtime = bootstrap_gstreamer_env(env)
    assert runtime is not None
    assert env["PATH"] == first_path
