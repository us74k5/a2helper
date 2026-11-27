"""The built-in Linux input code, without real devices: ioctl numbers, event parsing, the watcher loop."""
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from aion2helper.keys import VK

if not sys.platform.startswith("linux"):
    raise unittest.SkipTest("Linux input code")
from aion2helper import linux_evdev as E  # noqa: E402


def ev(etype, code, value):
    return E.EVENT.pack(0, 0, etype, code, value)


class PipeDevice:
    """Stands in for InputDevice: a pipe the test writes events into."""

    def __init__(self, path, name, keys):
        self.path, self.name, self.keys = path, name, set(keys)
        self.fd, self.wfd = os.pipe()
        os.set_blocking(self.fd, False)
        self._buf = b""

    read = E.InputDevice.read

    def send(self, *events):
        os.write(self.wfd, b"".join(ev(*e) for e in events))

    closed = False

    def close(self):
        self.closed = True
        for fd in (self.fd, self.wfd):
            try:
                os.close(fd)
            except OSError:
                pass


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux only")
class TestKernelNumbers(unittest.TestCase):
    def test_ioctls_match_kernel_headers(self):
        # values from linux/input.h and linux/uinput.h on x86_64
        self.assertEqual(E.UI_DEV_CREATE, 0x5501)
        self.assertEqual(E.UI_DEV_DESTROY, 0x5502)
        self.assertEqual(E.UI_DEV_SETUP, 0x405C5503)
        self.assertEqual(E.UI_SET_EVBIT, 0x40045564)
        self.assertEqual(E.UI_SET_KEYBIT, 0x40045565)
        self.assertEqual(E.UI_SET_RELBIT, 0x40045566)
        self.assertEqual(E.EVIOCGNAME(256), 0x81004506)
        self.assertEqual(E.EVIOCGBIT(E.EV_KEY, 96), 0x80604521)
        self.assertEqual(E.EVENT.size, 24)
        self.assertEqual(E.UINPUT_SETUP.size, 92)

    def test_every_key_has_a_code(self):
        self.assertEqual(sorted(set(VK) - set(E.CODES)), [])

    def test_codes_match_python_evdev(self):
        try:
            from evdev import ecodes
        except ImportError:
            self.skipTest("python-evdev not installed (only used to cross-check)")
        wrong = {k: (v, ecodes.ecodes[k]) for k, v in E.CODES.items() if ecodes.ecodes[k] != v}
        self.assertEqual(wrong, {})
        self.assertEqual((E.EV_KEY, E.EV_REL, E.KEY_MAX), (ecodes.EV_KEY, ecodes.EV_REL, ecodes.KEY_MAX))

    def test_read_events_across_partial_reads(self):
        d = PipeDevice("/dev/input/event9", "kbd", [])
        data = ev(E.EV_KEY, 67, 1) + ev(E.EV_SYN, 0, 0) + ev(E.EV_KEY, 67, 0)
        os.write(d.wfd, data[:30])
        self.assertEqual(list(d.read()), [(E.EV_KEY, 67, 1)])
        os.write(d.wfd, data[30:])
        self.assertEqual(list(d.read()), [(E.EV_SYN, 0, 0), (E.EV_KEY, 67, 0)])
        with self.assertRaises(BlockingIOError):
            list(d.read())
        d.close()

    def test_uinput_setup_sequence(self):
        calls = []
        with tempfile.NamedTemporaryFile() as f:
            u = E.UInputDevice([0x110, 0x111], "aion2-helper virtual mouse", mouse=True, path=f.name,
                               ioctl=lambda fd, req, arg=0: calls.append((req, arg)))
            u.write(E.EV_KEY, 0x110, 1)
            u.syn()
            u.close()
            written = open(f.name, "rb").read()
        reqs = [c[0] for c in calls]
        self.assertEqual(reqs[:3], [E.UI_SET_EVBIT, E.UI_SET_KEYBIT, E.UI_SET_KEYBIT])
        self.assertIn((E.UI_SET_EVBIT, E.EV_REL), calls)
        self.assertEqual(reqs[-3:], [E.UI_DEV_SETUP, E.UI_DEV_CREATE, E.UI_DEV_DESTROY])
        setup = E.UINPUT_SETUP.unpack(calls[-3][1])
        self.assertEqual(setup[4].rstrip(b"\0"), b"aion2-helper virtual mouse")
        self.assertEqual([E.EVENT.unpack_from(written, o)[2:] for o in (0, 24)], [(1, 0x110, 1), (0, 0, 0)])


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux only")
class TestUInputOutput(unittest.TestCase):
    def test_keys_go_to_the_right_device(self):
        from aion2helper.input_linux import UInputOutput
        made = []

        class Dev:
            def __init__(self, codes, name, mouse=False):
                self.codes, self.name, self.mouse, self.events, self.closed = codes, name, mouse, [], False
                made.append(self)

            def write(self, t, c, v):
                self.events.append((t, c, v))

            def syn(self):
                pass

            def close(self):
                self.closed = True

        out = UInputOutput(["KEY_F9", "BTN_LEFT"], device=Dev)
        kbd, mouse = made
        self.assertEqual((kbd.codes, kbd.mouse, mouse.codes, mouse.mouse), ([67], False, [0x110], True))
        out.key_down("KEY_F9")
        out.key_down("BTN_LEFT")
        out.close()
        self.assertEqual(kbd.events, [(1, 67, 1), (1, 67, 0)])
        self.assertEqual(mouse.events, [(1, 0x110, 1), (1, 0x110, 0)])
        self.assertTrue(kbd.closed and mouse.closed)

    def test_no_permission_is_explained(self):
        from aion2helper.input_linux import UInputOutput

        def denied(*a, **k):
            raise PermissionError(13, "denied")
        with self.assertRaises(RuntimeError) as cm:
            UInputOutput(["KEY_F9"], device=denied)
        self.assertIn("install.sh", str(cm.exception))


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux only")
class TestWatcher(unittest.TestCase):
    def test_watches_devices_with_the_keys(self):
        from aion2helper import input_linux as IL
        mouse = PipeDevice("/dev/input/event1", "Logitech Mouse", [0x110, 0x111])
        ours = PipeDevice("/dev/input/event2", "aion2-helper virtual mouse", [0x110, 0x111])
        kbd = PipeDevice("/dev/input/event3", "Keyboard", [74, 78, 67])
        other = PipeDevice("/dev/input/event4", "Power Button", [116])
        devs = {d.path: d for d in (mouse, ours, kbd, other)}
        seen = []
        got = threading.Event()

        def on_key(n, d):
            seen.append((n, d))
            if len(seen) >= 3:
                got.set()

        with mock.patch.object(IL, "list_devices", lambda: list(devs)), \
             mock.patch.object(IL, "InputDevice", lambda p: devs[p]):
            w = IL.EvdevWatcher(on_key)
            w.set_keys({"BTN_RIGHT", "KEY_KPMINUS"})
            w.start()
            time.sleep(0.3)
            mouse.send((E.EV_KEY, 0x111, 1), (E.EV_SYN, 0, 0), (E.EV_KEY, 0x110, 1))
            self.assertTrue(ours.closed and other.closed)   # our own device and keyless ones: not watched
            self.assertFalse(mouse.closed or kbd.closed)
            kbd.send((E.EV_KEY, 74, 1), (E.EV_KEY, 74, 2), (E.EV_KEY, 67, 1))  # repeat + unwatched key
            mouse.send((E.EV_KEY, 0x111, 0))
            got.wait(2)
            self.assertTrue(w.is_down("KEY_KPMINUS"))
            w.stop()
        self.assertEqual(sorted(seen), sorted([("BTN_RIGHT", True), ("KEY_KPMINUS", True), ("BTN_RIGHT", False)]))
        self.assertEqual(w.problem, "")
        for d in devs.values():
            d.close()

    def test_a_slow_device_scan_never_delays_the_trigger(self):
        # Opening every /dev/input device can take hundreds of milliseconds; a release that
        # arrives meanwhile must still get through at once (it used to wait for the scan).
        from aion2helper import input_linux as IL
        mouse = PipeDevice("/dev/input/event1", "Logitech Mouse", [0x110, 0x111])
        slow_paths = [f"/dev/input/event{i}" for i in range(2, 6)]
        opened = []

        def open_device(p):
            if p == mouse.path:
                return mouse
            opened.append(p)
            time.sleep(0.3)
            return PipeDevice(p, "Headphone jack", [])

        seen = []
        got = threading.Event()

        def on_key(n, d):
            seen.append((time.monotonic(), d))
            got.set()

        with mock.patch.object(IL, "list_devices", lambda: [mouse.path] + slow_paths), \
             mock.patch.object(IL, "InputDevice", open_device), \
             mock.patch.object(IL, "_signature", lambda p: ("same", p)):
            w = IL.EvdevWatcher(on_key)
            w.set_keys({"BTN_RIGHT"})
            w.start()
            deadline = time.monotonic() + 5
            while len(opened) < len(slow_paths) and time.monotonic() < deadline:
                time.sleep(0.02)
            time.sleep(0.4)                     # the first scan has looked at every device
            first = len(opened)
            opened.clear()
            w._rescan.set()                     # a rescan skips devices it already turned down
            time.sleep(0.1)
            self.assertEqual(opened, [])
            # now make a rescan slow again and release the trigger in the middle of it
            with mock.patch.object(IL, "_signature", lambda p: ("new", p)):
                w._rescan.set()
                time.sleep(0.05)
                t = time.monotonic()
                mouse.send((E.EV_KEY, 0x111, 0))
                got.wait(1)
            w.stop()
        self.assertEqual(first, len(slow_paths))
        self.assertTrue(seen, "the release never arrived")
        self.assertLess(seen[0][0] - t, 0.1)
        mouse.close()


if __name__ == "__main__":
    unittest.main()
