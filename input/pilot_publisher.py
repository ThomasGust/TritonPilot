# input/pilot_publisher.py
from __future__ import annotations

import argparse
import time

from config import PILOT_PUB_ENDPOINT
from input.pilot_service import PilotPublisherService
from input.controller import list_controllers


def main():
    ap = argparse.ArgumentParser(description="Topside pilot publisher (controller → ROV)")
    ap.add_argument(
        "--endpoint",
        default=PILOT_PUB_ENDPOINT,
        help="ZMQ PUB endpoint of ROV pilot SUB",
    )
    ap.add_argument("--rate", type=float, default=30.0, help="Publish rate (Hz)")
    # Default deadzone comes from config (and can be overridden by env).
    from config import CONTROLLER_DEADZONE
    ap.add_argument("--deadzone", type=float, default=CONTROLLER_DEADZONE, help="Stick deadzone (abs)")
    ap.add_argument("--index", type=int, default=0, help="pygame joystick index")
    ap.add_argument("--debug", action="store_true", help="Enable debug logs")
    ap.add_argument("--list", action="store_true", help="List detected controllers and exit")

    ap.add_argument(
        "--dump-raw-every",
        type=float,
        default=0.0,
        help="If >0, print RAW axes/buttons/hats every N seconds",
    )
    ap.add_argument(
        "--foreground",
        action="store_true",
        help="Run publish loop in the foreground (no thread) for debugging",
    )
    args = ap.parse_args()

    if args.list:
        devices = list_controllers()
        if not devices:
            print("No controllers detected.")
            return
        print("Detected controllers:")
        for d in devices:
            print(
                f"  index={d['index']} name='{d['name']}' guid='{d['guid']}' "
                f"axes={d['axes']} buttons={d['buttons']} hats={d['hats']}"
            )
        return

    svc = PilotPublisherService(
        endpoint=args.endpoint,
        rate_hz=args.rate,
        deadzone=args.deadzone,
        debug=args.debug,
        index=args.index,
        dump_raw_every_s=args.dump_raw_every,
    )

    print(
        f"[pilot] publishing to {args.endpoint} at {args.rate} Hz "
        f"(index={args.index}, deadzone={args.deadzone}, debug={args.debug})"
    )

    try:
        svc.start(threaded=not args.foreground)

        # If threaded, keep main alive without pegging CPU
        if not args.foreground:
            while True:
                time.sleep(0.25)

    except KeyboardInterrupt:
        print("[pilot] stopping…")
        svc.stop()


if __name__ == "__main__":
    main()
