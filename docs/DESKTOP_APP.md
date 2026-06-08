# Desktop App Build

TritonPilot can be packaged as a Windows desktop app with PyInstaller. There
are two useful formats.

The default one-folder build starts faster but must be kept together:

```text
dist\TritonPilot\TritonPilot.exe
dist\TritonPilot\_internal\
```

The single-file build is the one to copy to the Desktop or a USB drive by
itself:

```text
dist\TritonPilot.exe
```

## Build

Run this from the repository root on the pilot computer:

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\build_windows_app.ps1 -Clean
```

Build the independent single-file app:

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\build_windows_app.ps1 -Clean -OneFile
```

The build script:

- Reuses `.venv`, or creates it through `setup_windows.ps1 -SkipSystemDeps`
- Installs runtime dependencies plus `requirements-build.txt`
- Generates the golden trident app icon under `assets\`
- Runs PyInstaller using `deploy\tritonpilot.spec`
- Produces either `dist\TritonPilot\TritonPilot.exe` or `dist\TritonPilot.exe`

To add a desktop shortcut after building:

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\build_windows_app.ps1 -CreateDesktopShortcut
```

For a desktop shortcut to the single-file app:

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\build_windows_app.ps1 -OneFile -CreateDesktopShortcut
```

The one-folder build is usually better for the pilot laptop once installed. The
single-file build is more portable, but it has to unpack itself into a temporary
directory at launch, so startup can be a little slower.

## Runtime Notes

The bundled app includes the Python GUI code and `data\streams.json`. It still
expects the pilot laptop to have the Windows GStreamer runtime installed so the
Direct3D video receive pipelines can launch `gst-launch-1.0`.

The app opens maximized by default for pilot use. Press `F11` or use
`View > Full Screen` for true full-screen mode; press `F11` or `Esc` to return
to maximized mode. For development or bench testing, launch with `--windowed`
or set:

```powershell
$env:TRITON_START_MAXIMIZED="0"
```

True full-screen can still be forced with `--fullscreen` or
`TRITON_START_FULLSCREEN=1`.

If video does not start on a fresh laptop, run:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_windows.ps1
```

## Recordings

The default recording root is now operator-visible and outside the checkout:

```text
%USERPROFILE%\Documents\TritonPilot\Recordings
```

Operators can still change the folder from `Record > Set Save Directory...`.
Use `Record > Use Default Recordings Folder` to return to the app default.

Developers can override paths without rebuilding:

```powershell
$env:TRITON_RECORDINGS_DIR="D:\TritonPilotRecordings"
$env:TRITON_STREAMS_FILE="C:\field-configs\streams.json"
```
