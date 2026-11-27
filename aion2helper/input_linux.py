"""Linux input: a virtual keyboard and mouse through uinput, and a read-only key watcher (evdev).

The game sees the virtual devices as ordinary ones, so this works under Proton on X11 and Wayland.
The watcher never grabs a device, so the game still sees every key you press.
"""

from __future__ import annotations

import logging
import os
import select
import threading
import time

from .linux_evdev import CODES, EV_KEY, InputDevice, UInputDevice, list_devices

log = logging.getLogger("aion2helper")

OWN_DEVICE_NAMES = ("aion2-helper", "combat-helper", "chanterbot", "ydotool")
RESCAN_S = 3.0


class UInputOutput:
    NAME = "aion2-helper virtual keyboard"
    MOUSE_NAME = "aion2-helper virtual mouse"

    def __init__(self, keys: list[str], device=UInputDevice):
        self._codes = {k: CODES[k] for k in keys}
        kbd = sorted(c for k, c in self._codes.items() if not k.startswith("BTN_"))
        btn = sorted(c for k, c in self._codes.items() if k.startswith("BTN_"))
        self._kbd = self._mouse = None
        self._down: set[str] = set()
        try:
            if kbd:
                self._kbd = device(kbd, self.NAME)
            if btn:
                self._mouse = device(btn, self.MOUSE_NAME, mouse=True)
        except OSError as e:
            self.close()
            raise RuntimeError("cannot open /dev/uinput. Run install.sh (it sets up the uinput rule and "
                               "the input group), then log out and back in once.") from e

    def _set(self, key: str, value: int) -> None:
        ui = self._mouse if key.startswith("BTN_") else self._kbd
        ui.write(EV_KEY, self._codes[key], value)
        ui.syn()

    def key_down(self, key: str) -> None:
        self._set(key, 1)
        self._down.add(key)

    def key_up(self, key: str) -> None:
        self._set(key, 0)
        self._down.discard(key)

    def tap(self, key: str, hold_s: float) -> None:
        self.key_down(key)
        time.sleep(hold_s)
        self.key_up(key)

    def close(self) -> None:
        for key in list(self._down):  # never leave a key stuck down
            try:
                self.key_up(key)
            except OSError:
                pass
        for ui in (self._kbd, self._mouse):
            if ui is not None:
                try:
                    ui.close()
                except OSError:
                    pass
        self._kbd = self._mouse = None


class EvdevWatcher:
    """Calls on_key(name, down) when a watched key or button changes, on any keyboard or mouse."""

    def __init__(self, on_key):
        self.on_key = on_key
        self._keys: dict[int, str] = {}
        self._devices: dict[str, object] = {}
        self._pressed: dict[tuple[str, int], bool] = {}
        self._lock = threading.Lock()
        self._skipped: dict[str, tuple] = {}    # path -> stat signature of a device not worth watching
        self._gen = 0                           # bumped when the watched keys change
        self._stop = threading.Event()
        self._rescan = threading.Event()
        self._thread: threading.Thread | None = None
        self._scanner: threading.Thread | None = None
        self.problem = ""

    def set_keys(self, names) -> None:
        with self._lock:
            self._keys = {CODES[n]: n for n in names if n}
            self._gen += 1
            for d in list(self._devices.values()):
                d.close()
            self._devices.clear()
            self._pressed.clear()
            self._skipped.clear()
        self._rescan.set()

    def start(self) -> None:
        self._stop.clear()
        self._rescan.set()
        # Looking for new devices runs on its own thread: opening every /dev/input device takes
        # hundreds of milliseconds on some machines, and a trigger release must never wait for that.
        self._scanner = threading.Thread(target=self._scan_loop, name="key-scan", daemon=True)
        self._scanner.start()
        self._thread = threading.Thread(target=self._run, name="key-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._rescan.set()
        for t in (self._thread, self._scanner):
            if t:
                t.join(timeout=2)
        with self._lock:
            for d in self._devices.values():
                d.close()
            self._devices.clear()

    def is_down(self, name: str) -> bool:
        with self._lock:
            return any(v for (path, code), v in self._pressed.items() if self._keys.get(code) == name)

    def _scan(self) -> None:
        with self._lock:
            want = set(self._keys)
            known = set(self._devices)
            gen = self._gen
        denied = 0
        for path in list_devices():
            if path in known:
                continue
            sig = _signature(path)
            if sig is not None and self._skipped.get(path) == sig:
                continue                # same device as last time, and it had none of our keys
            try:
                d = InputDevice(path)
            except PermissionError:
                denied += 1
                continue
            except OSError:
                continue
            if any(s in d.name.lower() for s in OWN_DEVICE_NAMES) or not (d.keys & want):
                d.close()
                if sig is not None:
                    self._skipped[path] = sig
                continue
            with self._lock:
                if gen != self._gen:    # the keys changed while we were looking; start over
                    d.close()
                    self._rescan.set()
                    return
                self._devices[path] = d
            log.info("watching %s", d.name)
        with self._lock:
            have = len(self._devices)
        if have:
            self.problem = ""
        elif denied:
            self.problem = ("cannot read your keyboard and mouse: your user is not in the input group yet "
                            "(run install.sh, then log out and back in once)")
        elif want:
            self.problem = "no keyboard or mouse with the chosen keys was found"

    def _drop(self, path: str) -> None:
        with self._lock:
            d = self._devices.pop(path, None)
            lost = [k for k in self._pressed if k[0] == path]
            for k in lost:
                was = self._pressed.pop(k)
                if was and k[1] in self._keys:
                    self.on_key(self._keys[k[1]], False)
        if d is not None:
            log.warning("lost input device %s", d.name)
            try:
                d.close()
            except OSError:
                pass

    def _scan_loop(self) -> None:
        while not self._stop.is_set():
            self._rescan.wait(RESCAN_S)
            self._rescan.clear()
            if self._stop.is_set():
                break
            try:
                self._scan()
            except Exception as e:  # never let the watcher die
                self.problem = f"input scan failed: {e}"

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                fds = {d.fd: (p, d) for p, d in self._devices.items()}
            if not fds:
                self._stop.wait(0.5)
                continue
            try:
                ready, _, _ = select.select(list(fds), [], [], 0.25)
            except (OSError, ValueError):
                self._rescan.set()
                continue
            for fd in ready:
                path, dev = fds[fd]
                try:
                    events = list(dev.read())
                except BlockingIOError:
                    continue
                except OSError:
                    self._drop(path)
                    continue
                for etype, code, value in events:
                    if etype != EV_KEY or value not in (0, 1):  # 2 = key repeat
                        continue
                    name = self._keys.get(code)
                    if name is None:
                        continue
                    with self._lock:
                        self._pressed[(path, code)] = bool(value)
                    self.on_key(name, bool(value))


def _signature(path: str):
    """Identifies the device behind a path, so a replugged device is looked at again."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_rdev, st.st_ino, st.st_ctime_ns)


def input_permission_problem() -> str:
    """A plain sentence if the macro cannot work yet, or ''."""
    if not os.access("/dev/uinput", os.W_OK):
        return ("cannot write to /dev/uinput yet. Run install.sh (it adds a uinput rule and puts you "
                "in the input group), then log out and back in once.")
    return ""
