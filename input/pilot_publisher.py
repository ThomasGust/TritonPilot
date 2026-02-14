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
    from config import CONTROLLER_INDEX
    ap.add_argument("--index", type=int, default=CONTROLLER_INDEX, help="pygame joystick index")
    ap.add_argument("--debug", action="store_true", help="Enable debug logs")
    ap.add_argument("--list", action="store_true", help="List detected controllers and exit")

    from config import CONTROLLER_DUMP_RAW_EVERY_S
    ap.add_argument(
        "--dump-raw-every",
        type=float,
        default=CONTROLLER_DUMP_RAW_EVERY_S,
        help="If >0, print RAW axes/buttons/hats every N seconds",
    )

    # Optional mapping overrides for diagnosing controllers with unusual layouts.
    ap.add_argument(
        "--axis-map",
        default=None,
        help="Override axis map as 6 comma-separated ints: lx,ly,rx,ry,lt,rt (e.g. '0,1,2,3,4,5')",
    )
    ap.add_argument(
        "--hat-index",
        type=int,
        default=None,
        help="Override dpad hat index (usually 0)",
    )
    ap.add_argument(
        "--menu-buttons",
        default=None,
        help="Override menu/start button candidate indices (comma-separated, e.g. '7,9,11')",
    )
    ap.add_argument(
        "--win-buttons",
        default=None,
        help="Override win/back button candidate indices (comma-separated, e.g. '6,8,10')",
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

    # Parse optional overrides
    def _parse_int_list(s: str) -> list[int]:
        return [int(x.strip()) for x in s.split(",") if x.strip() != ""]

    if args.axis_map is not None:
        from config import CONTROLLER_AXIS_MAP
        try:
            axis_map = _parse_int_list(args.axis_map)
        except Exception:
            axis_map = CONTROLLER_AXIS_MAP
    else:
        from config import CONTROLLER_AXIS_MAP
        axis_map = CONTROLLER_AXIS_MAP

    if args.hat_index is not None:
        hat_index = int(args.hat_index)
    else:
        from config import CONTROLLER_HAT_INDEX
        hat_index = CONTROLLER_HAT_INDEX

    if args.menu_buttons is not None:
        try:
            menu_buttons = _parse_int_list(args.menu_buttons)
        except Exception:
            menu_buttons = []
    else:
        from config import CONTROLLER_MENU_BUTTONS
        menu_buttons = CONTROLLER_MENU_BUTTONS

    if args.win_buttons is not None:
        try:
            win_buttons = _parse_int_list(args.win_buttons)
        except Exception:
            win_buttons = []
    else:
        from config import CONTROLLER_WIN_BUTTONS
        win_buttons = CONTROLLER_WIN_BUTTONS

    svc = PilotPublisherService(
        endpoint=args.endpoint,
        rate_hz=args.rate,
        deadzone=args.deadzone,
        debug=args.debug,
        index=args.index,
        axis_map=axis_map,
        hat_index=hat_index,
        menu_buttons=menu_buttons,
        win_buttons=win_buttons,
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
