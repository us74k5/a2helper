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


def stroll(tr, clock, seconds, dt=0.05):
    """Walk with no position readings: only movement packets, like walking without skills."""
    for _ in range(int(round(seconds / dt))):
        clock.t += dt
        tr.note_movement()
        tr.advance()


class TestOwnMovement(unittest.TestCase):
    """Between position readings the tracker estimates your walking."""

    def setUp(self):
        self.c = Clock()
        self.tr = PositionTracker(clock=self.c)
        self.x, self.y = walk(self.tr, self.c, (10000, 10000), 0, 8)   # facing +x

    def test_estimate_moves_along_facing_while_walking(self):
        stroll(self.tr, self.c, 2.0)
        ex, ey, _ = self.tr.estimate
        self.assertAlmostEqual(ex - self.x, 2.0 * self.tr.speed, delta=60)
        self.assertAlmostEqual(ey, self.y, delta=1)
        self.assertEqual(self.tr.pos[:2], (self.x, self.y))             # real reading unchanged

    def test_estimate_stops_when_movement_stops(self):
        stroll(self.tr, self.c, 1.0)
        before = self.tr.estimate
        self.c.t += 3.0
        self.tr.advance()           # coasts at most MOVING_S past the last movement packet
        self.assertLessEqual(self.tr.estimate[0] - before[0], self.tr.speed * 0.35 + 1)
        held = self.tr.estimate
        self.c.t += 3.0
        self.assertFalse(self.tr.advance())
        self.assertEqual(self.tr.estimate, held)

    def test_standing_still_never_drifts(self):
        self.c.t += 5.0
        self.tr.advance()
        self.assertEqual(self.tr.estimate[:2], (self.x, self.y))

    def test_real_reading_snaps_estimate(self):
        stroll(self.tr, self.c, 2.0)
        self.tr.update((self.x + 1200, self.y + 300, 0.0))
        self.assertEqual(self.tr.estimate, (self.x + 1200, self.y + 300, 0.0))

    def test_long_walk_reading_is_not_thrown_away(self):
        # 6 s of walking with no skills: 60 m on. The old gate (26.5 m) dropped this as "someone else".
        stroll(self.tr, self.c, 6.0)
        self.tr.update((self.x + 6000, self.y, 0.0))
        self.assertEqual(self.tr.pos[:2], (self.x + 6000, self.y))

    def test_repeated_frozen_position_does_not_narrow_the_gate(self):
        # the capture sends the last (frozen) position with every snapshot while you walk
        for _ in range(int(6.0 * 15)):
            self.c.t += 1 / 15
            self.tr.note_movement()
            self.tr.update((self.x, self.y, 0.0))
        self.tr.update((self.x + 4500, self.y, 0.0))      # a skill, 45 m on
        self.assertEqual(self.tr.pos[:2], (self.x + 4500, self.y))

    def test_far_reading_while_standing_still_is_still_dropped(self):
        self.c.t += 1.0
        self.tr.update((self.x + 6000, self.y, 0.0))
        self.assertEqual(self.tr.pos[:2], (self.x, self.y))

    def test_learns_walking_speed(self):
        for _ in range(4):           # really going 9 m/s
            stroll(self.tr, self.c, 2.0)
            self.x += 1800
            self.tr.update((self.x, self.y, 0.0))
        self.assertGreater(self.tr.speed, 800)
        self.assertLessEqual(self.tr.speed, 1200)

    def test_estimate_has_a_limit(self):
        stroll(self.tr, self.c, 120.0)
        self.assertLessEqual(self.tr.estimate[0] - self.x, 30000 + 1)

    def test_fighting_in_place_keeps_other_entities_out(self):
        # In a fight your client sends packets all the time, which look like movement. Another
        # entity 25 m away must still be dropped, and you must not drift.
        others = 0
        for i in range(200):                    # 10 s of fighting, readings mixed in
            self.c.t += 0.05
            self.tr.note_movement()
            self.tr.advance()
            if i % 20 == 0:
                self.tr.update((self.x + 30 * (i % 3), self.y, 0.0))     # you, near enough the same spot
            elif i % 7 == 0:
                self.tr.update((self.x - 1800, self.y + 1600, 0.0))      # someone else
                others += 1
        self.assertGreater(others, 10)
        self.assertLess(math.hypot(self.tr.pos[0] - self.x, self.tr.pos[1] - self.y), 200)
        self.assertLess(math.hypot(self.tr.estimate[0] - self.x, self.tr.estimate[1] - self.y), 400)

    def test_casting_in_place_stops_the_estimate(self):
        # Packets kept coming but the readings show you stayed put: stop estimating until
        # a reading shows you moving again.
        for _ in range(3):
            stroll(self.tr, self.c, 1.0)
            self.tr.update((self.x + 20, self.y, 0.0))
            self.tr.update((self.x, self.y, 0.0))
        before = self.tr.estimate
        stroll(self.tr, self.c, 2.0)
        self.assertEqual(self.tr.estimate, before)
        self.x += 1300                              # then you walk 13 m and use a skill
        self.tr.update((self.x, self.y, 0.0))
        stroll(self.tr, self.c, 1.0)
        self.assertGreater(self.tr.estimate[0] - self.x, 300)


def replay(path, moving=False):
    c = Clock()
    tr = PositionTracker(clock=c)
    jumps = 0
    prev = None
    with open(path) as f:
        lines = f.readlines()
    for line in lines:
        s = json.loads(line)
        c.t = s["pkts"] / 20.0          # ~20 game packets a second in these captures
        if moving:                      # as if your client sent packets the whole time (a fight)
            tr.note_movement()
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

    def test_constant_client_packets_do_not_let_other_entities_in(self):
        self.assertEqual(replay(os.path.join(HERE, "data", "capture_0a65eeae.jsonl"), moving=True), 0)
        self.assertLessEqual(replay(os.path.join(HERE, "data", "capture_a31f1e62.jsonl"), moving=True), 10)


if __name__ == "__main__":
    unittest.main()
