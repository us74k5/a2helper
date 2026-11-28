"""Position tracker: synthetic cases plus replays of two real captures."""
import json
import math
import os
import sys
import unittest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
from aion2helper.tracker import PositionTracker  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def walk(tr, clock, start, heading_deg, metres, step_m=1.0, dt=0.1):
    x, y = start
    a = math.radians(heading_deg)
    for _ in range(int(metres / step_m)):
        x += math.cos(a) * step_m * 100
        y += math.sin(a) * step_m * 100
        clock.t += dt
        tr.update((x, y, 0.0))
    return x, y


def angle(h):
    return math.degrees(math.atan2(h[1], h[0]))


class TestTracker(unittest.TestCase):
    def setUp(self):
        self.c = Clock()
        self.tr = PositionTracker(clock=self.c)

    def test_zero_readings_ignored(self):
        self.tr.update((50000.0, 60000.0, 100.0))
        self.c.t += 0.2
        self.tr.update((0.0, -0.0, 0.0))
        self.assertEqual(self.tr.pos, (50000.0, 60000.0, 100.0))

    def test_facing_from_straight_walk(self):
        walk(self.tr, self.c, (10000, 10000), 30, 8)
        self.assertAlmostEqual(angle(self.tr.heading), 30, delta=2)

    def test_single_far_reading_does_not_flip_facing(self):
        x, y = walk(self.tr, self.c, (10000, 10000), 0, 8)
        before = self.tr.heading
        self.c.t += 0.1
        self.tr.update((x - 5000, y + 3000, 0.0))      # someone else, 58 m away
        self.c.t += 0.1
        self.tr.update((x + 100, y, 0.0))              # back to us
        self.assertEqual(self.tr.pos[:2], (x + 100, y))
        self.assertGreater(self.tr.heading[0] * before[0] + self.tr.heading[1] * before[1], 0.95)

    def test_teleport_is_followed_after_a_few_readings(self):
        walk(self.tr, self.c, (10000, 10000), 0, 5)
        for i in range(3):
            self.c.t += 0.2
            self.tr.update((900000.0 + i * 50, 400000.0, 0.0))
        self.assertGreater(self.tr.pos[0], 800000)

    def test_u_turn_followed_quickly(self):
        x, y = walk(self.tr, self.c, (10000, 10000), 0, 8)
        walk(self.tr, self.c, (x, y), 180, 8)
        self.assertAlmostEqual(abs(angle(self.tr.heading)), 180, delta=5)

    def test_small_wobble_is_smoothed(self):
        x, y = walk(self.tr, self.c, (10000, 10000), 0, 8)
        # two sideways steps of 1.5 m: the old tracker would point ~90 degrees off
        self.c.t += 0.1
        self.tr.update((x, y + 150, 0.0))
        self.c.t += 0.1
        self.tr.update((x, y + 300, 0.0))
        self.assertLess(abs(angle(self.tr.heading)), 45)


def replay(path):
    c = Clock()
    tr = PositionTracker(clock=c)
    jumps = 0
    prev = None
    with open(path) as f:
        lines = f.readlines()
    for line in lines:
        s = json.loads(line)
        c.t = s["pkts"] / 20.0          # ~20 game packets a second in these captures
        tr.update(s.get("player"))
        if tr.pos and prev and math.hypot(tr.pos[0] - prev[0], tr.pos[1] - prev[1]) > 2000:
            jumps += 1
        prev = tr.pos or prev
    return jumps


class TestRealCaptures(unittest.TestCase):
    def test_recent_capture_has_no_false_jumps(self):
        self.assertEqual(replay(os.path.join(HERE, "data", "capture_0a65eeae.jsonl")), 0)

    def test_messy_capture_is_calmer(self):
        # the old logic jumped 26 times on this one (two entities mixed together)
        self.assertLessEqual(replay(os.path.join(HERE, "data", "capture_a31f1e62.jsonl")), 10)


if __name__ == "__main__":
    unittest.main()
