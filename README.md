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

## Video uses tether (while keeping Wi‑Fi on)

The video system is UDP by default. If your topside machine has both Wi‑Fi and
tether connected, TritonPilot will now **auto-select the local IP that can reach
the ROV video RPC host**, preferring wired/tether when possible, and bind the
UDP receiver to that IP.

You can override behavior in `data/streams.json`:

```json
{
  "tether_prefer_wired": true,
  "bind_receiver_to_host": true,
  "windows_host": "192.168.2.1",
  "streams": [ ... ]
}
```

If you still see “Waiting for frames…” after switching to tether, Windows
firewall/network profile settings may be blocking inbound UDP on the Ethernet
adapter. In that case, mark the tether network as **Private** or add a firewall
rule to allow UDP on the stream ports (e.g. 5000-5003).
