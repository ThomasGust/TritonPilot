# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

repo_root = Path(SPECPATH).parent
icon_path = repo_root / "assets" / "tritonpilot_icon.ico"

datas = [
    (str(repo_root / "assets" / "tritonpilot_icon.ico"), "assets"),
    (str(repo_root / "assets" / "tritonpilot_icon.png"), "assets"),
    (str(repo_root / "data" / "streams.json"), "data"),
]

hiddenimports = [
    "cv2",
    "pygame",
    "zmq",
    "paramiko",
]

a = Analysis(
    [str(repo_root / "main_topside.py")],
    pathex=[str(repo_root)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "tests"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="TritonPilot",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(icon_path),
)
