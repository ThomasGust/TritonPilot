# TritonPilot

Topside control, video, recording, and telemetry code for the TritonPilot ROV project.

Mac setup for analysis applets:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-macos.txt
```

Standalone crab competition analyzer:
`python -m analysis.main_crab_detection [image-folder-or-video ...]`

The analyzer can run a photo directly, scrub to a selected video frame, or scan a
video time range and show the best frame with species labels, masks, and counts.

Standalone iceberg tracking threat applet:
`python -m analysis.main_iceberg_tracking`

Standalone coral garden CAD model applet:
`python -m analysis.main_coral_garden_model`

Standalone eDNA frequency analysis applet:
`python -m analysis.main_edna_analysis`

Standalone iceberg measurement applet:
`python -m analysis.main_iceberg_measurement`

Standalone planar height measurement applet:
`python -m analysis.main_planar_height_measurement`

Standalone multi-rectangle length measurement applet:
`python -m analysis.main_multi_rect_length_measurement`

Underwater color correction / frame export applet:
`python -m analysis.color_corr`

The analysis applets live under `analysis/` so a competition-day laptop can find
the task-specific tools without digging through the pilot interface. The old
top-level launcher names still forward to these modules for compatibility.
The Qt applets size themselves to the active display and put wide toolbars in
scrollable strips, which keeps the controls reachable on smaller Mac laptop
screens as well as larger Windows monitors.
