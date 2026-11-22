"""A small ctypes binding to the pcap API: Npcap's wpcap.dll on Windows, libpcap elsewhere.

Windows has no raw sockets that see TCP traffic without administrator rights, so the cube
overlay reads the game's traffic through Npcap, like the Windows cube tools and the DPS meter.
Npcap's default install lets ordinary programs capture. It only reads; nothing is sent.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import sys
import threading

PCAP_ERRBUF_SIZE = 256
PCAP_IF_LOOPBACK = 0x1
PCAP_NETMASK_UNKNOWN = 0xFFFFFFFF
NPCAP_URL = "https://npcap.com/#download"
_dll_dirs = []  # keep Npcap's folder registered for the life of the program


class PcapError(OSError):
    pass


class _Timeval(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_long)]


class _PktHdr(ctypes.Structure):
    _fields_ = [("ts", _Timeval), ("caplen", ctypes.c_uint32), ("len", ctypes.c_uint32)]


class _BpfProgram(ctypes.Structure):
    _fields_ = [("bf_len", ctypes.c_uint), ("bf_insns", ctypes.c_void_p)]


class _PcapIf(ctypes.Structure):
    pass


_PcapIf._fields_ = [("next", ctypes.POINTER(_PcapIf)), ("name", ctypes.c_char_p),
                    ("description", ctypes.c_char_p), ("addresses", ctypes.c_void_p),
                    ("flags", ctypes.c_uint32)]


def load_library():
    """The pcap library, or raise PcapError with a sentence a player can act on."""
    if sys.platform == "win32":
        npcap = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "Npcap")
        if not os.path.isfile(os.path.join(npcap, "wpcap.dll")):
            raise PcapError(f"Npcap is not installed. Install it from {NPCAP_URL} (keep the default "
                            "options), then press \"try again\" on the page.")
        _dll_dirs.append(os.add_dll_directory(npcap))
        try:
            return ctypes.CDLL(os.path.join(npcap, "wpcap.dll"))
        except OSError as e:
            raise PcapError(f"Npcap is installed but could not be loaded ({e}); reinstall it from {NPCAP_URL}") from e
    name = ctypes.util.find_library("pcap") or "libpcap.so.0.8"
    try:
        return ctypes.CDLL(name)
    except OSError as e:
        raise PcapError("libpcap is not installed") from e


class Pcap:
    def __init__(self, lib=None):
        self.lib = lib or load_library()
        L = self.lib
        L.pcap_findalldevs.argtypes = [ctypes.POINTER(ctypes.POINTER(_PcapIf)), ctypes.c_char_p]
        L.pcap_findalldevs.restype = ctypes.c_int
        L.pcap_freealldevs.argtypes = [ctypes.POINTER(_PcapIf)]
        L.pcap_freealldevs.restype = None
        L.pcap_open_live.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_char_p]
        L.pcap_open_live.restype = ctypes.c_void_p
        L.pcap_open_offline.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        L.pcap_open_offline.restype = ctypes.c_void_p
        L.pcap_close.argtypes = [ctypes.c_void_p]
        L.pcap_close.restype = None
        L.pcap_datalink.argtypes = [ctypes.c_void_p]
        L.pcap_datalink.restype = ctypes.c_int
        L.pcap_compile.argtypes = [ctypes.c_void_p, ctypes.POINTER(_BpfProgram), ctypes.c_char_p,
                                   ctypes.c_int, ctypes.c_uint32]
        L.pcap_compile.restype = ctypes.c_int
        L.pcap_setfilter.argtypes = [ctypes.c_void_p, ctypes.POINTER(_BpfProgram)]
        L.pcap_setfilter.restype = ctypes.c_int
        L.pcap_freecode.argtypes = [ctypes.POINTER(_BpfProgram)]
        L.pcap_freecode.restype = None
        L.pcap_geterr.argtypes = [ctypes.c_void_p]
        L.pcap_geterr.restype = ctypes.c_char_p
        L.pcap_next_ex.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.POINTER(_PktHdr)),
                                   ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte))]
        L.pcap_next_ex.restype = ctypes.c_int
        L.pcap_breakloop.argtypes = [ctypes.c_void_p]
        L.pcap_breakloop.restype = None

    def devices(self) -> list[tuple[str, str, int]]:
        """(name, description, flags) for every capture device."""
        alldevs = ctypes.POINTER(_PcapIf)()
        err = ctypes.create_string_buffer(PCAP_ERRBUF_SIZE)
        if self.lib.pcap_findalldevs(ctypes.byref(alldevs), err) != 0:
            raise PcapError(f"could not list network adapters: {err.value.decode(errors='replace')}")
        out = []
        node = alldevs
        while node:
            d = node.contents
            out.append(((d.name or b"").decode(errors="replace"),
                        (d.description or b"").decode(errors="replace"), int(d.flags)))
            node = d.next
        if alldevs:
            self.lib.pcap_freealldevs(alldevs)
        return out

    def open_live(self, device: str, bpf: str, timeout_ms: int = 250) -> "Handle":
        err = ctypes.create_string_buffer(PCAP_ERRBUF_SIZE)
        h = self.lib.pcap_open_live(device.encode(), 65535, 0, timeout_ms, err)
        if not h:
            raise PcapError(err.value.decode(errors="replace") or f"could not open {device}")
        handle = Handle(self.lib, h)
        handle.set_filter(bpf)
        return handle

    def open_offline(self, path: str, bpf: str = "") -> "Handle":
        err = ctypes.create_string_buffer(PCAP_ERRBUF_SIZE)
        h = self.lib.pcap_open_offline(os.fsencode(path), err)
        if not h:
            raise PcapError(err.value.decode(errors="replace"))
        handle = Handle(self.lib, h)
        if bpf:
            handle.set_filter(bpf)
        return handle


class Handle:
    TIMEOUT = object()

    def __init__(self, lib, h):
        self.lib, self.h = lib, h
        self.linktype = lib.pcap_datalink(h)
        self._lock = threading.Lock()

    def error(self) -> str:
        msg = self.lib.pcap_geterr(self.h)
        return msg.decode(errors="replace") if msg else "pcap error"

    def set_filter(self, bpf: str) -> None:
        prog = _BpfProgram()
        if self.lib.pcap_compile(self.h, ctypes.byref(prog), bpf.encode(), 1, PCAP_NETMASK_UNKNOWN) != 0:
            raise PcapError(f"bad capture filter: {self.error()}")
        try:
            if self.lib.pcap_setfilter(self.h, ctypes.byref(prog)) != 0:
                raise PcapError(f"could not set the capture filter: {self.error()}")
        finally:
            self.lib.pcap_freecode(ctypes.byref(prog))

    def next(self):
        """bytes of the next frame, Handle.TIMEOUT, or None at the end of a file."""
        hdr = ctypes.POINTER(_PktHdr)()
        data = ctypes.POINTER(ctypes.c_ubyte)()
        r = self.lib.pcap_next_ex(self.h, ctypes.byref(hdr), ctypes.byref(data))
        if r == 1:
            return ctypes.string_at(data, hdr.contents.caplen)
        if r == 0:
            return self.TIMEOUT
        if r == -2:
            return None
        raise PcapError(self.error())

    def close(self) -> None:
        with self._lock:
            if self.h:
                self.lib.pcap_close(self.h)
                self.h = None


def bpf_for(ports) -> str:
    return "tcp and (" + " or ".join(f"port {int(p)}" for p in ports) + ")"
