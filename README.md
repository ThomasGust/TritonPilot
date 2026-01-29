# TritonPilot

Topside pilot/control station software for TritonOS.

## Network transparency

If the ROV publishes ``type="net"`` telemetry (see TritonOS), it will appear in
the sensor table and as a status-bar indicator (tether vs wifi, link state,
RX/TX).

For ad-hoc speed tests against the ROV tether network:

```bash
python -m tools.netdiag_client --host 192.168.1.4 udp
python -m tools.netdiag_client --host 192.168.1.4 tcp-rx --seconds 5
python -m tools.netdiag_client --host 192.168.1.4 tcp-tx --seconds 5
```
