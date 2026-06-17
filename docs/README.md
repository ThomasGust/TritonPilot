# TritonPilot Documentation

This folder is the maintained documentation set for TritonPilot. It is meant to
replace scattered notes and wiki pages with repository-local Markdown that
stays close to the code.

## Guides

- [Setup Guide](SETUP.md) - Install Python dependencies, GStreamer, and the
  Windows helper environment needed to run the topside app.
- [Desktop App Build](DESKTOP_APP.md) - Build the Windows desktop executable
  bundle and golden trident icon for pilot handoff.
- [Network Guide](NETWORKING.md) - Tether addressing, ZeroMQ endpoints, video
  ports, Windows NAT routing, firewalls, and diagnostics.
- [Pilot Tether Adapter Setup](PILOT_TETHER_SETUP.md) - Configure the Windows
  USB/Ethernet adapter for the live ROV tether once the adapter is connected.
- [Analysis Transfer Link](ANALYSIS_TRANSFER.md) - Optional read-only
  USB-Ethernet handoff from TritonPilot recordings to TritonAnalysis.
- [Operations Guide](OPERATIONS.md) - Pilot-station startup, preflight,
  controller use, recording, raw sensor checks, management tools, and shutdown.
- [Architecture Overview](ARCHITECTURE.md) - How GUI, controller, telemetry,
  video, recording, and RPC services fit together.
- [Media Capture Architecture](MEDIA_CAPTURE_ARCHITECTURE.md) - Display,
  recording, and stereo capture timing boundaries.
- [Subsystem Reference](SUBSYSTEMS.md) - What each package/module owns and how
  maintainers should extend the code.
- [Configuration Guide](CONFIGURATION.md) - Environment variables,
  `data/streams.json`, controller mapping, video correction, and save paths.
- [Stereo Capture](STEREO_CAPTURE.md) - Stereo pair configuration, calibration
  capture sessions, manifest format, and quality checklist.
- [Testing And Troubleshooting](TESTING_AND_TROUBLESHOOTING.md) - Unit tests,
  focused diagnostics, and common failure symptoms.

## Related Repositories

- `TritonOS` runs onboard the ROV and owns hardware control.
- `TritonAnalysis` runs standalone mission-analysis applets on a separate
  analysis computer.

TritonPilot sits between those two worlds: it operates the ROV and records data
that can be moved into analysis workflows, but it should not contain
mission-specific scoring tools.

## Documentation Style

When updating these docs:

- Write commands relative to the repository root.
- Say which computer a command runs on: pilot computer, ROV, or analysis
  computer.
- Keep live-control and safety notes near the command or UI workflow that can
  affect the vehicle.
- Link to repository paths with relative paths.
- Update this index whenever a maintained guide is added, renamed, or removed.
