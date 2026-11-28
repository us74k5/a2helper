"""Wires it together: the Qt overlay on the main thread, the page, the macro and the key watcher."""

from __future__ import annotations

import collections
import json
import logging
import os
import sys
import threading

from . import APP_NAME, __version__
from .keys import pretty
from .settings import HOTKEY_NAMES, Settings, SettingsError, config_dir, data_dir

log = logging.getLogger("aion2helper")
IS_WINDOWS = sys.platform == "win32"


class RingHandler(logging.Handler):
    def __init__(self, n: int = 300):
        super().__init__()
        self.lines: collections.deque[str] = collections.deque(maxlen=n)

    def emit(self, record):
        try:
            self.lines.append(self.format(record))
        except Exception:
            pass


def setup_logging(debug: bool = False) -> RingHandler:
    ring = RingHandler()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
    ring.setFormatter(fmt)
    log.setLevel(logging.DEBUG if debug else logging.INFO)
    log.addHandler(ring)
    try:
        d = data_dir()
        d.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(d / "aion2-helper.log", mode="w", encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except OSError:
        pass
    if sys.stderr is not None:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        log.addHandler(sh)
    return ring


def make_watcher(on_key):
    if IS_WINDOWS:
        from .input_windows import WinWatcher
        return WinWatcher(on_key)
    from .input_linux import EvdevWatcher
    return EvdevWatcher(on_key)


def input_problem() -> str:
    if IS_WINDOWS:
        return ""
    from .input_linux import input_permission_problem
    return input_permission_problem()


class Api:
    """What the page can ask for. Called on the web server's threads."""

    def __init__(self, settings, macro, overlay, watcher, ring, quit_cb, parser_dir):
        self.settings, self.macro, self.overlay, self.watcher = settings, macro, overlay, watcher
        self.ring, self.quit_cb, self.parser_dir = ring, quit_cb, parser_dir
        self.lock = threading.Lock()
        self.fetching = False

    def state(self) -> dict:
        from .capture.pcap import NPCAP_URL
        from .parser_fetch import have_parser

        cfg = self.settings.snapshot()
        return {"version": __version__, "platform": "windows" if IS_WINDOWS else "linux",
                "macro": cfg["macro"], "hotkeys": cfg["hotkeys"],
                "overlay_settings": {k: cfg["overlay"][k] for k in ("enabled", "char", "alert_sound", "elevate")},
                "macro_status": self.macro.status(), "overlay": self.overlay.status(),
                "input_problem": input_problem() or getattr(self.watcher, "problem", ""),
                "parser": {"present": have_parser(self.parser_dir), "fetching": self.fetching},
                "notes": self.settings.notes, "npcap_url": NPCAP_URL,
                "config_path": str(self.settings.path), "log": list(self.ring.lines)[-200:]}

    def watched_keys(self) -> set[str]:
        cfg = self.settings.data
        return {cfg["macro"]["trigger"], cfg["macro"]["toggle_key"]} | {
            cfg["hotkeys"].get(n, "") for n in HOTKEY_NAMES} - {""}

    def post(self, path: str, body: dict):
        with self.lock:
            if path == "/api/macro/start":
                problem = "" if body.get("dry_run") else input_problem()
                if problem:
                    raise RuntimeError(problem)
                self.macro.start(bool(body.get("dry_run")))
                return {}
            if path == "/api/macro/stop":
                self.macro.stop()
                return {}
            if path == "/api/settings":
                section, values = body["section"], body["values"]
                if not isinstance(values, dict):
                    raise SettingsError("values must be an object")
                try:
                    self.settings.update(section, values)
                except SettingsError as e:
                    raise RuntimeError(str(e)) from None
                if section in ("macro", "hotkeys"):
                    self.watcher.set_keys(self.watched_keys())
                    self.macro.settings_changed()
                if section == "overlay" and ("char" in values or "elevate" in values):
                    self.overlay.post("restart-capture")
                if section == "overlay" and "enabled" in values:
                    self.overlay.post("enable" if values["enabled"] else "disable")
                return {}
            if path == "/api/overlay":
                cmd = body["cmd"]
                if cmd not in ("map", "map-off", "calibrate", "calibrate-reset", "radar", "arrow",
                               "arrow-mode", "restart-capture"):
                    raise SettingsError(f"unknown overlay command {cmd!r}")
                self.overlay.post(cmd)
                return {}
            if path == "/api/parser/update":
                if self.fetching:
                    return {"message": "already downloading"}
                self.fetching = True
                threading.Thread(target=self._fetch, daemon=True).start()
                return {"message": "downloading the cube parser…"}
            if path == "/api/quit":
                threading.Thread(target=self.quit_cb, daemon=True).start()
                return {}
        raise KeyError(path)

    def _fetch(self) -> None:
        from .parser_fetch import fetch_parser

        try:
            msg = fetch_parser(self.parser_dir)
            log.info("%s", msg)
            if self.settings.data["overlay"].get("enabled", True):
                self.overlay.post("restart-capture")
        except Exception as e:
            log.error("could not download the cube parser: %s", e)
        finally:
            self.fetching = False


def already_running(lock_path) -> str | None:
    from .web import is_our_page

    try:
        old = json.loads(lock_path.read_text())
        if is_our_page(int(old["port"])):
            return f"http://127.0.0.1:{int(old['port'])}/"
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def run(open_browser: bool | None = None, debug: bool = False) -> int:
    from .web import WebServer, open_url

    lock_path = config_dir() / "running.json"
    url = already_running(lock_path)
    if url:  # opening the app again just brings the page back
        print(f"{APP_NAME} is already running at {url}", flush=True)
        if open_browser is not False:
            open_url(url)
        return 0

    ring = setup_logging(debug)
    if not IS_WINDOWS:
        # Overlays need X11/XWayland: Wayland does not let a window place itself or stay on top.
        os.environ.setdefault("QT_QPA_PLATFORM", "xcb")

    from PySide6.QtCore import QObject, QTimer, Signal
    from PySide6.QtWidgets import QApplication

    from .macro import Macro
    from .overlay import OverlayController
    from .parser_fetch import have_parser

    qapp = QApplication.instance() or QApplication(sys.argv)
    qapp.setApplicationName("aion2-helper")
    qapp.setQuitOnLastWindowClosed(False)

    settings = Settings()
    for note in settings.notes:
        log.info("first run: %s", note)
    parser_dir = data_dir() / "upstream"
    macro = Macro(settings)
    overlay = OverlayController(settings, parser_dir)

    def on_key(name: str, down: bool) -> None:
        macro.on_key(name, down)
        if down:
            hk = settings.data["hotkeys"]
            for cmd in HOTKEY_NAMES:
                if hk.get(cmd) == name:
                    overlay.post(cmd)

    watcher = make_watcher(on_key)

    class Quitter(QObject):
        go = Signal()

    quitter = Quitter()
    quitter.go.connect(qapp.quit)

    api = Api(settings, macro, overlay, watcher, ring, quitter.go.emit, parser_dir)
    watcher.set_keys(api.watched_keys())
    watcher.start()
    web = WebServer(api)
    web.start()
    config_dir().mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps({"pid": os.getpid(), "port": web.port_box[0]}))
    log.info("%s %s: the page is at %s", APP_NAME, __version__, web.url)
    m = settings.data["macro"]
    log.info("macro keys: trigger %s, macro %s, attack %s (%s)", pretty(m["trigger"]), pretty(m["macro_key"]),
             pretty(m["attack_key"]), m["attack_mode"])

    if not have_parser(parser_dir):
        api.post("/api/parser/update", {})  # first run: fetch it, then the capture starts on its own
    QTimer.singleShot(0, overlay.start)
    if open_browser if open_browser is not None else settings.data.get("open_browser", True):
        QTimer.singleShot(400, lambda: open_url(web.url))

    try:
        code = qapp.exec()
    finally:
        macro.stop()
        watcher.stop()
        overlay.shutdown()
        web.stop()
        try:
            lock_path.unlink()
        except OSError:
            pass
    return code
