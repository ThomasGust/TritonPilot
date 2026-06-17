# TritonPilot Operations Guide

This guide describes the normal pilot-station workflow. It assumes TritonOS is
already installed on the ROV and the tether network is configured.

## Preflight

On the pilot computer:

1. Connect the tether.
2. Connect the controller.
3. Confirm the recording drive or folder is available.
4. Activate the TritonPilot virtual environment.
5. Set `ROV_HOST` if the ROV is not at `192.168.1.4`.

On the ROV:

1. Confirm TritonOS is running.
2. Confirm sensors and camera devices are present.
3. Keep the vehicle disarmed until the pilot station is verified.

## Start TritonPilot

From the TritonPilot repository root:

```powershell
.\.venv\Scripts\activate
python .\main_topside.py
```

The window should open with pilot, reverse-drive, hold-test, raw-sensor, and
vehicle-setup pages available. The status bar is the quickest health check.

## Verify Control

Before arming:

- Confirm the controller is detected.
- Confirm neutral sticks send near-zero command values.
- Confirm the displayed max gain is appropriate.
- Confirm reverse drive is off unless intentionally being used.
- Confirm arm/disarm backup controls are understood by the pilot.

Default controls include:

- Right stick press toggles depth hold.
- Left stick press toggles yaw hold.
- Left bumper toggles reverse drive.
- Keyboard `R` toggles reverse drive.
- Keyboard `L` sends the lights edge.
- Keyboard `O` sends the arm/disarm edge.
- `Y`/`A` on the controller and keyboard `+`/`-` adjust the main ROV motion gain.
- Keyboard `1`/`2` adjust the back rotating gripper gain.
- Keyboard `6`/`7` adjust the arm gain.
- `W`, `S`, `A`, and `D` provide trigger-like keyboard backup manipulator controls.

The pilot-side telemetry column shows three live gain indicators: `BACK`,
`ROV`, and `ARM`.

Controller mappings can vary by operating system and driver. Use
`tools/controller_probe.py` when the observed behavior does not match the
expected layout.

## Verify Telemetry

The sensor panel and status bar should update continuously. The Raw Sensors
page shows live IMU, magnetometer, depth, environment, ADC, power, leak, and
attitude values when they are available.

The Raw Sensors page includes a topside fallback roll/pitch/yaw estimator for
visualization and logging. The onboard TritonOS estimator is authoritative when
it is present in telemetry.

## Verify Video

Video tabs are backed by `data/streams.json`. The default pane order is:

1. Primary Camera
2. Aux Camera
3. Arm Camera
4. Back Gripper Camera

If a camera is missing or rotated incorrectly, fix the stream definition before
competition instead of relying on the operator to remember a special case.

Use reverse drive when the pilot is intentionally driving from a rear-facing
view. Reverse drive flips translation commands so stick direction matches the
camera perspective while yaw remains normal. Press `R` to toggle reverse drive
from the main pilot view.

## Data Logging

Use the GUI recording controls for data logs:

- Pilot/sensor JSONL stream logs
- Raw sensor CSV time series

Logs should go into the selected session directory. If the preferred save
location is unavailable, `recording/save_location.py` falls back to the default
recordings directory in `Documents\TritonPilot\Recordings`.

Media capture is currently disabled in TritonPilot. TritonAnalysis should work
from saved logs or manually entered task data until the media pipeline is rebuilt.

## Management Page

The Vehicle Setup page uses the management RPC endpoint to inspect and adjust
ROV-side configuration. Treat it as a live vehicle tool:

- Avoid changing calibration values while another operator is piloting.
- Confirm the ROV is in a safe state before actuator or calibration actions.
- If a request times out, check the TritonOS service before repeating commands.

## Raw Sensor Logging

Use `Recording > Start Stream Log` for full JSONL capture of pilot and sensor
messages. On the Raw Sensors page, `Start Raw CSV` writes
`raw_sensor_timeseries.csv` in the active recording session for spreadsheet
review.

A terminal-only telemetry capture is available:

```powershell
python .\tools\sensor_stream_sub_test.py --endpoint tcp://192.168.1.4:6001 --jsonl raw_sensors.jsonl
```

## Shutdown

At the end of a run:

1. Disarm the ROV.
2. Stop video and stream logging.
3. Confirm logs flushed to disk.
4. Close TritonPilot.
5. Copy or move mission media to the analysis computer as needed.
6. Stop or power down TritonOS only after the ROV is safe.
