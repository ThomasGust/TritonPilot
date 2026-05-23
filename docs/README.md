# TritonPilot Documentation

This folder is the maintained documentation set for TritonPilot. It is meant to
replace scattered notes and wiki pages with repository-local Markdown that
stays close to the code.

## Guides

- [Setup Guide](SETUP.md) - Install Python dependencies, GStreamer, and the
  Windows helper environment needed to run the topside app.
- [Network Guide](NETWORKING.md) - Tether addressing, ZeroMQ endpoints, video
  ports, Windows NAT routing, firewalls, and diagnostics.
- [Operations Guide](OPERATIONS.md) - Pilot-station startup, preflight,
  controller use, recording, raw sensor checks, management tools, and shutdown.
- [Architecture Overview](ARCHITECTURE.md) - How GUI, controller, telemetry,
  video, recording, and RPC services fit together.
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
