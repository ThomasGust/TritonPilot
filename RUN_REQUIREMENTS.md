# TritonPilot Run Requirements

This file is kept as a compatibility pointer for older handoffs. The maintained
runtime documentation now lives in `docs/`.

Start with:

- [Setup Guide](docs/SETUP.md)
- [Network Guide](docs/NETWORKING.md)
- [Operations Guide](docs/OPERATIONS.md)
- [Configuration Guide](docs/CONFIGURATION.md)
- [Testing And Troubleshooting](docs/TESTING_AND_TROUBLESHOOTING.md)

The short version is:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_windows.ps1
.\.venv\Scripts\activate
$env:ROV_HOST="192.168.1.4"
python .\main_topside.py
```

Full operation requires a reachable TritonOS ROV, a working controller,
GStreamer, outbound TCP access to ROV ports `6000`, `6001`, `5555`, and `5556`,
and inbound UDP access on the camera ports listed in `data/streams.json`.
