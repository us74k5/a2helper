"""Calibration math checks. Run: python3 -m unittest discover -s tests"""
import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from aion2helper.overlay import MapTransform  # noqa: E402
from aion2helper.settings import DEFAULTS  # noqa: E402
DEFAULT_CONFIG = DEFAULTS["overlay"]


def fresh():
    return copy.deepcopy(DEFAULT_CONFIG["map"])


def true_map(wx, wy):
    # pretend the game map shows world +X up, +Y right, 0.0375 px per cm, offset
    return 0.0375 * wy + 410.0, -0.0375 * wx + 880.0


class Calibration(unittest.TestCase):
    def assertClose(self, a, b, tol=0.5):
        self.assertLess(abs(a[0] - b[0]) + abs(a[1] - b[1]), tol, f"{a} vs {b}")

    def test_two_points_recover_axis_aligned_map(self):
        cfg = fresh()
        cfg["m"] = [0.05, 0.0, 0.0, 0.05]  # deliberately wrong start (rotated + wrong scale)
        tf = MapTransform(cfg)
        for w in ((10000, 20000), (16000, 26000)):
            cfg["points"].append([*true_map(*w), *w])
            tf.solve()
        probe = (13500, 18000)
        self.assertClose(tf.apply(*probe), true_map(*probe))

    def test_two_points_recover_mirrored_map(self):
        cfg = fresh()
        tf = MapTransform(cfg)

        def mirrored(wx, wy):
            return -0.02 * wy + 1500.0, -0.02 * wx + 900.0

        for w in ((10000, 20000), (17000, 24000)):
            cfg["points"].append([*mirrored(*w), *w])
            tf.solve()
        probe = (5000, 30000)
        self.assertClose(tf.apply(*probe), mirrored(*probe))

    def test_three_points_fit_rotated_map(self):
        cfg = fresh()
        tf = MapTransform(cfg)

        def rotated(wx, wy):  # 30 degree tilt, non-uniform scale
            return 0.017 * wx + 0.03 * wy + 100.0, -0.03 * wx + 0.017 * wy + 700.0

        for w in ((0, 0), (8000, 1000), (2000, 9000)):
            cfg["points"].append([*rotated(*w), *w])
            tf.solve()
        probe = (4000, 4000)
        self.assertClose(tf.apply(*probe), rotated(*probe))

    def test_small_move_is_rejected(self):
        cfg = fresh()
        tf = MapTransform(cfg)
        cfg["points"].append([500, 500, 10000, 20000])
        tf.solve()
        cfg["points"].append([505, 500, 10500, 20000])  # only 5 m
        msg = tf.solve()
        self.assertIn("ignored", msg)
        self.assertEqual(len(cfg["points"]), 1)

    def test_static_arrow_switches_to_follow_mode(self):
        cfg = fresh()
        tf = MapTransform(cfg)
        cfg["points"].append([960, 540, 10000, 20000])
        tf.solve()
        cfg["points"].append([961, 541, 20000, 26000])  # moved 100 m+, arrow stayed centered
        tf.solve()
        self.assertEqual(cfg["mode"], "follow")
        player = (20000, 26000, 0)
        self.assertClose(tf.apply(20000, 26000, player), (961, 541))

    def test_scale_and_rotate_keep_player_pinned(self):
        cfg = fresh()
        tf = MapTransform(cfg)
        player = (12345.0, 6789.0, 0.0)
        before = tf.apply(player[0], player[1], player)
        tf.scale(1.3, player)
        tf.rotate90(player)
        tf.flip(player)
        self.assertClose(tf.apply(player[0], player[1], player), before)

    def test_mode_switch_preserves_current_view(self):
        cfg = fresh()
        tf = MapTransform(cfg)
        player = (1000.0, 2000.0, 0.0)
        cube = (4000.0, -500.0)
        before = tf.apply(*cube, player)
        tf.set_mode("follow", player)
        self.assertClose(tf.apply(*cube, player), before)
        tf.set_mode("fixed", player)
        self.assertClose(tf.apply(*cube, player), before)


if __name__ == "__main__":
    unittest.main()
