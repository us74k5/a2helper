"""Guide-arrow direction checks. Run: python3 -m unittest discover -s tests"""
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from aion2helper.overlay import Model, arrow_wanted, guide_direction, turn_hint  # noqa: E402
from aion2helper.settings import DEFAULTS  # noqa: E402
DEFAULT_CONFIG = DEFAULTS["overlay"]

M = DEFAULT_CONFIG["map"]["m"]   # world +X up, +Y right
P = (0.0, 0.0, 0.0)


def cube(x, y, z=0.0):
    return [1, 1, x, y, z, "c"]


def deg(yaw):
    return round(math.degrees(yaw))


class Direction(unittest.TestCase):
    def test_map_up_compass(self):
        self.assertEqual(deg(guide_direction(P, cube(5000, 0), M)[0]), 0)      # north
        self.assertEqual(deg(guide_direction(P, cube(0, 5000), M)[0]), 90)     # east = right
        self.assertEqual(deg(guide_direction(P, cube(0, -5000), M)[0]), -90)   # west = left
        self.assertEqual(abs(deg(guide_direction(P, cube(-5000, 0), M)[0])), 180)

    def test_relative_to_heading(self):
        east = (0.0, 1.0)   # moving east; a cube to the north is on your left
        self.assertEqual(deg(guide_direction(P, cube(5000, 0), M, east)[0]), -90)
        self.assertEqual(deg(guide_direction(P, cube(0, 5000), M, east)[0]), 0)
        self.assertEqual(turn_hint(guide_direction(P, cube(5000, 0), M, east)[0], True), "Turn left")
        self.assertEqual(turn_hint(guide_direction(P, cube(-200, -5000), M, east)[0], True), "Behind you")

    def test_mirrored_map_flips_left_right(self):
        mirrored = [-v for v in M[:2]] + M[2:]
        self.assertEqual(deg(guide_direction(P, cube(0, 5000), mirrored)[0]), -90)

    def test_pitch_and_distance(self):
        yaw, pitch, dist, dz = guide_direction(P, cube(3000, 0, 4000), M)
        self.assertAlmostEqual(dist, 50.0)
        self.assertAlmostEqual(dz, 40.0)
        self.assertAlmostEqual(pitch, math.atan2(4000, 3000))

    def test_heading_from_movement(self):
        m = Model()
        for x in (50000, 50050, 50100, 50200, 50300):   # moving north in 0.5-1 m steps
            m.on_snapshot({"player": [x, 40000, 0], "cubes": []})
        self.assertIsNotNone(m.heading)
        self.assertAlmostEqual(m.heading[0], 1.0)
        m.on_snapshot({"player": [900000, 40000, 0], "cubes": []})   # one far reading: ignored
        self.assertIsNotNone(m.heading)
        for x in (900050, 900100):                                    # it keeps coming: teleport
            m.on_snapshot({"player": [x, 40000, 0], "cubes": []})
        self.assertIsNone(m.heading)
        self.assertGreater(m.player[0], 800000)



class ArrowPopUp(unittest.TestCase):
    """The arrow only appears while a cube is within its range."""

    def model(self, player, cubes):
        m = Model()
        m.player = player
        m.cubes = cubes
        return m

    def test_hidden_without_position_or_cubes(self):
        cfg = {"visible": True, "range_m": 150.0}
        self.assertFalse(arrow_wanted(self.model(None, [cube(100, 0)]), cfg))
        self.assertFalse(arrow_wanted(self.model(P, []), cfg))

    def test_pops_up_only_in_range(self):
        cfg = {"visible": True, "range_m": 150.0}
        self.assertTrue(arrow_wanted(self.model(P, [cube(14_000, 0)]), cfg))       # 140 m
        self.assertTrue(arrow_wanted(self.model(P, [cube(15_000, 0)]), cfg))       # exactly 150 m
        self.assertFalse(arrow_wanted(self.model(P, [cube(16_000, 0)]), cfg))      # 160 m
        self.assertFalse(arrow_wanted(self.model(P, [cube(12_000, 0, 10_000)]), cfg))  # 156 m, mostly up

    def test_nearest_cube_decides(self):
        cfg = {"visible": True, "range_m": 150.0}
        self.assertTrue(arrow_wanted(self.model(P, [cube(90_000, 0), cube(0, 5_000)]), cfg))

    def test_switched_off_never_shows(self):
        self.assertFalse(arrow_wanted(self.model(P, [cube(100, 0)]), {"visible": False, "range_m": 150.0}))

    def test_default_range(self):
        self.assertEqual(DEFAULT_CONFIG["arrow"]["range_m"], 150.0)
        self.assertTrue(arrow_wanted(self.model(P, [cube(14_000, 0)]), {"visible": True}))


if __name__ == "__main__":
    unittest.main()
