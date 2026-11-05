#!/usr/bin/env python3
"""UNO over the local network - entry point.

    python3 uno.py                 # open the menu
    python3 uno.py --host          # host a LAN room
    python3 uno.py --join 192.168.1.7
    python3 uno.py --host --no-web     # do not serve the phone/browser version
    python3 uno.py --selftest      # run the rules test-suite
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="UNO - local network card game")
    ap.add_argument("--selftest", action="store_true", help="run the rules test-suite")
    ap.add_argument("--host", action="store_true", help="host a room on the LAN")
    ap.add_argument("--join", metavar="HOST", help="join a room by IP/hostname")
    ap.add_argument("--spectate", action="store_true", help="join as a spectator")
    ap.add_argument("--port", type=int, default=0, help="TCP port (default 45872)")
    ap.add_argument("--name", default="", help="player name")
    ap.add_argument("--lang", choices=("zh", "en"), help="UI language")
    ap.add_argument("--no-discovery", action="store_true",
                    help="do not advertise this room via UDP broadcast")
    ap.add_argument("--no-web", action="store_true",
                    help="do not serve the phone/browser version of the room")
    ap.add_argument("--web-port", type=int, default=0,
                    help="HTTP port for the browser version (default 8080)")
    args = ap.parse_args(argv)

    if args.selftest:
        import selftest

        return selftest.main()

    import net
    import ui

    port = args.port or net.DEFAULT_PORT
    app = ui.UnoApp(name=args.name, lang=args.lang, host_port=port)
    app.discovery = not args.no_discovery
    app.web_enabled = not args.no_web
    if args.web_port:
        app.web_port = args.web_port
    if args.host:
        app.after(150, app.host_room)
    elif args.join:
        app.after(150, lambda: app.join_room(args.join, port, args.spectate))
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
