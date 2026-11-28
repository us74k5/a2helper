"""Starting and stopping the capture, and handing its snapshots to the overlay.

Linux:   the root-owned helper in /usr/local/libexec/aion2-helper, started with pkexec, sends
         JSON lines over a pipe (see linux_helper.py).
Windows: Npcap, read in this process by one thread per network adapter.

Set AION2_HELPER_REPLAY=/path/file.pcap to replay a capture instead (testing), and
AION2_HELPER_CAPTURE=pcap to use the pcap path on Linux too.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import core

log = logging.getLogger("aion2helper")

SYSTEM_HELPER_DIR = Path("/usr/local/libexec/aion2-helper")
SYSTEM_HELPER = SYSTEM_HELPER_DIR / "aion2-capture"
HERE = Path(__file__).resolve().parent


class CaptureFeed:
    def __init__(self, parser_dir: Path, on_snapshot, on_status):
        self.parser_dir = Path(parser_dir)
        self.on_snapshot = on_snapshot
        self.on_status = on_status       # (message, is_error)
        self.proc: subprocess.Popen | None = None
        self.running = False
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._handles = []

    # ── which way ──────────────────────────────────────────────────────────
    @staticmethod
    def use_pcap() -> bool:
        return sys.platform == "win32" or os.environ.get("AION2_HELPER_CAPTURE") == "pcap"

    def start(self, char: str = "", elevate: str = "pkexec") -> None:
        if self.running:
            return
        self._stop.clear()
        if self.use_pcap():
            self._start_pcap(char)
        else:
            self._start_linux_helper(char, elevate)

    def stop(self) -> None:
        self._stop.set()
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.close()  # the helper exits on stdin EOF (works though it was elevated)
            except OSError:
                pass
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        for t in self._threads:
            t.join(timeout=2)
        for h in self._handles:
            h.close()
        self._threads, self._handles = [], []
        if self.running:
            self.running = False
            self.on_status("Capture stopped.", False)

    # ── Linux: the root helper ─────────────────────────────────────────────
    def helper_cmd(self, char: str, elevate: str):
        args = ["--parser-dir", str(self.parser_dir)]
        # Ask this Python where its packages are instead of matching paths:
        # on Bazzite /home is a symlink to /var/home, so string comparisons can miss.
        import site
        for sp in site.getsitepackages():
            if os.path.isdir(sp):
                args += ["--site-packages", sp]
        if char:
            args += ["--char", char]
        replay = os.environ.get("AION2_HELPER_REPLAY")
        own = [sys.executable, str(HERE / "linux_helper.py")]
        if replay:
            return own + args + ["--replay", replay]
        if elevate == "none":  # e.g. you granted CAP_NET_RAW yourself
            return own + args
        # The root-owned copy installed by install.sh; never elevate user-writable code.
        if not SYSTEM_HELPER.exists():
            self.on_status(f"{SYSTEM_HELPER} is missing; re-run install.sh", True)
            return None
        return (["pkexec"] if elevate == "pkexec" else ["sudo", "-A"]) + [str(SYSTEM_HELPER), *args]

    def _start_linux_helper(self, char: str, elevate: str) -> None:
        cmd = self.helper_cmd(char, elevate)
        if not cmd:
            return
        try:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, text=True, bufsize=1)
        except OSError as exc:
            self.on_status(f"Could not start capture: {exc}", True)
            return
        self.running = True
        self.on_status("Starting capture (approve the password prompt)…", False)
        t = threading.Thread(target=self._read_helper, args=(self.proc,), name="capture-pipe", daemon=True)
        t.start()

    def _read_helper(self, proc) -> None:
        last_error = None
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            kind = msg.get("t")
            if kind == "snap":
                self.on_snapshot(msg)
            elif kind in ("info", "error"):
                if kind == "error":
                    last_error = msg.get("msg", "")
                self.on_status(msg.get("msg", ""), kind == "error")
        code = proc.wait()
        err = (proc.stderr.read() or "").strip()
        if err:
            log.warning("capture helper: %s", err)
        self.running = False
        if self._stop.is_set():
            return
        if code in (126, 127):
            self.on_status("Password prompt was cancelled; capture not running.", True)
        elif code:
            reason = last_error or (err.splitlines()[-1] if err else f"exit code {code}")
            self.on_status(f"Capture stopped: {reason}", True)
        else:
            self.on_status("Capture finished.", False)

    # ── Windows (or AION2_HELPER_CAPTURE=pcap): Npcap in this process ──────
    def _start_pcap(self, char: str) -> None:
        from .pcap import Pcap, PcapError, PCAP_IF_LOOPBACK, bpf_for

        try:
            war = core.load_parser(str(self.parser_dir))
        except Exception as e:
            self.on_status(f"parser load failed: {type(e).__name__}: {e}", True)
            return
        session = core.Session(war, self.on_snapshot, char=char)
        try:
            pcap = Pcap()
            replay = os.environ.get("AION2_HELPER_REPLAY")
            if replay:
                handles = [pcap.open_offline(replay)]
            else:
                handles = []
                problems = []
                for name, desc, flags in pcap.devices():
                    if flags & PCAP_IF_LOOPBACK:
                        continue
                    try:
                        handles.append(pcap.open_live(name, bpf_for(core.SERVER_PORTS)))
                        log.info("capturing on %s", desc or name)
                    except PcapError as e:
                        problems.append(f"{desc or name}: {e}")
                if not handles:
                    hint = (" If you ticked \"Restrict Npcap driver's access to Administrators only\" when "
                            "installing Npcap, reinstall it without that option." if sys.platform == "win32" else "")
                    raise PcapError("no network adapter could be opened" +
                                    (f" ({problems[0]})." if problems else ".") + hint)
        except PcapError as e:
            self.on_status(str(e), True)
            return
        self._handles = handles
        self.running = True
        self.on_status(("replaying " + os.path.basename(replay)) if replay else
                       f"capturing on {len(handles)} adapter(s), ports " + ",".join(map(str, core.SERVER_PORTS)), False)
        for h in handles:
            t = threading.Thread(target=self._read_pcap, args=(h, session, bool(replay)), name="capture", daemon=True)
            t.start()
            self._threads.append(t)
        t = threading.Thread(target=self._publish, args=(session,), name="capture-publish", daemon=True)
        t.start()
        self._threads.append(t)

    def _read_pcap(self, handle, session, replay: bool) -> None:
        from .pcap import Handle, PcapError

        while not self._stop.is_set():
            try:
                frame = handle.next()
            except PcapError as e:
                if not self._stop.is_set():
                    self.on_status(f"capture error: {e}", True)
                return
            if frame is None:  # end of a replay file
                session.maybe_send(force=True)
                self.on_status("replay complete", False)
                return
            if frame is Handle.TIMEOUT:
                continue
            ip = core.strip_link(handle.linktype, frame)
            if ip is not None:
                session.handle(ip)
                if replay:
                    session.maybe_send(force=True)

    def _publish(self, session) -> None:
        last_beat = time.monotonic()
        while not self._stop.wait(0.05):
            session.maybe_send()
            if time.monotonic() - last_beat > 2.0:
                session.maybe_send(force=True)
                last_beat = time.monotonic()
