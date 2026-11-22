"""Key names and the Windows virtual-key table.

Keys are spelled the evdev way everywhere (KEY_F9, KEY_KPMINUS, BTN_RIGHT), on Windows too,
so the settings file is the same on both systems.
"""

from __future__ import annotations

# name -> (Windows virtual-key code, extended key?)
VK: dict[str, tuple[int, bool]] = {}
for _i in range(26):
    VK[f"KEY_{chr(65 + _i)}"] = (65 + _i, False)
for _i in range(10):
    VK[f"KEY_{_i}"] = (48 + _i, False)
    VK[f"KEY_KP{_i}"] = (96 + _i, False)
for _i in range(1, 25):
    VK[f"KEY_F{_i}"] = (111 + _i, False)
VK.update({
    "KEY_SPACE": (32, False), "KEY_TAB": (9, False), "KEY_ENTER": (13, False), "KEY_ESC": (27, False),
    "KEY_BACKSPACE": (8, False), "KEY_CAPSLOCK": (20, False),
    "KEY_GRAVE": (192, False), "KEY_MINUS": (189, False), "KEY_EQUAL": (187, False),
    "KEY_LEFTBRACE": (219, False), "KEY_RIGHTBRACE": (221, False), "KEY_SEMICOLON": (186, False),
    "KEY_APOSTROPHE": (222, False), "KEY_COMMA": (188, False), "KEY_DOT": (190, False),
    "KEY_SLASH": (191, False), "KEY_BACKSLASH": (220, False),
    "KEY_LEFTSHIFT": (160, False), "KEY_RIGHTSHIFT": (161, False),
    "KEY_LEFTCTRL": (162, False), "KEY_RIGHTCTRL": (163, True),
    "KEY_LEFTALT": (164, False), "KEY_RIGHTALT": (165, True),
    "KEY_INSERT": (45, True), "KEY_DELETE": (46, True), "KEY_HOME": (36, True), "KEY_END": (35, True),
    "KEY_PAGEUP": (33, True), "KEY_PAGEDOWN": (34, True),
    "KEY_UP": (38, True), "KEY_DOWN": (40, True), "KEY_LEFT": (37, True), "KEY_RIGHT": (39, True),
    "KEY_SCROLLLOCK": (145, False), "KEY_PAUSE": (19, False),
    "KEY_KPMINUS": (109, False), "KEY_KPPLUS": (107, False), "KEY_KPASTERISK": (106, False),
    "KEY_KPSLASH": (111, True), "KEY_KPDOT": (110, False), "KEY_KPENTER": (13, True),
    "BTN_LEFT": (1, False), "BTN_RIGHT": (2, False), "BTN_MIDDLE": (4, False),
    "BTN_SIDE": (5, False), "BTN_EXTRA": (6, False),
})
del _i

MOUSE_BUTTONS = ("BTN_LEFT", "BTN_RIGHT", "BTN_MIDDLE", "BTN_SIDE", "BTN_EXTRA")


def is_mouse(key: str) -> bool:
    return key.startswith("BTN_")


def valid_key(key: str) -> bool:
    return key in VK


def pretty(key: str) -> str:
    if not key:
        return "not set"
    if key.startswith("KEY_KP"):
        return "Num " + key[6:].lower()
    if key.startswith("BTN_"):
        return "Mouse " + key[4:].lower()
    return key[4:].capitalize()
