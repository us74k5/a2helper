#!/usr/bin/env python3
"""Linux capture helper for AION 2 Helper's cube overlay.

install.sh copies this file and core.py into /usr/local/libexec/aion2-helper/, owned by root,
and the app starts it with pkexec. It opens one raw AF_PACKET socket (the only step that needs
root), immediately drops back to the user who started it, and only then loads the parser and lz4
from that user's folders. Nothing in your home folder ever runs as root.

It prints JSON lines on stdout and exits when stdin closes (the app quit) or stdout breaks.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time

ETH_P_IP = 0x0800
HERE = os.path.dirname(os.path.abspath(__file__))


def emit(obj) -> None:
    try:
        sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
        sys.stdout.flush()
    except (BrokenPipeError, ValueError):
        os._exit(0)


def open_raw_socket() -> socket.socket:
    # SOCK_DGRAM strips the link-layer header, so every interface type
    # (ethernet, wifi, tun/VPN) hands us a bare IPv4 packet.
    s = socket.socket(socket.AF_PACKET, socket.SOCK_DGRAM, socket.htons(ETH_P_IP))
    s.settimeout(1.0)
    return s


def drop_privileges() -> None:
    if os.geteuid() != 0:
        return
    uid = os.environ.get("PKEXEC_UID") or os.environ.get("SUDO_UID")
    if not uid:
        raise SystemExit("refusing to keep running as root: no PKEXEC_UID/SUDO_UID to drop to")
    import pwd
    pw = pwd.getpwuid(int(uid))
    os.setgroups([])
    os.setgid(pw.pw_gid)
    os.setuid(pw.pw_uid)
    os.environ["HOME"] = pw.pw_dir
    if os.geteuid() == 0 or os.getuid() == 0:
        raise SystemExit("failed to drop root privileges")


def watch_stdin() -> None:
    try:
        while sys.stdin.buffer.read(4096):
            pass
    finally:
        os._exit(0)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--parser-dir", required=True, help="folder holding working_aion_radar.py")
    ap.add_argument("--char", default="", help="character name (helps lock onto your own entity)")
    ap.add_argument("--replay", help="replay a classic .pcap instead of live capture")
    ap.add_argument("--site-packages", action="append", default=[],
                    help="extra import path (the app's venv), added only after dropping root")
    ap.add_argument("--no-stdin-watch", action="store_true")
    args = ap.parse_args()

    sock = None
    if not args.replay:
        try:
            sock = open_raw_socket()
        except PermissionError:
            emit({"t": "error", "msg": "need root (or CAP_NET_RAW) to open the capture socket"})
            sys.exit(2)
        drop_privileges()

    # Everything above ran as root using only the standard library.
    # User-writable code (venv packages, the parser) is loaded only now.
    for path in args.site_packages:
        sys.path.append(os.path.abspath(path))
    sys.path.insert(0, HERE)
    import core  # the root-owned copy next to this file

    try:
        war = core.load_parser(args.parser_dir)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        hint = ""
        if isinstance(exc, ModuleNotFoundError) and exc.name and exc.name.startswith("lz4"):
            hint = " (lz4 not found: re-run install.sh)"
        emit({"t": "error", "msg": f"parser load failed: {type(exc).__name__}: {exc}{hint}"})
        sys.exit(3)

    if not args.no_stdin_watch and not args.replay:
        threading.Thread(target=watch_stdin, daemon=True).start()

    session = core.Session(war, emit, char=args.char)

    if args.replay:
        emit({"t": "info", "msg": f"replaying {args.replay}"})
        for pkt in core.iter_pcap(args.replay):
            session.handle(pkt)
            session.maybe_send(force=True)
        emit({"t": "info", "msg": "replay complete"})
        return

    emit({"t": "info", "msg": "capturing on all interfaces, ports " + ",".join(map(str, core.SERVER_PORTS))})
    last_heartbeat = time.monotonic()
    while True:
        try:
            session.handle(sock.recv(65535))
        except socket.timeout:
            pass
        except InterruptedError:
            continue
        session.maybe_send()
        if time.monotonic() - last_heartbeat > 2.0:
            session.maybe_send(force=True)
            last_heartbeat = time.monotonic()


if __name__ == "__main__":
    main()
