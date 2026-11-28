"""aion2-helper [run | fetch-parser | devices | version]"""

from __future__ import annotations

import argparse
import sys

from . import APP_NAME, __version__


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="aion2-helper", description=f"{APP_NAME} {__version__}")
    ap.add_argument("command", nargs="?", default="run", choices=["run", "fetch-parser", "devices", "version"])
    ap.add_argument("--no-browser", action="store_true", help="do not open the page")
    ap.add_argument("--debug", action="store_true", help="more detail in the log")
    args = ap.parse_args(argv)

    if args.command == "version":
        print(__version__)
        return 0
    if args.command == "fetch-parser":
        from .parser_fetch import fetch_parser
        from .settings import data_dir
        try:
            print(fetch_parser(data_dir() / "upstream"))
        except Exception as e:
            print(f"could not download the cube parser: {e}", file=sys.stderr)
            return 1
        return 0
    if args.command == "devices":
        if sys.platform == "win32":
            print("Windows reads keys directly; there are no devices to choose.")
            return 0
        from .linux_evdev import InputDevice, list_devices
        for path in list_devices():
            try:
                d = InputDevice(path)
                print(f"{path}  {d.name}")
                d.close()
            except PermissionError:
                print(f"{path}  (no permission: is your user in the input group?)")
            except OSError as e:
                print(f"{path}  ({e})")
        return 0
    from .app import run
    return run(open_browser=False if args.no_browser else None, debug=args.debug)


if __name__ == "__main__":
    sys.exit(main())
