#!/usr/bin/env python3
import socket
import time
import argparse

def main():
    ap = argparse.ArgumentParser(description="TCP client stream test")
    ap.add_argument("--host", required=True, help="ROV IP address, e.g. {ROV_HOST}")
    ap.add_argument("--port", type=int, default=6000)
    ap.add_argument("--rate", type=float, default=20.0, help="messages per second")
    args = ap.parse_args()

    addr = (args.host, args.port)
    print(f"[client] connecting to {addr} ...")

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(5.0)
    s.connect(addr)
    s.settimeout(None)

    print("[client] connected. streaming... (Ctrl+C to stop)")
    period = 1.0 / max(0.1, args.rate)

    i = 0
    total_bytes = 0
    t0 = time.time()

    try:
        while True:
            msg = f"{time.time():.3f} i={i} hello_from_windows\n".encode("utf-8")
            s.sendall(msg)
            total_bytes += len(msg)
            if i % int(max(1, args.rate)) == 0:
                dt = time.time() - t0
                print(f"[client] sent i={i} total_bytes={total_bytes} rate~={total_bytes/max(dt,1e-6):.0f} B/s")
            i += 1
            time.sleep(period)
    except KeyboardInterrupt:
        print("\n[client] ctrl+c, quitting.")
    finally:
        try:
            s.close()
        except Exception:
            pass

if __name__ == "__main__":
    main()
