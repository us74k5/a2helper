"""Turning captured packets into cube snapshots. Standard library only.

This file is also installed root-owned next to the Linux capture helper, which imports it
after it has dropped root. It never sends or changes any traffic.

Snapshots are dicts:
  {"t":"snap","player":[x,y,z] | None,"cubes":[[eid,npc_id,x,y,z,name],...],"pkts":n,"mv":n}
  ("mv": how many movement packets your client has sent; the overlay estimates walking from it)
  {"t":"info","msg":"..."}   {"t":"error","msg":"..."}
"""

from __future__ import annotations

import json
import os
import socket
import struct
import sys
import threading
import time
import types

SERVER_PORTS = (13328, 13320)
PARSER_FILE = "working_aion_radar.py"

# link-layer type -> bytes before the IPv4 header (None: look at the ethertype)
LINK_OFFSETS = {0: 4, 1: None, 12: 0, 14: 0, 101: 0, 108: 4, 113: 16, 228: 0, 276: 20}


def strip_link(linktype: int, data: bytes) -> bytes | None:
    """The IPv4 packet inside one captured frame, or None."""
    if linktype == 1:  # ethernet, with up to two VLAN tags
        off = 12
        while len(data) >= off + 2 and data[off:off + 2] in (b"\x81\x00", b"\x88\xa8"):
            off += 4
        if data[off:off + 2] != b"\x08\x00":
            return None
        return data[off + 2:]
    off = LINK_OFFSETS.get(linktype)
    if off is None:
        return None
    return data[off:]


def split_ipv4_tcp(pkt: bytes):
    """Return (src_ip, sport, dst_ip, dport, payload) or None."""
    if len(pkt) < 20 or (pkt[0] >> 4) != 4:
        return None
    ihl = (pkt[0] & 0x0F) * 4
    if pkt[9] != 6 or ihl < 20:
        return None
    total = struct.unpack_from("!H", pkt, 2)[0]
    if total and total <= len(pkt):
        pkt = pkt[:total]
    frag = struct.unpack_from("!H", pkt, 6)[0]
    if frag & 0x1FFF:  # non-first fragment; no TCP header
        return None
    if len(pkt) < ihl + 20:
        return None
    sport, dport = struct.unpack_from("!HH", pkt, ihl)
    doff = (pkt[ihl + 12] >> 4) * 4
    payload = pkt[ihl + doff:]
    return socket.inet_ntoa(pkt[12:16]), sport, socket.inet_ntoa(pkt[16:20]), dport, payload


def iter_pcap(path: str):
    """Minimal classic-pcap reader yielding IPv4 packets (offline testing)."""
    with open(path, "rb") as f:
        head = f.read(24)
        magic = head[:4]
        if magic in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1"):
            endian = "<"
        elif magic in (b"\xa1\xb2\xc3\xd4", b"\xa1\xb2\x3c\x4d"):
            endian = ">"
        else:
            raise ValueError("not a classic pcap file (pcapng is not supported)")
        linktype = struct.unpack(endian + "I", head[20:24])[0]
        while True:
            rec = f.read(16)
            if len(rec) < 16:
                return
            incl = struct.unpack(endian + "IIII", rec)[2]
            ip = strip_link(linktype, f.read(incl))
            if ip is not None:
                yield ip


def load_parser(parser_dir: str):
    """Import the upstream Aion2CubeGPS parser without its Windows/Tk dependencies."""
    # It imports tkinter, psutil and scapy at the top but only uses them in its own window and
    # sniffer, which this program replaces. Stub them so neither system needs them.
    sys.modules.setdefault("tkinter", types.ModuleType("tkinter"))
    if "psutil" not in sys.modules:
        try:
            import psutil  # noqa: F401
        except ImportError:
            sys.modules["psutil"] = types.ModuleType("psutil")
    if "scapy.all" not in sys.modules:
        scapy = types.ModuleType("scapy")
        scapy_all = types.ModuleType("scapy.all")
        for name in ("sniff", "IP", "TCP", "PcapReader"):
            setattr(scapy_all, name, None)
        scapy.all = scapy_all
        sys.modules["scapy"] = scapy
        sys.modules["scapy.all"] = scapy_all
    parser_dir = os.path.abspath(parser_dir)
    if not os.path.isfile(os.path.join(parser_dir, PARSER_FILE)):
        raise FileNotFoundError(f"the cube parser is not downloaded yet ({PARSER_FILE} not in {parser_dir})")
    if parser_dir not in sys.path:
        sys.path.insert(0, parser_dir)
    sys.modules.pop("working_aion_radar", None)
    import working_aion_radar as war
    war.play_cube_sound = lambda *a, **k: None  # the overlay plays its own alert
    war.print = lambda *a, **k: None            # keep stdout clean for JSON
    return war


class Session:
    """One parser, fed packets from any number of capture threads, emitting snapshots."""

    def __init__(self, war, emit, char: str = "", ports=SERVER_PORTS, max_hz: float = 15.0):
        self.emit = emit
        self.ports = tuple(ports)
        self.state = war.RadarState()
        self.state.extrapolation_enabled = False  # only show real decoded positions
        self.state.target_char_name = char.strip()
        self.engine = war.PureStreamEngine(self.state)
        self.state.on_update_callback = self.mark
        self.pkts = 0
        self.min_dt = 1.0 / max_hz
        self.last_sent = 0.0
        self.last_key = None
        self.dirty = True
        self.feed_lock = threading.Lock()

    def mark(self) -> None:
        self.dirty = True

    def handle(self, ip_packet: bytes) -> None:
        parts = split_ipv4_tcp(ip_packet)
        if not parts:
            return
        src, sport, dst, dport, payload = parts
        if not payload or (sport not in self.ports and dport not in self.ports):
            return
        with self.feed_lock:
            self.pkts += 1
            try:
                self.engine.feed_payload(src, sport, dst, dport, payload)
            except Exception:
                pass

    def maybe_send(self, force: bool = False) -> None:
        now = time.monotonic()
        if not (self.dirty or force) or (now - self.last_sent < self.min_dt and not force):
            return
        with self.state.lock:
            pos = self.state.player_pos
            cubes = [[c.entity_id, c.npc_id, round(c.x, 1), round(c.y, 1), round(c.z, 1), c.name]
                     for c in self.state.cubes.values()]
            moves = getattr(self.state, "c2s_step_count", None)
        payload = {"t": "snap", "player": [round(v, 1) for v in pos] if pos else None, "cubes": cubes}
        if moves is not None:
            payload["mv"] = moves   # your client's movement packets: the overlay estimates walking from them
        key = json.dumps(payload, sort_keys=True)
        if key != self.last_key or force:
            payload["pkts"] = self.pkts
            self.emit(payload)
            self.last_key = key
        self.last_sent = now
        self.dirty = False
