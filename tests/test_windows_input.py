"""The Windows key code paths, against a fake Win32 (so they run on Linux too)."""
import unittest

from aion2helper.input_windows import MOUSE_FLAGS, WinOutput, WinWatcher
from aion2helper.keys import VK


class FakeWin32:
    def __init__(self):
        self.sent = []
        self.down = set()
        self.tapped = set()

    def scan_code(self, vk):
        return vk + 1000  # recognisable

    def key(self, scan, extended, up):
        self.sent.append(("key", scan, extended, up))

    def mouse(self, flag, data):
        self.sent.append(("mouse", flag, data))

    def is_down(self, vk):
        return vk in self.down

    def key_state(self, vk):
        tapped = vk in self.tapped
        self.tapped.discard(vk)
        return vk in self.down, tapped


class TestWinOutput(unittest.TestCase):
    def test_keys_as_scan_codes(self):
        api = FakeWin32()
        out = WinOutput(["KEY_F9", "KEY_RIGHTCTRL", "BTN_LEFT", "BTN_SIDE"], api=api)
        out.key_down("KEY_F9")
        out.key_down("KEY_RIGHTCTRL")
        out.key_down("BTN_LEFT")
        out.tap("BTN_SIDE", 0)
        out.close()  # lets go of everything still down
        self.assertEqual(api.sent[0], ("key", 120 + 1000, False, False))
        self.assertEqual(api.sent[1], ("key", 163 + 1000, True, False))
        self.assertEqual(api.sent[2], ("mouse", MOUSE_FLAGS["BTN_LEFT"][0], 0))
        self.assertEqual(api.sent[3:5], [("mouse", 0x80, 1), ("mouse", 0x100, 1)])
        self.assertIn(("key", 1120, False, True), api.sent[5:])
        self.assertIn(("mouse", 0x4, 0), api.sent[5:])
        self.assertEqual(len(api.sent), 8)

    def test_unknown_key(self):
        with self.assertRaises(RuntimeError):
            WinOutput(["KEY_NOPE"], api=FakeWin32())

    def test_refused_input_is_said_once(self):
        class Refusing(FakeWin32):
            def key(self, *a, **k):
                raise OSError("SendInput was refused")
        out = WinOutput(["KEY_F9"], api=Refusing())
        with self.assertLogs("aion2helper", "ERROR") as cm:
            out.key_down("KEY_F9")
            out.key_up("KEY_F9")
        self.assertEqual(len(cm.output), 1)


class TestWinWatcher(unittest.TestCase):
    def test_edges(self):
        api = FakeWin32()
        seen = []
        w = WinWatcher(lambda n, d: seen.append((n, d)), api=api)
        w.set_keys({"BTN_RIGHT", "KEY_KPMINUS"})
        w.poll_once()
        api.down.add(VK["BTN_RIGHT"][0])
        w.poll_once()
        w.poll_once()
        api.down.add(VK["KEY_KPMINUS"][0])
        api.down.discard(VK["BTN_RIGHT"][0])
        w.poll_once()
        self.assertEqual(sorted(seen), sorted([("BTN_RIGHT", True), ("BTN_RIGHT", False), ("KEY_KPMINUS", True)]))
        self.assertTrue(w.is_down("KEY_KPMINUS"))

    def test_tap_between_polls_is_not_missed(self):
        api = FakeWin32()
        seen = []
        w = WinWatcher(lambda n, d: seen.append((n, d)), api=api)
        w.set_keys({"KEY_KPMINUS"})
        w.poll_once()
        api.tapped.add(VK["KEY_KPMINUS"][0])   # pressed and let go before the next poll
        w.poll_once()
        w.poll_once()
        self.assertEqual(seen, [("KEY_KPMINUS", True), ("KEY_KPMINUS", False)])


class TestKeyTable(unittest.TestCase):
    def test_matches_page_keys(self):
        # every key the page can produce has a Windows code
        for k in ["KEY_A", "KEY_Z", "KEY_0", "KEY_9", "KEY_F1", "KEY_F12", "KEY_KP0", "KEY_KPMINUS", "KEY_KPPLUS",
                  "KEY_KPASTERISK", "KEY_KPSLASH", "KEY_KPENTER", "KEY_KPDOT", "KEY_SPACE", "KEY_GRAVE",
                  "KEY_LEFTSHIFT", "KEY_ENTER", "KEY_BACKSPACE", "KEY_CAPSLOCK", "BTN_LEFT", "BTN_EXTRA"]:
            self.assertIn(k, VK)


if __name__ == "__main__":
    unittest.main()
