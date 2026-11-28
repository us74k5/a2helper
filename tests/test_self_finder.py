"""Working out which entity is you without a character name."""
import os
import struct
import sys
import unittest
from pathlib import Path

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
from aion2helper.capture import core  # noqa: E402
from aion2helper.capture.core import SelfFinder  # noqa: E402

REAL_PARSER = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "aion2-helper/upstream"
ME, OTHER, MOB = 5001, 6002, 7003


def varint(v):
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        out.append(b | (0x80 if v else 0))
        if not v:
            return bytes(out)


def body(op, eid, rest=b""):
    return struct.pack("<H", op) + varint(eid) + rest


def frame(b):
    return varint(len(b) + 4) + b


def action(eid, x=0.0, y=0.0, z=0.0, op=14100):
    return body(op, eid, b"\x00\x00" + struct.pack("<fff", x, y, z))


class Clock:
    t = 100.0

    def __call__(self):
        return self.t


class TestSelfFinder(unittest.TestCase):
    def setUp(self):
        self.c = Clock()
        self.f = SelfFinder(clock=self.c)

    def press(self, *actors, target=None):
        if target is not None:
            self.f.on_c2s(struct.pack("<H", core.OP_TARGET_REQ) + b"\x00" + varint(target))
        self.f.on_c2s(struct.pack("<HH", core.OP_SKILL_REQ, 0) + b"\x00" * 6)
        self.c.t += 0.1
        for eid in actors:
            self.f.on_s2c(action(eid))
        self.c.t += 1.0

    def test_own_effect_list_names_you(self):
        self.f.on_s2c(body(core.OP_OWN_LIST, ME, b"\x0f" * 8))
        self.assertEqual(self.f.entity, ME)

    def test_map_load_forgets(self):
        self.f.on_s2c(body(core.OP_OWN_LIST, ME))
        self.f.on_s2c(body(core.OP_MAP_LOAD, 0))
        self.assertIsNone(self.f.entity)

    def test_skill_presses_find_you_among_others(self):
        others = [OTHER, None, 8004, None, OTHER, 9005]
        for o in others:
            self.press(ME, *([o] if o else []))
        self.assertEqual(self.f.entity, ME)

    def test_needs_enough_presses(self):
        for _ in range(core.VOTE_MIN - 1):
            self.press(ME)
        self.assertIsNone(self.f.entity)

    def test_target_you_hit_is_left_out(self):
        for _ in range(6):
            self.press(MOB, ME, target=MOB)
        self.assertEqual(self.f.entity, ME)

    def test_tie_decides_nothing(self):
        # something reacting to every press as reliably as you (an untargeted mob): don't guess
        for _ in range(8):
            self.press(ME, MOB)
        self.assertIsNone(self.f.entity)

    def test_late_actions_dont_count(self):
        for _ in range(6):
            self.f.on_c2s(struct.pack("<HH", core.OP_SKILL_REQ, 0) + b"\x00" * 6)
            self.c.t += 2.0
            self.f.on_s2c(action(OTHER))
        self.assertIsNone(self.f.entity)


@unittest.skipUnless((REAL_PARSER / core.PARSER_FILE).is_file(), "upstream parser not downloaded")
class TestWithRealParser(unittest.TestCase):
    """The parser on its own latches onto whoever moves first; the session pins it to you."""

    def setUp(self):
        self.snaps = []
        self.s = core.Session(core.load_parser(str(REAL_PARSER)), self.snaps.append)

    def s2c(self, *bodies):
        self.s.engine.feed_payload("1.1.1.1", 13328, "2.2.2.2", 50000, b"".join(frame(b) for b in bodies))

    def test_own_list_overrides_a_wrong_guess(self):
        self.s2c(action(OTHER, 10000.0, 10000.0, 0.0))
        self.assertEqual(self.s.state.player_entity_id, OTHER)      # the parser's own guess
        self.assertEqual(self.s.state.player_pos[0], 10000.0)
        self.s2c(body(core.OP_OWN_LIST, ME, b"\x0f" * 8))
        self.assertEqual(self.s.state.player_entity_id, ME)
        self.assertIsNone(self.s.state.player_pos)                  # OTHER's position is dropped
        self.s2c(action(OTHER, 20000.0, 0.0, 0.0), action(ME, 500.0, 600.0, 0.0))
        self.assertEqual(self.s.state.player_pos[:2], (500.0, 600.0))
        self.s.maybe_send(force=True)
        self.assertEqual(self.snaps[-1]["pid"], ME)


if __name__ == "__main__":
    unittest.main()
