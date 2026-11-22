"""Windows input: SendInput for pressing, GetAsyncKeyState polling for watching.

Keys are sent as scan codes, which is what games that read the keyboard directly expect.
Polling needs no hook and no elevated rights. A game running as administrator ignores input
from a program that is not, so run both the same way.
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time

from .keys import VK

log = logging.getLogger("aion2helper")

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_SCANCODE = 0x1, 0x2, 0x8
MAPVK_VK_TO_VSC = 0
# button name -> (down flag, up flag, mouseData)
MOUSE_FLAGS = {
    "BTN_LEFT": (0x0002, 0x0004, 0), "BTN_RIGHT": (0x0008, 0x0010, 0), "BTN_MIDDLE": (0x0020, 0x0040, 0),
    "BTN_SIDE": (0x0080, 0x0100, 1), "BTN_EXTRA": (0x0080, 0x0100, 2),
}
POLL_S = 0.01


class Win32:
    """The few Win32 calls used, behind one object so tests can replace it."""

    def __init__(self):
        from ctypes import wintypes

        u = ctypes.WinDLL("user32", use_last_error=True)
        ULONG_PTR = ctypes.c_size_t

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

        class HARDWAREINPUT(ctypes.Structure):
            _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]

        class _U(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]

        class INPUT(ctypes.Structure):
            _anonymous_ = ("u",)
            _fields_ = [("type", wintypes.DWORD), ("u", _U)]

        self._INPUT, self._KB, self._MI = INPUT, KEYBDINPUT, MOUSEINPUT
        self._send = u.SendInput
        self._send.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
        self._send.restype = wintypes.UINT
        self._map = u.MapVirtualKeyW
        self._map.argtypes = (wintypes.UINT, wintypes.UINT)
        self._map.restype = wintypes.UINT
        self._state = u.GetAsyncKeyState
        self._state.argtypes = (ctypes.c_int,)
        self._state.restype = ctypes.c_short

    def scan_code(self, vk: int) -> int:
        return self._map(vk, MAPVK_VK_TO_VSC)

    def key(self, scan: int, extended: bool, up: bool) -> None:
        flags = KEYEVENTF_SCANCODE | (KEYEVENTF_EXTENDEDKEY if extended else 0) | (KEYEVENTF_KEYUP if up else 0)
        self._emit(self._INPUT(type=INPUT_KEYBOARD, ki=self._KB(0, scan, flags, 0, 0)))

    def mouse(self, flag: int, data: int) -> None:
        self._emit(self._INPUT(type=INPUT_MOUSE, mi=self._MI(0, 0, data, flag, 0, 0)))

    def _emit(self, inp) -> None:
        if self._send(1, ctypes.byref(inp), ctypes.sizeof(inp)) != 1:
            raise OSError(f"SendInput was refused (error {ctypes.get_last_error()}); "
                          "is the game running as administrator?")

    def is_down(self, vk: int) -> bool:
        return bool(self._state(vk) & 0x8000)


class WinOutput:
    def __init__(self, keys: list[str], api=None):
        unknown = [k for k in keys if k not in VK]
        if unknown:
            raise RuntimeError(f"keys not supported on Windows: {unknown}")
        self._api = api or Win32()
        self._warned = False
        self._down: set[str] = set()

    def _set(self, key: str, up: bool) -> None:
        a = self._api
        try:
            if key in MOUSE_FLAGS:
                down, upflag, data = MOUSE_FLAGS[key]
                a.mouse(upflag if up else down, data)
            else:
                vk, ext = VK[key]
                a.key(a.scan_code(vk), ext, up=up)
        except OSError as e:
            if not self._warned:  # say it once; a refused press is not fatal
                log.error("%s", e)
                self._warned = True

    def key_down(self, key: str) -> None:
        self._set(key, up=False)
        self._down.add(key)

    def key_up(self, key: str) -> None:
        self._set(key, up=True)
        self._down.discard(key)

    def tap(self, key: str, hold_s: float) -> None:
        self.key_down(key)
        time.sleep(hold_s)
        self.key_up(key)

    def close(self) -> None:
        for key in list(self._down):  # never leave a key stuck down
            self.key_up(key)


class WinWatcher:
    """Polls the watched keys and calls on_key(name, down) when one changes."""

    def __init__(self, on_key, api=None):
        self.on_key = on_key
        self._api = api
        self._keys: dict[str, int] = {}
        self._state: dict[str, bool] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.problem = ""

    def set_keys(self, names) -> None:
        with self._lock:
            self._keys = {n: VK[n][0] for n in names if n}
            self._state = {n: False for n in self._keys}

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="key-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def is_down(self, name: str) -> bool:
        with self._lock:
            return self._state.get(name, False)

    def poll_once(self) -> None:
        api = self._api
        with self._lock:
            keys = dict(self._keys)
        for name, vk in keys.items():
            down = api.is_down(vk)
            with self._lock:
                if name not in self._state or self._state[name] == down:
                    continue
                self._state[name] = down
            self.on_key(name, down)

    def _run(self) -> None:
        if self._api is None:
            self._api = Win32()
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(POLL_S)
