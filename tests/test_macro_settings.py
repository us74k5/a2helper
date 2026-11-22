import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aion2helper import settings as S
from aion2helper.macro import Macro


class FakeOutput:
    def __init__(self, keys):
        self.keys = keys
        self.events = []
        self.closed = False

    def key_down(self, k):
        self.events.append(("down", k))

    def key_up(self, k):
        self.events.append(("up", k))

    def tap(self, k, hold_s):
        self.events.append(("tap", k))

    def close(self):
        self.closed = True


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def make_settings(tmp, **macro):
    st = S.Settings(Path(tmp) / "config.json")
    if macro:
        st.update("macro", macro)
    return st


class TestSettings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.tmp.name, "APPDATA": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_defaults_are_valid_and_written(self):
        st = make_settings(self.tmp.name)
        self.assertEqual(st.data["macro"]["macro_key"], "KEY_F9")
        self.assertTrue((Path(self.tmp.name) / "config.json").exists())

    def test_conflicts_are_refused(self):
        st = make_settings(self.tmp.name)
        for section, values in [("macro", {"macro_key": "BTN_RIGHT"}),          # the trigger
                                ("macro", {"attack_key": "KEY_F9"}),            # same as macro key
                                ("hotkeys", {"map": "KEY_KPMINUS"}),            # the on/off key
                                ("hotkeys", {"radar": "KEY_KPPLUS"}),           # the map hotkey
                                ("macro", {"trigger": "KEY_NOPE"}),
                                ("macro", {"attack_mode": "spam"}),
                                ("overlay", {"radar": {}})]:
            with self.subTest(values=values), self.assertRaises(S.SettingsError):
                st.update(section, values)
        self.assertEqual(st.data["macro"]["macro_key"], "KEY_F9")  # nothing half-applied

    def test_attack_off_frees_its_key(self):
        st = make_settings(self.tmp.name, attack_mode="off")
        st.update("macro", {"trigger": "BTN_LEFT"})  # allowed: the attack is not pressed
        self.assertEqual(st.data["macro"]["trigger"], "BTN_LEFT")

    def test_clamped(self):
        st = make_settings(self.tmp.name, attack_interval=99, tap_hold_ms=1)
        self.assertEqual(st.data["macro"]["attack_interval"], 5.0)
        self.assertEqual(st.data["macro"]["tap_hold_ms"], 15)

    def test_update_keeps_overlay_dicts(self):
        st = make_settings(self.tmp.name)
        radar = st.data["overlay"]["radar"]
        st.update("overlay", {"char": "  Nyx  "})
        self.assertIs(st.data["overlay"]["radar"], radar)
        self.assertEqual(st.data["overlay"]["char"], "Nyx")

    def test_migrates_old_programs(self):
        base = Path(self.tmp.name)
        (base / "aion-cube-overlay").mkdir()
        (base / "aion-cube-overlay" / "config.json").write_text(json.dumps(
            {"char": "Nyx", "map": {"mode": "follow", "points": [[1, 2, 3, 4]]}, "cc": {"visible": True}}))
        (base / "combat-helper").mkdir()
        (base / "combat-helper" / "settings.json").write_text(json.dumps(
            {"general": {"trigger": "BTN_SIDE"}, "class": "ranger",
             "classes": {"ranger": {"macro_key": "KEY_F8", "hold_weave": False, "weave_interval": 0.8}}}))
        st = S.Settings(base / "new" / "config.json")
        m, o = st.data["macro"], st.data["overlay"]
        self.assertEqual((m["trigger"], m["macro_key"], m["attack_mode"], m["attack_interval"]),
                         ("BTN_SIDE", "KEY_F8", "tap", 0.8))
        self.assertEqual((o["char"], o["map"]["mode"], o["map"]["points"]), ("Nyx", "follow", [[1, 2, 3, 4]]))
        self.assertNotIn("cc", o)
        self.assertEqual(len(st.notes), 2)

    def test_broken_file_falls_back(self):
        p = Path(self.tmp.name) / "config.json"
        p.write_text(json.dumps({"macro": {"macro_key": "BTN_RIGHT"}}))
        st = S.Settings(p)
        self.assertEqual(st.data["macro"]["macro_key"], "KEY_F9")
        self.assertTrue(st.notes)


class TestMacro(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.tmp.name, "APPDATA": self.tmp.name})
        self.env.start()
        self.outs = []

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def factory(self, keys):
        o = FakeOutput(keys)
        self.outs.append(o)
        return o

    def macro(self, **m):
        st = make_settings(self.tmp.name, **m)
        clock = Clock()
        mac = Macro(st, output_factory=self.factory, clock=clock)
        mac.start()
        mac._stop.set()          # drive step() by hand instead of the thread
        mac._thread.join()
        return mac, clock

    def test_hold_mode(self):
        mac, clock = self.macro()
        out = self.outs[-1]
        self.assertEqual(out.keys, ["KEY_F9", "BTN_LEFT"])
        mac.step(clock())
        self.assertEqual(out.events, [])
        mac.on_key("BTN_RIGHT", True)
        mac.step(clock())
        self.assertEqual(out.events, [("down", "KEY_F9"), ("down", "BTN_LEFT")])  # macro key first
        clock.t += 1
        mac.step(clock())
        self.assertEqual(len(out.events), 2)  # held, not re-pressed
        mac.on_key("BTN_RIGHT", False)
        mac.step(clock())
        self.assertEqual(sorted(out.events[2:]), [("up", "BTN_LEFT"), ("up", "KEY_F9")])

    def test_toggle_pauses(self):
        mac, clock = self.macro()
        out = self.outs[-1]
        mac.on_key("BTN_RIGHT", True)
        mac.step(clock())
        mac.on_key("KEY_KPMINUS", True)
        mac.step(clock())
        self.assertFalse(mac.enabled)
        self.assertEqual(out.events[-2:], [("up", "BTN_LEFT"), ("up", "KEY_F9")])
        mac.on_key("KEY_KPMINUS", False)   # key release does not toggle back
        mac.on_key("KEY_KPMINUS", True)
        mac.step(clock())
        self.assertEqual(out.events[-1], ("down", "BTN_LEFT"))

    def test_tap_mode(self):
        mac, clock = self.macro(attack_mode="tap", attack_interval=0.5)
        out = self.outs[-1]
        mac.on_key("BTN_RIGHT", True)
        for _ in range(9):  # 1.0 s in 0.125 s ticks (exact in binary)
            mac.step(clock())
            clock.t += 0.125
        self.assertEqual(out.events[0], ("down", "KEY_F9"))
        self.assertEqual([e for e in out.events if e[0] == "tap"], [("tap", "BTN_LEFT")] * 3)
        self.assertNotIn(("down", "BTN_LEFT"), out.events)

    def test_off_mode_and_start_delay(self):
        mac, clock = self.macro(attack_mode="off", start_delay_ms=200)
        out = self.outs[-1]
        self.assertEqual(out.keys, ["KEY_F9"])
        mac.on_key("BTN_RIGHT", True)
        mac.step(clock())
        clock.t += 0.1
        mac.step(clock())
        self.assertEqual(out.events, [])
        clock.t += 0.15
        mac.step(clock())
        self.assertEqual(out.events, [("down", "KEY_F9")])

    def test_stop_releases_and_closes(self):
        mac, clock = self.macro()
        out = self.outs[-1]
        mac.on_key("BTN_RIGHT", True)
        mac.step(clock())
        mac.running = True  # stop() only acts on a running macro
        mac.stop()
        self.assertIn(("up", "KEY_F9"), out.events)
        self.assertIn(("up", "BTN_LEFT"), out.events)
        self.assertTrue(out.closed)

    def test_new_macro_key_reopens_devices(self):
        st = make_settings(self.tmp.name)
        mac = Macro(st, output_factory=self.factory)
        mac.start()
        st.update("macro", {"attack_mode": "tap"})
        mac.settings_changed()
        self.assertEqual(len(self.outs), 1)        # same keys: no restart
        st.update("macro", {"macro_key": "KEY_F8"})
        mac.settings_changed()
        self.assertEqual(len(self.outs), 2)
        self.assertEqual(self.outs[-1].keys, ["KEY_F8", "BTN_LEFT"])
        self.assertTrue(self.outs[0].closed)
        mac.stop()

    def test_thread_runs(self):
        import time
        st = make_settings(self.tmp.name)
        mac = Macro(st, output_factory=self.factory)
        mac.start()
        mac.on_key("BTN_RIGHT", True)
        time.sleep(0.1)
        mac.on_key("BTN_RIGHT", False)
        time.sleep(0.1)
        mac.stop()
        self.assertEqual(self.outs[-1].events[:2], [("down", "KEY_F9"), ("down", "BTN_LEFT")])
        self.assertTrue(self.outs[-1].closed)


if __name__ == "__main__":
    unittest.main()
