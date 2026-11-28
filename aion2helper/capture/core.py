"""Turning captured packets into cube snapshots. Standard library only.

This file is also installed root-owned next to the Linux capture helper, which imports it
after it has dropped root. It never sends or changes any traffic.

Snapshots are dicts:
  {"t":"snap","player":[x,y,z] | None,"cubes":[[eid,npc_id,x,y,z,name],...],"pkts":n,"mv":n,"pid":n}
  ("pid": the entity ID taken to be you; a change means earlier positions were someone else's)
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


def read_varint(data: bytes, at: int = 0):
    """(value, next offset) for the game's unsigned varint."""
    value = shift = 0
    while at < len(data) and shift < 35:
        byte = data[at]
        at += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, at
        shift += 7
    raise ValueError("invalid varint")


def iter_frames(payload: bytes):
    """Complete game frames (opcode + body) in one TCP payload."""
    at = 0
    while at < len(payload):
        try:
            encoded, body_at = read_varint(payload, at)
        except ValueError:
            return
        body_len = encoded - 4
        if body_len < 2 or body_at + body_len > len(payload):
            return
        yield payload[body_at:body_at + body_len]
        at = body_at + body_len


OP_OWN_LIST = 0x3633        # server -> you: the effects on YOUR character (sent after a map load)
OP_MAP_LOAD = 0x3621        # server: a new map is loading; entity IDs start over
OP_ACTION = frozenset(range(14096, 14161)) | {14378, 14379, 14380, 14340}  # an entity moved or acted
OP_SKILL_REQ = 0x3800       # you -> server: use a skill
OP_TARGET_REQ = 0x3833      # you -> server: select a target
VOTE_WINDOW_S = 0.6         # the server's answer to your skill arrives within this
VOTE_ROUNDS = 8             # judge from your last few skill presses
VOTE_MIN = 4                # ...needing at least this many
VOTE_WIN = 0.75             # you show up after at least this share of them
VOTE_RUNNER_UP = 0.5        # and nobody else after more than this share


class SelfFinder:
    """Works out which entity is your character without being told its name.

    * The server sends your own effect list on every map load: that names you outright.
    * Starting mid-game, it's learned from your skill presses: the entity that acts right after
      most of them is you. Your selected target is left out, and if anything else keeps pace
      with you, nothing is decided (a wrong lock is worse than none).
    """

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.own = None             # entity ID from your own effect list
        self.voted = None           # entity ID from your skill presses
        self.target = None
        self._window = None         # (opened at, entities seen since)
        self._rounds = []

    @property
    def entity(self):
        return self.own or self.voted

    def on_c2s(self, body: bytes) -> None:
        if len(body) < 3:
            return
        op = body[0] | body[1] << 8
        now = self.clock()
        if op == OP_TARGET_REQ:
            try:
                self.target = read_varint(body, 3)[0] or None
            except ValueError:
                pass
        elif op == OP_SKILL_REQ:
            self._close_window()
            self._window = (now, set())

    def on_s2c(self, body: bytes) -> None:
        if len(body) < 3:
            return
        op = body[0] | body[1] << 8
        try:
            eid = read_varint(body, 2)[0]
        except ValueError:
            return
        if op == OP_MAP_LOAD:
            self.own = self.voted = self.target = self._window = None
            self._rounds.clear()
        elif op == OP_OWN_LIST and eid > 100:
            self.own = eid
        elif op in OP_ACTION and eid > 100 and self._window is not None:
            if self.clock() - self._window[0] > VOTE_WINDOW_S:
                self._close_window()
            elif eid != self.target:
                self._window[1].add(eid)

    def _close_window(self):
        if self._window is None:
            return
        self._rounds.append(self._window[1])
        self._window = None
        del self._rounds[:-VOTE_ROUNDS]
        if len(self._rounds) < VOTE_MIN:
            return
        counts = {}
        for seen in self._rounds:
            for eid in seen:
                counts[eid] = counts.get(eid, 0) + 1
        ranked = sorted(counts.values(), reverse=True) + [0, 0]
        n = len(self._rounds)
        if ranked[0] >= VOTE_WIN * n and ranked[1] <= VOTE_RUNNER_UP * n:
            self.voted = max(counts, key=counts.get)


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
        # Without a character name the parser guesses which entity is you and can latch onto
        # someone else; SelfFinder works it out and pins the parser to it.
        self.finder = SelfFinder()
        orig = getattr(self.engine, "handle_frame", None)
        if orig is not None:
            def handle_frame(body, *a, **k):
                before = getattr(self.state, "player_entity_id", None)
                self.finder.on_s2c(body)
                orig(body, *a, **k)
                self._pin_self(before)
            self.engine.handle_frame = handle_frame   # the parser's nested calls go through here too
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
            if dport in self.ports:
                for body in iter_frames(payload):
                    self.finder.on_c2s(body)
            try:
                self.engine.feed_payload(src, sport, dst, dport, payload)
            except Exception:
                pass

    def _pin_self(self, before) -> None:
        eid = self.finder.entity
        st = self.state
        if eid is None:
            return
        if getattr(st, "player_entity_id", None) != eid or not getattr(st, "player_id_locked", True):
            # your own effect list beats everything; a vote doesn't override a name lock
            if self.finder.own != eid and getattr(st, "player_id_locked", False):
                return
            st.set_player_id(eid, lock=True, force=True)
            self.dirty = True
        if before is not None and before != eid:
            with st.lock:
                st.player_pos = None    # that was someone else's position
            self.dirty = True

    def maybe_send(self, force: bool = False) -> None:
        now = time.monotonic()
        if not (self.dirty or force) or (now - self.last_sent < self.min_dt and not force):
            return
        with self.state.lock:
            pos = self.state.player_pos
            cubes = [[c.entity_id, c.npc_id, round(c.x, 1), round(c.y, 1), round(c.z, 1), c.name]
                     for c in self.state.cubes.values()]
            moves = getattr(self.state, "c2s_step_count", None)
            pid = getattr(self.state, "player_entity_id", None)
        payload = {"t": "snap", "player": [round(v, 1) for v in pos] if pos else None, "cubes": cubes,
                   "pid": pid}
        if moves is not None:
            payload["mv"] = moves   # your client's movement packets: the overlay estimates walking from them
        key = json.dumps(payload, sort_keys=True)
        if key != self.last_key or force:
            payload["pkts"] = self.pkts
            self.emit(payload)
            self.last_key = key
        self.last_sent = now
        self.dirty = False
