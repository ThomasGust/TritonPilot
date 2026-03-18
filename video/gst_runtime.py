from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from shutil import which
from typing import Iterable, Mapping


_ROOT_ENV_NAMES = (
    "GSTREAMER_1_0_ROOT_MSVC_X86_64",
    "GSTREAMER_1_0_ROOT_X86_64",
    "GSTREAMER_ROOT_X86_64",
    "GSTREAMER_ROOT",
    "GST_ROOT",
)

_PROCESS_ENV_KEYS = (
    "GST_LAUNCH",
    "GST_PLUGIN_SCANNER",
)


@dataclass(frozen=True)
class GStreamerRuntime:
    root: Path
    bin_dir: Path
    gst_launch: Path
    gst_inspect: Path | None
    plugin_scanner: Path | None
    plugin_dir: Path | None

    def apply_to_env(self, env: dict[str, str]) -> None:
        env["GST_LAUNCH"] = str(self.gst_launch)
        env["GSTREAMER_1_0_ROOT_MSVC_X86_64"] = str(self.root)

        path_parts = [p for p in env.get("PATH", "").split(os.pathsep) if p]
        lowered = {p.lower() for p in path_parts}
        bin_dir = str(self.bin_dir)
        if bin_dir.lower() not in lowered:
            path_parts.insert(0, bin_dir)
            env["PATH"] = os.pathsep.join(path_parts)

        if self.plugin_scanner is not None:
            env["GST_PLUGIN_SCANNER"] = str(self.plugin_scanner)

        plugin_system_path = env.get("GST_PLUGIN_SYSTEM_PATH_1_0")
        if self.plugin_dir is not None and (not plugin_system_path or not Path(plugin_system_path).exists()):
            env["GST_PLUGIN_SYSTEM_PATH_1_0"] = str(self.plugin_dir)


def _existing_path(path: str | Path | None) -> Path | None:
    if not path:
        return None
    candidate = Path(path)
    if candidate.exists():
        return candidate
    return None


def _iter_env_roots(env: Mapping[str, str]) -> Iterable[Path]:
    for key in _PROCESS_ENV_KEYS:
        value = env.get(key)
        candidate = _existing_path(value)
        if candidate is None:
            continue
        if candidate.name.lower().startswith("gst-launch-1.0"):
            yield candidate.parent.parent
        elif candidate.name.lower() == "gst-plugin-scanner.exe":
            yield candidate.parent.parent.parent

    for key in _ROOT_ENV_NAMES:
        candidate = _existing_path(env.get(key))
        if candidate is not None:
            yield candidate


def _iter_registry_env_roots() -> Iterable[Path]:
    if os.name != "nt":
        return

    # Read user/machine environment directly so installs from a later shell session
    # are still discoverable in the already-running app.
    try:
        import winreg
    except ImportError:
        return

    locations = (
        (winreg.HKEY_CURRENT_USER, r"Environment"),
        (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
    )
    for hive, subkey in locations:
        try:
            reg_key = winreg.OpenKey(hive, subkey)
        except OSError:
            continue
        with reg_key:
            for key in _ROOT_ENV_NAMES:
                try:
                    value, _value_type = winreg.QueryValueEx(reg_key, key)
                except OSError:
                    continue
                candidate = _existing_path(value)
                if candidate is not None:
                    yield candidate


def _iter_common_roots(env: Mapping[str, str]) -> Iterable[Path]:
    candidates = [
        Path(r"C:\gstreamer\1.0\msvc_x86_64"),
        Path(r"C:\gstreamer\1.0\mingw_x86_64"),
    ]

    program_files = env.get("ProgramFiles")
    if program_files:
        pf = Path(program_files)
        candidates.extend(
            [
                pf / "GStreamer" / "1.0" / "msvc_x86_64",
                pf / "gstreamer" / "1.0" / "msvc_x86_64",
                pf / "GStreamer" / "1.0" / "mingw_x86_64",
                pf / "gstreamer" / "1.0" / "mingw_x86_64",
            ]
        )

    local_app_data = env.get("LOCALAPPDATA")
    if local_app_data:
        lad = Path(local_app_data)
        candidates.extend(
            [
                lad / "Programs" / "GStreamer" / "1.0" / "msvc_x86_64",
                lad / "Programs" / "gstreamer" / "1.0" / "msvc_x86_64",
                lad / "Programs" / "GStreamer" / "1.0" / "mingw_x86_64",
                lad / "Programs" / "gstreamer" / "1.0" / "mingw_x86_64",
            ]
        )

    for candidate in candidates:
        if candidate.exists():
            yield candidate


def _runtime_from_root(root: Path) -> GStreamerRuntime | None:
    bin_dir = root / "bin"
    gst_launch = bin_dir / "gst-launch-1.0.exe"
    if not gst_launch.exists():
        gst_launch = bin_dir / "gst-launch-1.0"
    if not gst_launch.exists():
        return None

    gst_inspect = bin_dir / "gst-inspect-1.0.exe"
    if not gst_inspect.exists():
        gst_inspect = bin_dir / "gst-inspect-1.0"
    if not gst_inspect.exists():
        gst_inspect = None

    plugin_scanner = root / "libexec" / "gstreamer-1.0" / "gst-plugin-scanner.exe"
    if not plugin_scanner.exists():
        plugin_scanner = None

    plugin_dir = root / "lib" / "gstreamer-1.0"
    if not plugin_dir.exists():
        plugin_dir = None

    return GStreamerRuntime(
        root=root,
        bin_dir=bin_dir,
        gst_launch=gst_launch,
        gst_inspect=gst_inspect,
        plugin_scanner=plugin_scanner,
        plugin_dir=plugin_dir,
    )


def _iter_candidate_roots(
    env: Mapping[str, str],
    command_lookup,
    registry_roots: Iterable[Path] | None,
) -> Iterable[Path]:
    seen: set[str] = set()

    def emit(path: Path | None) -> Iterable[Path]:
        if path is None:
            return ()
        key = str(path).lower()
        if key in seen:
            return ()
        seen.add(key)
        return (path,)

    command_path = command_lookup("gst-launch-1.0") or command_lookup("gst-launch-1.0.exe")
    if command_path:
        yield from emit(Path(command_path).parent.parent)

    for root in _iter_env_roots(env):
        yield from emit(root)

    if registry_roots is None:
        for root in _iter_registry_env_roots():
            yield from emit(root)
    else:
        for root in registry_roots:
            yield from emit(root)

    for root in _iter_common_roots(env):
        yield from emit(root)


def find_gstreamer_runtime(
    env: Mapping[str, str] | None = None,
    *,
    command_lookup=which,
    registry_roots: Iterable[Path] | None = None,
) -> GStreamerRuntime | None:
    env_map = dict(os.environ if env is None else env)
    for root in _iter_candidate_roots(env_map, command_lookup, registry_roots):
        runtime = _runtime_from_root(root)
        if runtime is not None:
            return runtime
    return None


def bootstrap_gstreamer_env(env: dict[str, str] | None = None) -> GStreamerRuntime | None:
    runtime = find_gstreamer_runtime(env=env)
    if runtime is None:
        return None

    target_env = os.environ if env is None else env
    runtime.apply_to_env(target_env)
    return runtime
