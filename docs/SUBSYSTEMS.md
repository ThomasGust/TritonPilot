# TritonPilot Subsystem Reference

This guide maps repository areas to responsibilities so maintainers can make
changes without blurring the boundary between piloting, onboard control, and
mission analysis.

## Entry Points

- `main_topside.py` starts the production PyQt GUI.
- `input/pilot_publisher.py` is a lower-level controller-to-ZMQ publisher
  utility.
- `tools/` scripts provide focused diagnostics and media helpers.

## `config.py`

`config.py` centralizes runtime defaults for endpoints, controller tuning,
pilot modes, reverse drive, gains, video timeouts, water correction, and stream
configuration. Environment variables override most values so field changes do
not require code edits.

Keep operator-tunable constants here when they affect multiple UI/services.
Keep ROV hardware constants in TritonOS.

## `gui/`

The GUI package owns PyQt widgets and user interaction:

- `main_window.py` composes the application and status bar.
- `video_tabs.py` and `video_widget.py` display and control camera panes.
- `sensor_panel.py`, `instruments.py`, and `raw_sensor_page.py` display
  telemetry and diagnostic values.
- `management_page.py` wraps ROV setup/calibration commands.
- `responsive.py` and `style.py` hold shared layout/style helpers.

Widgets should be presentation-focused. Long-running I/O should live in service
objects or workers.

## `input/`

The input package converts physical controller state into a stable command
schema:

- `controller.py` handles pygame/SDL joystick discovery, mapping, deadzones,
  snapshots, and fallback heuristics.
- `pilot_service.py` polls the controller, tracks pilot-side modes, and
  publishes `PilotFrame` messages.
- `pilot_publisher.py` is a CLI utility for direct publisher testing.

This package should not know about thruster layout or hardware PWM. It only
publishes operator intent.

## `schema/`

`schema/pilot_common.py` defines the JSON wire shape shared with TritonOS:

- `PilotAxes`
- `PilotButtons`
- `PilotFrame`

Any schema change must be coordinated with TritonOS tests and docs because it
affects the live control contract.

## `telemetry/`

Telemetry code receives and interprets ROV sensor messages:

- `sensor_service.py` subscribes to the TritonOS sensor stream.
- `roll_pitch_estimator.py` provides a topside fallback attitude estimator for
  display, logging, and replay diagnostics.

Telemetry display code can derive operator-friendly values, but authoritative
control-state decisions should remain onboard.

## `video/`

The video package owns camera stream control and local receive processing:

- `rov_streams.py` talks to the TritonOS video RPC service.
- `cam.py` manages remote camera state and OpenCV-style frames.
- `gst_receiver.py` launches and monitors GStreamer receive processes.
- `gst_runtime.py` discovers and bootstraps the Windows GStreamer runtime.
- `frame_rotation.py` normalizes configured rotation values.
- `frame_correction.py` applies optional water/lens correction.

Keep stream metadata in `data/streams.json` and keep onboard capture behavior
in TritonOS.

## `recording/`

Recording helpers write pilot-side artifacts:

- `save_location.py` resolves an available destination.
- `capture_paths.py` builds safe timestamped names.
- `video_recorder.py` writes video files and snapshots.
- `stream_recorder.py` writes JSONL event logs.
- `raw_sensor_csv.py` writes spreadsheet-friendly telemetry rows.

Avoid putting analysis logic here. The goal is to preserve data clearly for
later interpretation.

## `network/`

Network helpers isolate endpoint and socket details:

- `management_rpc.py` implements management REQ/REP clients and the GUI worker
  service.
- `net_select.py` chooses a local receive interface for video.
- `zmq_hotplug.py` applies socket options that make reconnects less fragile.

Any new network path should document its port, direction, timeout behavior, and
which computer owns the server.

## `tools/`

Tool scripts are intentionally direct and practical:

- Controller probes inspect joystick mappings.
- Sensor subscriber tools verify telemetry without the full GUI.
- Network diagnostics measure tether health.
- Water-correction helpers process recorded media; RealityScan reconstruction
  lives in the sibling TritonAnalysis checkout.
- Tether NAT setup helps the pilot computer share internet with the ROV.

Tools may be more specialized than library code, but they should still avoid
silently changing live vehicle state unless that is the explicit purpose of the
tool.

## `data/`

`data/streams.json` is the pilot-side camera layout. It names the camera panes,
UDP ports, device hints, codec settings, rotation, and default pane order.

Keep this file synchronized with TritonOS video configuration before field
tests.

## `tests/`

Tests are designed to run without physical hardware. They cover schema
round-trips, publisher/subscriber behavior, GUI layout behavior, recording,
configuration, frame utilities, raw sensor display, and fallback attitude
estimation.

Hardware checks belong in tools and should be run deliberately.
