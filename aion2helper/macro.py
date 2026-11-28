"""The macro: while you hold the trigger, hold the in-game macro key and the basic attack.

Every class uses the game's own macro for its skill chain, so this is all the helper does:
  * trigger down  -> macro key down, and the basic attack held down (or tapped on an interval)
  * trigger up    -> both let go
  * on/off key    -> pauses and resumes the whole thing while it runs
Holding the basic attack (rather than tapping it) is what lets the macro cancel its swing on
every hit, which is where the extra damage and mana come from.
"""

from __future__ import annotations

import logging
import sys
import threading
import time

from .keys import pretty

log = logging.getLogger("aion2helper")
TICK_S = 0.01


class DryOutput:
    """Test run: says what it would press instead of pressing it."""

    def __init__(self):
        self.events: list[tuple[str, str]] = []

    def key_down(self, key: str) -> None:
        self.events.append(("down", key))
        log.info("[test run] would hold %s", pretty(key))

    def key_up(self, key: str) -> None:
        self.events.append(("up", key))
        log.info("[test run] would let go of %s", pretty(key))

    def tap(self, key: str, hold_s: float) -> None:
        self.events.append(("tap", key))
        log.info("[test run] would tap %s", pretty(key))

    def close(self) -> None:
        pass


def make_output(keys: list[str]):
    if sys.platform == "win32":
        from .input_windows import WinOutput
        return WinOutput(keys)
    from .input_linux import UInputOutput
    return UInputOutput(keys)


class Macro:
    def __init__(self, settings, output_factory=make_output, clock=time.monotonic):
        self.settings = settings
        self.output_factory = output_factory
        self.clock = clock
        self.running = False
        self.dry_run = False
        self.enabled = True
        self.held = False
        self.held_since = 0.0
        self.error = ""
        self._out = None
        self._down: set[str] = set()
        self._next_tap = 0.0
        self._keys: tuple[str, str] = ("", "")
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ── control ────────────────────────────────────────────────────────────
    def start(self, dry_run: bool = False) -> None:
        with self._lock:
            if self.running:
                self.stop()
            m = self.settings.snapshot()["macro"]
            keys = [m["macro_key"]] + ([m["attack_key"]] if m["attack_mode"] != "off" else [])
            self.error = ""
            self._out = DryOutput() if dry_run else self.output_factory(keys)
            self._keys = self._device_keys(m)
            self.dry_run = dry_run
            self.enabled = True
            self.running = True
            self._down.clear()
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="macro", daemon=True)
            self._thread.start()
            log.info("macro %s: hold %s to play; %s switches it on and off",
                     "test run" if dry_run else "started", pretty(m["trigger"]), pretty(m["toggle_key"]))

    def stop(self) -> None:
        with self._lock:
            if not self.running:
                return
            self.running = False
            self._stop.set()
            self._wake.set()
            t = self._thread
        if t and t is not threading.current_thread():
            t.join(timeout=2)
        with self._lock:
            self._release_all()
            if self._out is not None:
                try:
                    self._out.close()
                except Exception:
                    pass
                self._out = None
            log.info("macro stopped")

    def settings_changed(self) -> None:
        """A changed macro or attack key needs new virtual devices; anything else applies at once."""
        with self._lock:
            if not self.running:
                return
            if self._device_keys(self.settings.snapshot()["macro"]) != self._keys:
                dry = self.dry_run
                self.stop()
                self.start(dry)
        self._wake.set()

    @staticmethod
    def _device_keys(m: dict) -> tuple:
        return (m["macro_key"], m["attack_key"] if m["attack_mode"] != "off" else "")

    # ── input from the key watcher ─────────────────────────────────────────
    def on_key(self, name: str, down: bool) -> None:
        m = self.settings.data["macro"]
        if name == m["trigger"]:
            if down != self.held:
                self.held = down
                self.held_since = self.clock()
            self._wake.set()
        elif name == m["toggle_key"] and down and self.running:
            self.enabled = not self.enabled
            log.warning("macro %s", "ON" if self.enabled else "OFF (on/off key)")
            self._wake.set()

    # ── the loop ───────────────────────────────────────────────────────────
    def wanted(self, now: float) -> set[str]:
        m = self.settings.data["macro"]
        if not (self.running and self.enabled and self.held):
            return set()
        if now - self.held_since < m["start_delay_ms"] / 1000:
            return set()
        keys = {m["macro_key"]}
        if m["attack_mode"] == "hold":
            keys.add(m["attack_key"])
        return keys

    def step(self, now: float) -> None:
        """One tick: put the right keys down, let the others go, and tap in tap mode."""
        m = self.settings.data["macro"]
        out = self._out
        if out is None:
            return
        want = self.wanted(now)
        for key in sorted(self._down - want):
            out.key_up(key)
            self._down.discard(key)
        # The macro key goes down first, then the basic attack.
        for key in sorted(want - self._down, key=lambda k: k != m["macro_key"]):
            out.key_down(key)
            self._down.add(key)
        if want and m["attack_mode"] == "tap":
            if now >= self._next_tap:
                out.tap(m["attack_key"], m["tap_hold_ms"] / 1000)
                self._next_tap = now + m["attack_interval"]
        elif not want:
            self._next_tap = 0.0

    def _release_all(self) -> None:
        if self._out is None:
            return
        for key in sorted(self._down):
            try:
                self._out.key_up(key)
            except Exception:
                pass
        self._down.clear()

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                with self._lock:
                    if not self.running:
                        break
                    self.step(self.clock())
                self._wake.wait(TICK_S)
                self._wake.clear()
        except Exception as e:
            self.error = str(e)
            log.error("macro stopped: %s", e)
            with self._lock:
                self.running = False
                self._release_all()

    def status(self) -> dict:
        return {"running": self.running, "dry_run": self.dry_run, "enabled": self.enabled,
                "held": self.held, "active": bool(self._down) or (self.running and self.enabled and self.held),
                "error": self.error}
