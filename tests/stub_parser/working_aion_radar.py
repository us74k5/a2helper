"""Stand-in for the upstream parser, used only by the offline tests.

Exposes the same surface the helper uses (RadarState, PureStreamEngine) and
decodes just the three synthetic frame types in make_synthetic_pcap.py.
"""
import struct
import threading
from dataclasses import dataclass


def decode_uvarint(data, off=0):
    val = shift = 0
    start = off
    while True:
        b = data[off]
        off += 1
        val |= (b & 0x7F) << shift
        if not b & 0x80:
            return val, off - start
        shift += 7


@dataclass
class CubeItem:
    entity_id: int
    npc_id: int
    name: str
    x: float
    y: float
    z: float


class RadarState:
    def __init__(self):
        self.lock = threading.Lock()
        self.player_pos = None
        self.cubes = {}
        self.extrapolation_enabled = True
        self.target_char_name = ""
        self.on_update_callback = None

    def _notify(self):
        if self.on_update_callback:
            self.on_update_callback()


class PureStreamEngine:
    def __init__(self, state):
        self.state = state

    def feed_payload(self, src_ip, src_port, dst_ip, dst_port, payload):
        off = 0
        while off < len(payload):
            enc, n = decode_uvarint(payload, off)
            body = payload[off + n: off + n + enc - 4]
            off += n + enc - 4
            opcode = struct.unpack("<H", body[:2])[0]
            p = body[2:]
            eid, m = decode_uvarint(p, 0)
            with self.state.lock:
                if opcode == 13859:
                    self.state.cubes.clear()
                    self.state.player_pos = struct.unpack("<fff", p[m + 5:m + 17])
                elif opcode == 13876:
                    obj = struct.unpack("<I", p[m + 2:m + 6])[0]
                    x, y, z = struct.unpack("<fff", p[m + 6:m + 18])
                    self.state.cubes[eid] = CubeItem(eid, obj, f"Hidden Cube ({obj})", x, y, z)
                elif opcode == 13877:
                    self.state.cubes.pop(eid, None)
            self.state._notify()
