"""Build a tiny pcap of fake Aion 2 server frames for offline testing.

Frame layout follows the upstream parser: uvarint(len(body)+4) + u16 opcode + payload.
"""
import socket
import struct
import sys


def uvarint(n):
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out += bytes([b | 0x80])
        else:
            return out + bytes([b])


def frame(opcode, p):
    body = struct.pack("<H", opcode) + p
    return uvarint(len(body) + 4) + body


def zone_enter(x, y, z):  # opcode 13859: player xyz at n+5
    return frame(13859, uvarint(7) + b"\0" * 5 + struct.pack("<fff", x, y, z) + b"\0" * 4)


def cube_spawn(eid, obj, x, y, z):  # opcode 13876: object id at n+2, xyz at n+6
    return frame(13876, uvarint(eid) + b"\0\0" + struct.pack("<I", obj)
                 + struct.pack("<fff", x, y, z) + b"\0" * 4)


def cube_remove(eid):  # opcode 13877
    return frame(13877, uvarint(eid) + b"\0" * 4)


def ip_tcp(payload, sport=13328, dport=50000, src="203.0.113.5", dst="192.168.1.20"):
    tcp = struct.pack("!HHIIBBHHH", sport, dport, 1, 1, 5 << 4, 0x18, 65535, 0, 0)
    total = 20 + len(tcp) + len(payload)
    ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, total, 1, 0, 64, 6, 0,
                     socket.inet_aton(src), socket.inet_aton(dst))
    return ip + tcp + payload


PACKETS = [
    ip_tcp(zone_enter(10000.0, 20000.0, 500.0)),
    ip_tcp(cube_spawn(5001, 1200036, 13000.0, 21500.0, 1100.0)
           + cube_spawn(5002, 1000295, 7000.0, 16000.0, 300.0)),
    ip_tcp(b"noise" * 3, sport=50000, dport=443),
    ip_tcp(cube_remove(5002)),
]

if __name__ == "__main__":
    with open(sys.argv[1], "wb") as f:
        f.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 101))
        for i, p in enumerate(PACKETS):
            f.write(struct.pack("<IIII", 1700000000 + i, 0, len(p), len(p)) + p)
