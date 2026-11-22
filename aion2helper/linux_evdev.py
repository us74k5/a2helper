"""The little of the Linux input API this program needs, in plain Python (no compiler needed).

python-evdev has no prebuilt packages, and Bazzite has no compiler to build it, so this talks
to the kernel directly: reading /dev/input/event* and creating devices through /dev/uinput.
Both are stable kernel interfaces (linux/input.h and linux/uinput.h).
"""

from __future__ import annotations

import fcntl
import glob
import os
import struct

EV_SYN, EV_KEY, EV_REL = 0x00, 0x01, 0x02
SYN_REPORT = 0
REL_X, REL_Y = 0x00, 0x01
KEY_MAX = 0x2FF
BUS_USB = 0x03

# struct input_event: struct timeval, __u16 type, __u16 code, __s32 value
EVENT = struct.Struct("llHHi")

# Key and button codes (linux/input-event-codes.h) for every key this program can use.
CODES = {
    "KEY_ESC": 1, "KEY_1": 2, "KEY_2": 3, "KEY_3": 4, "KEY_4": 5, "KEY_5": 6, "KEY_6": 7, "KEY_7": 8,
    "KEY_8": 9, "KEY_9": 10, "KEY_0": 11, "KEY_MINUS": 12, "KEY_EQUAL": 13, "KEY_BACKSPACE": 14,
    "KEY_TAB": 15, "KEY_Q": 16, "KEY_W": 17, "KEY_E": 18, "KEY_R": 19, "KEY_T": 20, "KEY_Y": 21,
    "KEY_U": 22, "KEY_I": 23, "KEY_O": 24, "KEY_P": 25, "KEY_LEFTBRACE": 26, "KEY_RIGHTBRACE": 27,
    "KEY_ENTER": 28, "KEY_LEFTCTRL": 29, "KEY_A": 30, "KEY_S": 31, "KEY_D": 32, "KEY_F": 33,
    "KEY_G": 34, "KEY_H": 35, "KEY_J": 36, "KEY_K": 37, "KEY_L": 38, "KEY_SEMICOLON": 39,
    "KEY_APOSTROPHE": 40, "KEY_GRAVE": 41, "KEY_LEFTSHIFT": 42, "KEY_BACKSLASH": 43, "KEY_Z": 44,
    "KEY_X": 45, "KEY_C": 46, "KEY_V": 47, "KEY_B": 48, "KEY_N": 49, "KEY_M": 50, "KEY_COMMA": 51,
    "KEY_DOT": 52, "KEY_SLASH": 53, "KEY_RIGHTSHIFT": 54, "KEY_KPASTERISK": 55, "KEY_LEFTALT": 56,
    "KEY_SPACE": 57, "KEY_CAPSLOCK": 58, "KEY_F1": 59, "KEY_F2": 60, "KEY_F3": 61, "KEY_F4": 62,
    "KEY_F5": 63, "KEY_F6": 64, "KEY_F7": 65, "KEY_F8": 66, "KEY_F9": 67, "KEY_F10": 68,
    "KEY_SCROLLLOCK": 70, "KEY_KP7": 71, "KEY_KP8": 72, "KEY_KP9": 73, "KEY_KPMINUS": 74,
    "KEY_KP4": 75, "KEY_KP5": 76, "KEY_KP6": 77, "KEY_KPPLUS": 78, "KEY_KP1": 79, "KEY_KP2": 80,
    "KEY_KP3": 81, "KEY_KP0": 82, "KEY_KPDOT": 83, "KEY_F11": 87, "KEY_F12": 88, "KEY_KPENTER": 96,
    "KEY_RIGHTCTRL": 97, "KEY_KPSLASH": 98, "KEY_RIGHTALT": 100, "KEY_HOME": 102, "KEY_UP": 103,
    "KEY_PAGEUP": 104, "KEY_LEFT": 105, "KEY_RIGHT": 106, "KEY_END": 107, "KEY_DOWN": 108,
    "KEY_PAGEDOWN": 109, "KEY_INSERT": 110, "KEY_DELETE": 111, "KEY_PAUSE": 119,
    "KEY_F13": 183, "KEY_F14": 184, "KEY_F15": 185, "KEY_F16": 186, "KEY_F17": 187, "KEY_F18": 188,
    "KEY_F19": 189, "KEY_F20": 190, "KEY_F21": 191, "KEY_F22": 192, "KEY_F23": 193, "KEY_F24": 194,
    "BTN_LEFT": 0x110, "BTN_RIGHT": 0x111, "BTN_MIDDLE": 0x112, "BTN_SIDE": 0x113, "BTN_EXTRA": 0x114,
}

# ioctl numbers, built the way linux/ioctl.h builds them
_IOC_WRITE, _IOC_READ = 1, 2


def _IOC(direction: int, type_: str, nr: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (ord(type_) << 8) | nr


def EVIOCGNAME(length: int) -> int:
    return _IOC(_IOC_READ, "E", 0x06, length)


def EVIOCGBIT(ev: int, length: int) -> int:
    return _IOC(_IOC_READ, "E", 0x20 + ev, length)


UI_DEV_CREATE = _IOC(0, "U", 1, 0)
UI_DEV_DESTROY = _IOC(0, "U", 2, 0)
# struct uinput_setup { struct input_id {u16 bustype, vendor, product, version}; char name[80]; u32 ff_effects_max; }
UINPUT_SETUP = struct.Struct("HHHH80sI")
UI_DEV_SETUP = _IOC(_IOC_WRITE, "U", 3, UINPUT_SETUP.size)
UI_SET_EVBIT = _IOC(_IOC_WRITE, "U", 100, 4)
UI_SET_KEYBIT = _IOC(_IOC_WRITE, "U", 101, 4)
UI_SET_RELBIT = _IOC(_IOC_WRITE, "U", 102, 4)


class InputDevice:
    """One /dev/input/event* device, opened read-only and non-blocking. Never grabbed."""

    def __init__(self, path: str):
        self.path = path
        self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            buf = bytearray(256)
            fcntl.ioctl(self.fd, EVIOCGNAME(len(buf)), buf)
            self.name = bytes(buf).split(b"\0", 1)[0].decode(errors="replace")
            bits = bytearray(KEY_MAX // 8 + 1)
            fcntl.ioctl(self.fd, EVIOCGBIT(EV_KEY, len(bits)), bits)
            self.keys = {c for c in range(len(bits) * 8) if bits[c // 8] >> (c % 8) & 1}
        except OSError:
            os.close(self.fd)
            raise
        self._buf = b""

    def read(self):
        """(type, code, value) for every event waiting; raises BlockingIOError if none."""
        data = os.read(self.fd, EVENT.size * 64)
        if not data:
            raise OSError("device gone")
        data = self._buf + data
        n = len(data) // EVENT.size * EVENT.size
        self._buf = data[n:]
        for off in range(0, n, EVENT.size):
            _, _, etype, code, value = EVENT.unpack_from(data, off)
            yield etype, code, value

    def close(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None


def list_devices() -> list[str]:
    return sorted(glob.glob("/dev/input/event*"), key=lambda p: int(p.rsplit("event", 1)[1] or 0))


class UInputDevice:
    """A virtual keyboard or mouse that the game sees like any other."""

    def __init__(self, codes: list[int], name: str, mouse: bool = False, path: str = "/dev/uinput", ioctl=None):
        self._ioctl = ioctl or fcntl.ioctl
        self.fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
        try:
            self._ioctl(self.fd, UI_SET_EVBIT, EV_KEY)
            for c in codes:
                self._ioctl(self.fd, UI_SET_KEYBIT, c)
            if mouse:  # pointer axes too, or the system does not treat it as a mouse
                self._ioctl(self.fd, UI_SET_EVBIT, EV_REL)
                self._ioctl(self.fd, UI_SET_RELBIT, REL_X)
                self._ioctl(self.fd, UI_SET_RELBIT, REL_Y)
            setup = UINPUT_SETUP.pack(BUS_USB, 0x1209, 0xA201 if mouse else 0xA200, 1,
                                      name.encode()[:79], 0)
            self._ioctl(self.fd, UI_DEV_SETUP, setup)
            self._ioctl(self.fd, UI_DEV_CREATE)
        except OSError:
            os.close(self.fd)
            raise

    def write(self, etype: int, code: int, value: int) -> None:
        os.write(self.fd, EVENT.pack(0, 0, etype, code, value))

    def syn(self) -> None:
        self.write(EV_SYN, SYN_REPORT, 0)

    def close(self) -> None:
        if self.fd is None:
            return
        try:
            self._ioctl(self.fd, UI_DEV_DESTROY)
        except OSError:
            pass
        os.close(self.fd)
        self.fd = None
