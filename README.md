# TritonPilot

Topside control, video, recording, and telemetry code for the TritonPilot ROV project.

Mission-specific analysis applets are intentionally not part of this repo. They
live in the sibling `TritonAnalysis` repository and run on the competition-day
analysis laptop against saved images, video files, or manually entered task
data.

## Raw Sensor Bringup

The main topside app includes a `Raw Sensors` page for live IMU/depth/power
inspection. It shows rolling accel/gyro plots, separate AK09915 and MMC5983
magnetometer plots when both are available, and flattened live values for
depth, env, ADC, power, and leak telemetry.

The same page also runs a diagnostic topside roll/pitch estimator. It
calibrates its zero from the current rest pose, subtracts the observed gyro
bias, and publishes/logs `attitude` rows for visualization only. This is not
connected to vehicle control.

Use `Recording > Start Stream Log` for full JSONL capture of pilot and sensor
messages. On the `Raw Sensors` page, `Start Raw CSV` writes
`raw_sensor_timeseries.csv` in the current recording session for quick plotting
in spreadsheet tools. Raw sensor rows and derived roll/pitch `attitude` rows
share the same time-series file.

For a terminal-only sensor check:

```sh
python tools/sensor_stream_sub_test.py --endpoint tcp://<rov-ip>:6001 --jsonl raw_sensors.jsonl
```

## Tether Internet Routing

The pilot computer can act as the ROV's internet gateway over the tether. The
normal layout is:

- pilot Wi-Fi: internet-facing network
- pilot tether adapter: `192.168.1.1/24`
- ROV `eth0`: `192.168.1.4/24`

Probe the Windows side without admin:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\setup_tether_nat.ps1 -ProbeOnly
```

Configure or repair the Windows NAT path from an elevated PowerShell:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\setup_tether_nat.ps1 -TuneAdapter -ResetAdapter
```

Then, on the Pi, only switch the default route after the tether gateway answers:

```bash
sudo bash bin/configure_tether_gateway.sh --probe
sudo bash bin/configure_tether_gateway.sh --persistent
```
