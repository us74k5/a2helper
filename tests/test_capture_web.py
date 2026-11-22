import ctypes.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

from aion2helper.capture import core

HERE = Path(__file__).resolve().parent
STUB = HERE / "stub_parser"
ROOT = HERE.parent


def synthetic_pcap(path):
    subprocess.run([sys.executable, str(HERE / "make_synthetic_pcap.py"), str(path)], check=True,
                   stdout=subprocess.DEVNULL)


class TestLinkLayer(unittest.TestCase):
    IP = bytes([0x45]) + b"\0" * 19

    def test_ethernet(self):
        self.assertEqual(core.strip_link(1, b"\0" * 12 + b"\x08\x00" + self.IP), self.IP)
        self.assertEqual(core.strip_link(1, b"\0" * 12 + b"\x81\x00\0\x05\x08\x00" + self.IP), self.IP)
        self.assertIsNone(core.strip_link(1, b"\0" * 12 + b"\x86\xdd" + self.IP))  # IPv6

    def test_others(self):
        self.assertEqual(core.strip_link(0, b"\x02\0\0\0" + self.IP), self.IP)      # Windows loopback
        self.assertEqual(core.strip_link(101, self.IP), self.IP)
        self.assertEqual(core.strip_link(113, b"\0" * 16 + self.IP), self.IP)
        self.assertIsNone(core.strip_link(999, self.IP))


class TestSession(unittest.TestCase):
    def test_replay_through_stub_parser(self):
        with tempfile.TemporaryDirectory() as d:
            pcap = Path(d) / "t.pcap"
            synthetic_pcap(pcap)
            snaps = []
            war = core.load_parser(str(STUB))
            s = core.Session(war, snaps.append)
            for pkt in core.iter_pcap(str(pcap)):
                s.handle(pkt)
                s.maybe_send(force=True)
        last = snaps[-1]
        self.assertEqual(last["player"], [10000.0, 20000.0, 500.0])
        self.assertEqual(len(last["cubes"]), 1)
        self.assertEqual(last["pkts"], 3)

    def test_missing_parser(self):
        with tempfile.TemporaryDirectory() as d, self.assertRaises(FileNotFoundError):
            core.load_parser(d)


@unittest.skipUnless(ctypes.util.find_library("pcap"), "libpcap not installed")
class TestPcapBinding(unittest.TestCase):
    """The same ctypes code Windows runs against Npcap, here against libpcap."""

    def test_offline_with_filter(self):
        from aion2helper.capture.pcap import Handle, Pcap, bpf_for
        with tempfile.TemporaryDirectory() as d:
            pcap = Path(d) / "t.pcap"
            synthetic_pcap(pcap)
            p = Pcap()
            h = p.open_offline(str(pcap), bpf_for(core.SERVER_PORTS))
            frames = []
            while True:
                f = h.next()
                if f is None:
                    break
                if f is not Handle.TIMEOUT:
                    frames.append(core.strip_link(h.linktype, f))
            h.close()
            self.assertEqual(len(frames), 3)
            self.assertTrue(all(core.split_ipv4_tcp(f) for f in frames))
            h = p.open_offline(str(pcap), "tcp and port 1")
            self.assertIsNone(h.next())
            h.close()

    def test_feed_pcap_mode(self):
        from aion2helper.capture.feed import CaptureFeed
        with tempfile.TemporaryDirectory() as d:
            pcap = Path(d) / "t.pcap"
            synthetic_pcap(pcap)
            snaps, status = [], []
            os.environ["AION2_HELPER_CAPTURE"], os.environ["AION2_HELPER_REPLAY"] = "pcap", str(pcap)
            try:
                f = CaptureFeed(STUB, snaps.append, lambda m, e: status.append((m, e)))
                f.start()
                deadline = time.time() + 5
                while not any(m == "replay complete" for m, _ in status) and time.time() < deadline:
                    time.sleep(0.05)
                f.stop()
            finally:
                del os.environ["AION2_HELPER_CAPTURE"], os.environ["AION2_HELPER_REPLAY"]
        self.assertEqual(snaps[-1]["player"], [10000.0, 20000.0, 500.0])
        self.assertFalse(any(e for _, e in status), status)


class TestLinuxHelper(unittest.TestCase):
    def test_replay_subprocess(self):
        with tempfile.TemporaryDirectory() as d:
            pcap = Path(d) / "t.pcap"
            synthetic_pcap(pcap)
            out = subprocess.run([sys.executable, "-I", str(ROOT / "aion2helper" / "capture" / "linux_helper.py"),
                                  "--parser-dir", str(STUB), "--replay", str(pcap)],
                                 capture_output=True, text=True, timeout=20)
        lines = [json.loads(x) for x in out.stdout.splitlines()]
        snaps = [x for x in lines if x["t"] == "snap"]
        self.assertTrue(snaps, out.stderr)
        self.assertEqual(snaps[-1]["player"], [10000.0, 20000.0, 500.0])
        self.assertEqual(lines[-1], {"t": "info", "msg": "replay complete"})


class TestParserFetch(unittest.TestCase):
    def test_fetch_and_reject(self):
        from aion2helper.parser_fetch import fetch_parser, have_parser
        with tempfile.TemporaryDirectory() as d:
            good = (STUB / "working_aion_radar.py").as_uri()
            dest = Path(d) / "upstream"
            self.assertEqual(fetch_parser(dest, good), "cube parser downloaded")
            self.assertTrue(have_parser(dest))
            self.assertEqual(fetch_parser(dest, good), "the cube parser is already up to date")
            bad = Path(d) / "bad.py"
            bad.write_text("print('hi')")
            with self.assertRaises(RuntimeError):
                fetch_parser(dest, bad.as_uri())
            self.assertIn(b"PureStreamEngine", (dest / "working_aion_radar.py").read_bytes())


class FakeMacro:
    def __init__(self):
        self.calls = []

    def start(self, dry):
        self.calls.append(("start", dry))

    def stop(self):
        self.calls.append(("stop",))

    def settings_changed(self):
        self.calls.append(("changed",))

    def status(self):
        return {"running": False}


class FakeOverlay:
    def __init__(self):
        self.posted = []

    def post(self, cmd):
        self.posted.append(cmd)

    def status(self):
        return {"enabled": True}


class FakeWatcher:
    problem = ""

    def __init__(self):
        self.keys = None

    def set_keys(self, k):
        self.keys = set(k)


class TestWebApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from aion2helper.app import Api, RingHandler
        from aion2helper.settings import Settings
        from aion2helper.web import WebServer
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["XDG_CONFIG_HOME"] = cls.tmp.name
        cls.macro, cls.overlay, cls.watcher = FakeMacro(), FakeOverlay(), FakeWatcher()
        cls.quit = threading.Event()
        cls.api = Api(Settings(Path(cls.tmp.name) / "c.json"), cls.macro, cls.overlay, cls.watcher, RingHandler(),
                      cls.quit.set, Path(cls.tmp.name) / "upstream")
        cls.web = WebServer(cls.api, port=18766)
        cls.web.start()

    @classmethod
    def tearDownClass(cls):
        cls.web.stop()
        cls.tmp.cleanup()

    def req(self, path, body=None, token=None, host=None):
        r = urllib.request.Request(self.web.url.rstrip("/") + path,
                                   data=None if body is None else json.dumps(body).encode(),
                                   headers={"X-Token": self.web.token if token is None else token,
                                            "Content-Type": "application/json"})
        if host:
            r.add_header("Host", host)
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_page_has_token(self):
        code, body = self.req("/")
        self.assertEqual(code, 200)
        self.assertIn(self.web.token.encode(), body)
        self.assertNotIn(b"__TOKEN__", body)

    def test_guards(self):
        self.assertEqual(self.req("/api/state", token="x")[0], 403)
        self.assertEqual(self.req("/api/state", host="evil.example")[0], 403)
        self.assertEqual(self.req("/", host="evil.example")[0], 403)
        self.assertEqual(self.req("/api/state")[0], 200)

    def test_settings_rewire_watcher(self):
        code, _ = self.req("/api/settings", {"section": "hotkeys", "values": {"radar": "KEY_KPSLASH"}})
        self.assertEqual(code, 200)
        self.assertIn("KEY_KPSLASH", self.watcher.keys)
        self.assertIn(("changed",), self.macro.calls)
        code, body = self.req("/api/settings", {"section": "hotkeys", "values": {"arrow": "KEY_KPSLASH"}})
        self.assertEqual(code, 409)
        self.assertIn(b"both", body)

    def test_overlay_commands(self):
        self.assertEqual(self.req("/api/overlay", {"cmd": "calibrate"})[0], 200)
        self.assertEqual(self.overlay.posted[-1], "calibrate")
        self.assertEqual(self.req("/api/overlay", {"cmd": "quit; rm"})[0], 400)
        self.req("/api/settings", {"section": "overlay", "values": {"enabled": False}})
        self.assertEqual(self.overlay.posted[-1], "disable")

    def test_state_and_bad_body(self):
        code, body = self.req("/api/state")
        s = json.loads(body)
        self.assertEqual(s["macro"]["macro_key"], "KEY_F9")
        self.assertIn("platform", s)
        self.assertEqual(self.req("/api/settings", [1, 2])[0], 400)


if __name__ == "__main__":
    unittest.main()
