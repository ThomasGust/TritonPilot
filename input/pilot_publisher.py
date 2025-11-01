# input/pilot_publisher.py
from __future__ import annotations
import argparse

from config import PILOT_PUB_ENDPOINT
from input.pilot_service import PilotPublisherService


def main():
    ap = argparse.ArgumentParser(description="Topside pilot publisher (xbox → ROV)")
    ap.add_argument("--endpoint", default=PILOT_PUB_ENDPOINT,
                    help="ZMQ PUB endpoint of ROV pilot SUB")
    ap.add_argument("--rate", type=float, default=30.0)
    ap.add_argument("--deadzone", type=float, default=0.1)
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    svc = PilotPublisherService(
        endpoint=args.endpoint,
        rate_hz=args.rate,
        deadzone=args.deadzone,
        debug=args.debug,
    )
    print(f"[pilot] publishing to {args.endpoint} at {args.rate} Hz")
    svc.start()

    try:
        # just block here
        while True:
            pass
    except KeyboardInterrupt:
        print("[pilot] stopping…")
        svc.stop()


if __name__ == "__main__":
    main()
