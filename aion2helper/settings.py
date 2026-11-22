"""Settings: one JSON file for the macro, the overlay and the hotkeys.

Linux:   ~/.config/aion2-helper/config.json
Windows: %APPDATA%\\aion2-helper\\config.json

The first time it runs it picks up your old Combat Helper keys and your cube overlay
calibration, if it finds them, so nobody has to calibrate the map again.
"""

from __future__ import annotations

import copy
import json
import os
import sys
import threading
from pathlib import Path

from . import APP_ID
from .keys import valid_key

IS_WINDOWS = sys.platform == "win32"

ATTACK_MODES = ("hold", "tap", "off")

DEFAULTS: dict = {
    "macro": {
        "trigger": "BTN_RIGHT",        # hold this to play. Unbind it in the game.
        "toggle_key": "KEY_KPMINUS",   # switches the macro on and off while it runs
        "macro_key": "KEY_F9",         # your in-game macro (the whole skill chain lives there)
        "attack_key": "BTN_LEFT",      # the basic attack
        "attack_mode": "hold",         # hold: held the whole time (the cancel-weave); tap: on an interval
        "attack_interval": 0.6,        # seconds between taps in tap mode
        "tap_hold_ms": 40,             # how long a tap holds the key down
        "start_delay_ms": 0,           # wait this long after the trigger goes down before holding keys
    },
    "hotkeys": {                       # read-only: the game still sees these keys
        "map": "KEY_KPPLUS",
        "calibrate": "KEY_KPASTERISK",
        "radar": "",
        "arrow": "",
    },
    "overlay": {
        "enabled": True,               # start the capture and show the overlay when the app opens
        "char": "",
        "elevate": "pkexec",           # Linux only: pkexec | sudo | none
        "alert_sound": True,
        "radar": {"visible": True, "x": None, "y": 80, "size": 240, "range_m": 150.0},
        # mode "heading": points relative to the way you're moving; "north": relative to map-up.
        "arrow": {"visible": True, "x": None, "y": 90, "size": 220, "mode": "heading"},
        "map": {
            "mode": "fixed",               # fixed: map stays put | follow: map is centered on you
            "m": [0.0, 0.02, -0.02, 0.0],  # 2x2 world(cm) -> screen(px), row-major
            "t": [960.0, 540.0],
            "anchor": [960.0, 540.0],
            "points": [],
        },
    },
    "open_browser": True,
}

# Slider limits: key -> (min, max)
LIMITS = {
    "attack_interval": (0.2, 5.0),
    "tap_hold_ms": (15, 150),
    "start_delay_ms": (0, 500),
}
HOTKEY_NAMES = ("map", "calibrate", "radar", "arrow")


class SettingsError(ValueError):
    pass


def config_dir() -> Path:
    if os.environ.get("AION2_HELPER_CONFIG"):
        return Path(os.environ["AION2_HELPER_CONFIG"])
    if IS_WINDOWS:
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / APP_ID


def data_dir() -> Path:
    """Downloaded files (the cube parser) and the log."""
    if os.environ.get("AION2_HELPER_DATA"):
        return Path(os.environ["AION2_HELPER_DATA"])
    if IS_WINDOWS:
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / APP_ID


def deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# ── migration from the two old programs ────────────────────────────────────

def _read_json(path: Path) -> dict:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _old_cube_config() -> dict:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    old = _read_json(base / "aion-cube-overlay" / "config.json")
    return {k: old[k] for k in ("char", "elevate", "alert_sound", "radar", "arrow", "map") if k in old}


def _old_helper_config() -> dict:
    if IS_WINDOWS:
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    s = _read_json(base / "combat-helper" / "settings.json")
    out: dict = {}
    g = s.get("general") or {}
    for k in ("trigger", "toggle_key", "tap_hold_ms"):
        if k in g:
            out[k] = g[k]
    c = (s.get("classes") or {}).get(s.get("class") or "chanter") or {}
    if c.get("macro_key"):
        out["macro_key"] = c["macro_key"]
    if "hold_weave" in c:
        out["attack_mode"] = "hold" if c["hold_weave"] else "tap"
    if "weave_interval" in c:
        out["attack_interval"] = c["weave_interval"]
    return out


def migrate() -> tuple[dict, list[str]]:
    cfg: dict = {}
    notes = []
    helper = _old_helper_config()
    if helper:
        cfg["macro"] = helper
        notes.append("took your keys from Combat Helper")
    cube = _old_cube_config()
    if cube:
        cfg["overlay"] = cube
        notes.append("took your map calibration from the cube overlay")
    return cfg, notes


# ── validation ─────────────────────────────────────────────────────────────

def _clamp(key: str, value) -> float:
    lo, hi = LIMITS[key]
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise SettingsError(f"{key} must be a number") from None
    return max(lo, min(hi, v))


def validate(cfg: dict) -> None:
    m = cfg["macro"]
    for k in ("trigger", "toggle_key", "macro_key", "attack_key"):
        if not valid_key(m[k]):
            raise SettingsError(f"{k}: unknown key {m[k]!r}")
    if m["attack_mode"] not in ATTACK_MODES:
        raise SettingsError(f"attack_mode must be one of {', '.join(ATTACK_MODES)}")
    pressed = {m["macro_key"]} | ({m["attack_key"]} if m["attack_mode"] != "off" else set())
    watched = {"trigger": m["trigger"], "on/off key": m["toggle_key"]}
    for name in HOTKEY_NAMES:
        k = cfg["hotkeys"].get(name, "")
        if k:
            if not valid_key(k):
                raise SettingsError(f"hotkey {name}: unknown key {k!r}")
            watched[f"{name} hotkey"] = k
    seen: dict[str, str] = {}
    for label, key in watched.items():
        if key in pressed:
            raise SettingsError(f"the {label} ({key}) is also a key the macro presses; pick another")
        if key in seen:
            raise SettingsError(f"the {label} and the {seen[key]} are both {key}; pick another")
        seen[key] = label
    if m["macro_key"] == m["attack_key"] and m["attack_mode"] != "off":
        raise SettingsError("the macro key and the basic attack are the same key")


def normalize(cfg: dict) -> dict:
    m = cfg["macro"]
    for k in LIMITS:
        v = _clamp(k, m[k])
        m[k] = int(v) if k.endswith("_ms") else round(v, 2)
    return cfg


class Settings:
    """Thread-safe holder; the page, the macro and the overlay all read from it."""

    def __init__(self, path: Path | None = None):
        self.path = path or (config_dir() / "config.json")
        self.lock = threading.RLock()
        self.notes: list[str] = []
        self.data = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            raw = _read_json(self.path)
        else:
            raw, self.notes = migrate()
        cfg = deep_merge(copy.deepcopy(DEFAULTS), raw)
        try:
            normalize(cfg)
            validate(cfg)
        except SettingsError as e:
            self.notes.append(f"settings had a problem ({e}); the macro keys went back to the defaults")
            cfg["macro"] = copy.deepcopy(DEFAULTS["macro"])
            cfg["hotkeys"] = copy.deepcopy(DEFAULTS["hotkeys"])
        if not self.path.exists():
            self._write(cfg)
        return cfg

    def _write(self, cfg: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def snapshot(self) -> dict:
        with self.lock:
            return copy.deepcopy(self.data)

    def save(self) -> None:
        with self.lock:
            self._write(self.data)

    def update(self, section: str, values: dict) -> dict:
        """Change some values of one section (macro, hotkeys, overlay); refused as a whole if invalid."""
        if section not in ("macro", "hotkeys", "overlay"):
            raise SettingsError(f"unknown section {section!r}")
        with self.lock:
            trial = copy.deepcopy(self.data)
            target = trial[section]
            for k, v in values.items():
                if section == "overlay":
                    if k not in ("enabled", "char", "alert_sound", "elevate"):
                        raise SettingsError(f"overlay: {k} cannot be set here")
                    if k == "elevate" and v not in ("pkexec", "sudo", "none"):
                        raise SettingsError("elevate must be pkexec, sudo or none")
                    if k == "char":
                        v = str(v).strip()[:40]
                elif k not in DEFAULTS[section]:
                    raise SettingsError(f"{section}: unknown setting {k!r}")
                target[k] = v
            normalize(trial)
            validate(trial)
            # In place: the overlay windows hold references to the nested dicts (radar, arrow, map).
            live = self.data[section]
            for k in (values if section == "overlay" else trial[section]):
                live[k] = trial[section][k]
            self._write(self.data)
            return copy.deepcopy(self.data)
