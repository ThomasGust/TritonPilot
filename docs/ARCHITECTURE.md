# TritonPilot Architecture

TritonPilot is a topside application with a GUI shell and several small service
objects. The GUI owns operator interaction; background services own controller,
telemetry, RPC, and video receive work so the interface can stay responsive.

## System Boundary

TritonPilot owns:

- Pilot station GUI and status display
- Controller input
- Pilot command publication
- Sensor telemetry display and logging
- Camera stream control and local video receive
- Stream and raw sensor data logging
- Management RPC client tools

TritonPilot does not own:

- Thruster mixing
- Hardware PWM output
- Sensor drivers
- Camera capture on the ROV
- Mission-specific scoring or analysis

Those are split between TritonOS and TritonAnalysis.

## Startup Flow

`main_topside.py` creates a `QApplication`, applies the shared GUI style, and
shows `gui.main_window.MainWindow`.

`MainWindow` then creates:

- `PilotPublisherService` for controller polling and PilotFrame publishing
- `SensorSubscriberService` for telemetry subscription
- `ROVStreams` and the remote camera manager for video RPC and stream metadata
- `VideoTabs` and `VideoWidget` instances for display
- `SensorPanel`, `InstrumentPanel`, `HoldTestPanel`, and `RawSensorPage`
- `ManagementRpcService` and `ManagementPage`
- Recording helpers for stream logs and CSV telemetry

Most UI updates are delivered through Qt signals so background threads do not
directly mutate widgets.

## Control Flow

```text
pygame controller
        |
        v
input.controller.GamepadSource
        |
        v
input.pilot_service.PilotPublisherService
        |
        v
schema.pilot_common.PilotFrame
        |
        v
ZeroMQ PUB tcp://<ROV_HOST>:6000
        |
        v
TritonOS PilotReceiver
```

`PilotPublisherService` also tracks operator modes such as depth hold,
roll/pitch leveling requests, yaw hold, reverse drive, gain changes, light
edges, arm/disarm edges, and manipulator auxiliary axes.

The ROV decides what to do with those modes. TritonPilot packages operator
intent; TritonOS owns final control authority.

## Telemetry Flow

```text
TritonOS SensorPublisher
        |
        v
ZeroMQ SUB tcp://<ROV_HOST>:6001
        |
        v
telemetry.sensor_service.SensorSubscriberService
        |
        v
GUI panels, status bar, stream recorder, raw CSV logger
```

Telemetry is displayed in both summary panels and raw diagnostic views. The Raw
Sensors page can derive a local attitude estimate for visualization when
onboard attitude telemetry is missing, but onboard telemetry wins when present.

## Video Flow

```text
GUI video tab
        |
        v
video.rov_streams.ROVStreams start_stream RPC
        |
        v
TritonOS video service
        |
        v
RTP/UDP video to pilot port
        |
        v
video.gst_receiver ReceiverProcess
        |
        v
video.cam RemoteCv2Camera / RemoteCameraManager
        |
        v
gui.video_widget.VideoWidget
```

The stream definitions in `data/streams.json` are shared expectations between
the pilot UI and the onboard video service. Names, ports, rotation, resolution,
codec, and device hints should match the ROV configuration.

## Management RPC Flow

Management calls use a ZeroMQ REQ/REP client in `network/management_rpc.py`.
`ManagementRpcService` serializes calls on a worker thread so GUI actions do
not block the event loop.

Use management RPC for operator setup and calibration tasks, not for high-rate
control. High-rate control belongs in the PilotFrame stream.

## Data Logging Flow

Data logging is intentionally local to the pilot computer:

- `recording/stream_recorder.py` writes JSONL event streams.
- `recording/raw_sensor_csv.py` flattens raw telemetry into CSV rows.
- `recording/save_location.py` resolves the active save directory.

Saved stream logs and raw CSV files are the handoff point to TritonAnalysis.
Media capture has been removed from this baseline so it can be rebuilt without
entangling the live display path.

## Threading Model

Qt runs the main GUI event loop. Controller polling, sensor subscription,
management RPC, and video receive work run outside the GUI thread. Any future
subsystem should follow the same pattern: keep blocking I/O out of widgets and
deliver state back through signals or a small service boundary.
