# TritonPilot Testing And Troubleshooting

The TritonPilot test suite is designed to run without physical ROV hardware.
Use it before field work and after changing shared behavior.

## Run The Quick Trust Check

From the TritonPilot repository root:

```powershell
.\.venv\Scripts\activate
python -m pytest
```

If `pytest` is not installed:

```powershell
python -m pip install -r requirements-dev.txt
```

`pytest.ini` sets `tests/` as the test root, quiet output, and strict marker
validation. The default suite is the fast trust check: it skips tests that are
marked `network`, `hardware`, `slow`, or `groundtruth` so a normal code-quality
check does not depend on sockets, real ROV services, physical hardware, or large
local datasets.

The helper script exposes the same tiers with easier names:

```powershell
python .\tools\trust_check.py quick
python .\tools\trust_check.py network
python .\tools\trust_check.py extended
python .\tools\trust_check.py hardware
python .\tools\trust_check.py full
```

Equivalent direct pytest commands:

| Goal | Command |
| --- | --- |
| Fast software-only check | `python -m pytest` |
| Local socket/ZMQ tests | `python -m pytest --run-network -m network` |
| All non-hardware optional tiers | `python -m pytest --run-extended` |
| Physical ROV/hardware tests | `python -m pytest --run-hardware -m hardware` |
| Everything | `python -m pytest --run-all-trust` |
| Coverage report, if `pytest-cov` is installed | `python .\tools\trust_check.py coverage` |

Environment variables work for CI or shell profiles:

- `TRITON_RUN_NETWORK=1`
- `TRITON_RUN_GROUNDTRUTH=1`
- `TRITON_RUN_SLOW=1`
- `TRITON_RUN_HARDWARE=1`

## Focused Test Areas

Run a single test file while working on a subsystem:

```powershell
python -m pytest tests\test_pilot_publisher_pubsub.py
python -m pytest tests\test_sensor_subscriber_pubsub.py
python -m pytest tests\test_raw_sensor_page.py
python -m pytest tests\test_frame_correction.py
python -m pytest tests\test_gst_runtime.py
```

The tests cover:

- PilotFrame schema round-trips
- Controller publishing behavior
- Reverse-drive behavior
- Sensor subscriber pub/sub behavior
- Raw sensor page display and CSV logging
- Fallback roll/pitch/yaw estimation
- GStreamer runtime discovery
- Video tab layout behavior
- Recording paths and writer helpers
- Main-window backup controls

## Test Marker Policy

Use the default suite for deterministic tests that can run on any developer
machine. Mark new tests when they leave that boundary:

- `network`: opens sockets, uses ZMQ over TCP, or depends on active networking.
- `hardware`: touches physical ROV hardware or live system services.
- `slow`: intentionally takes long enough that it should not block quick checks.
- `groundtruth`: depends on optional saved media or datasets outside the normal
  repository fixtures.
- `integration`: crosses module/service boundaries but remains deterministic and
  hardware-free.

## Hardware-Free Diagnostics

Controller mapping:

```powershell
python .\tools\controller_probe.py
```

Sensor telemetry subscription:

```powershell
python .\tools\sensor_stream_sub_test.py --endpoint tcp://192.168.1.4:6001
```

Tether diagnostics:

```powershell
python .\tools\netdiag_client.py --host 192.168.1.4
```

Water correction preview:

```powershell
python .\tools\preview_water_correction.py path\to\frame.jpg
```

## Common Problems

The GUI does not open:

- Confirm the virtual environment is active.
- Reinstall dependencies with `python -m pip install -r requirements.txt`.
- Confirm PyQt6 imports in the same shell.

The controller is not detected:

- Reconnect the controller before launching TritonPilot.
- Run `tools/controller_probe.py`.
- Set `TRITON_CONTROLLER_INDEX` if another joystick is selected.
- Force `TRITON_CONTROLLER_AXIS_MAP` if axes are wrong.

Telemetry is missing:

- Confirm TritonOS is running.
- Confirm `ROV_HOST`.
- Confirm port `6001` is reachable.
- Run `tools/sensor_stream_sub_test.py`.

Video is missing:

- Confirm `gst-launch-1.0` is installed and discoverable.
- Run `setup_windows.ps1` to validate GStreamer elements.
- Confirm port `5555` is reachable.
- Allow inbound UDP camera ports through the firewall.
- Confirm `data/streams.json` matches the ROV camera layout.

Management actions time out:

- Confirm port `5556` is reachable.
- Check TritonOS logs.
- Wait for one management action to finish before issuing another.

Recordings are missing:

- Confirm the selected save directory exists and is writable.
- Check `Documents\TritonPilot\Recordings`, the default recordings directory.
- Stop recording before closing the app so files flush cleanly.

## Debugging Order

When multiple things fail, use this order:

1. Confirm the ROV is powered and TritonOS is running.
2. Confirm tether addressing and `ROV_HOST`.
3. Confirm telemetry on port `6001`.
4. Confirm management RPC on port `5556`.
5. Confirm video RPC on port `5555`.
6. Confirm inbound UDP camera ports.
7. Confirm controller mapping.
8. Start the full GUI.

This order separates network and service problems from GUI behavior.
