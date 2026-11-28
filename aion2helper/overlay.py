"""The hidden cube overlay: click-through layers drawn over the game.

  * a 3D guide arrow that points at the nearest cube
  * a corner radar (north up, distances in meters, height marks)
  * a full-screen map layer you line up once with the in-game map, so live cubes show
    as markers on top of it

It needs the game in borderless windowed mode (and on Linux, KDE desktop mode, not Game Mode).
"""
from __future__ import annotations

import logging
import math
import os
import shutil
import subprocess
import sys
import time

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QFont, QGuiApplication, QPainter,
                           QPainterPath, QPen, QPolygonF)
from PySide6.QtWidgets import QApplication, QWidget

from .tracker import PositionTracker

log = logging.getLogger("aion2helper")
UNITS_PER_M = 100.0  # game coordinates are centimeters

CUBE = QColor("#ff3b5c")
CUBE_FAR = QColor("#ff8fa3")
PLAYER = QColor("#39e6a0")
INK = QColor("#e8eef7")
MUTED = QColor("#8a98ad")
PANEL = QColor(10, 16, 28, 200)
CALIB = QColor("#ffd34d")


# --------------------------------------------------------------------------- map transform

# --------------------------------------------------------------------------- map transform

class MapTransform:
    """world (x, y) in cm -> screen (x, y) in px.

    fixed:  s = M w + t
    follow: s = M (w - player) + anchor
    """

    ORIENTATIONS = [
        (c, -s, s, c) if not flip else (c, s, s, -c)
        for flip in (False, True)
        for c, s in ((1, 0), (0, 1), (-1, 0), (0, -1))
    ]

    def __init__(self, cfg: dict):
        self.cfg = cfg

    # -- accessors
    @property
    def m(self):
        return self.cfg["m"]

    @property
    def mode(self):
        return self.cfg["mode"]

    def apply(self, wx, wy, player=None):
        a, b, c, d = self.m
        if self.mode == "follow":
            if player is None:
                return None
            ax, ay = self.cfg["anchor"]
            dx, dy = wx - player[0], wy - player[1]
            return ax + a * dx + b * dy, ay + c * dx + d * dy
        tx, ty = self.cfg["t"]
        return a * wx + b * wy + tx, c * wx + d * wy + ty

    # -- edits that keep the player's marker where it is on screen
    def _pin(self, player, fn):
        before = self.apply(player[0], player[1], player) if player else None
        fn()
        if before and self.mode == "fixed":
            after = self.apply(player[0], player[1], player)
            self.cfg["t"][0] += before[0] - after[0]
            self.cfg["t"][1] += before[1] - after[1]

    def scale(self, factor, player):
        def f():
            self.cfg["m"] = [v * factor for v in self.m]
        self._pin(player, f)

    def rotate90(self, player):
        def f():
            a, b, c, d = self.m  # screen rotation [[0,-1],[1,0]] applied on the left
            self.cfg["m"] = [-c, -d, a, b]
        self._pin(player, f)

    def flip(self, player):
        def f():
            a, b, c, d = self.m  # mirror screen x
            self.cfg["m"] = [-a, -b, c, d]
        self._pin(player, f)

    def pan(self, dx, dy):
        key = "anchor" if self.mode == "follow" else "t"
        self.cfg[key][0] += dx
        self.cfg[key][1] += dy

    def set_mode(self, mode, player):
        if mode == self.mode:
            return
        if mode == "follow" and player:
            self.cfg["anchor"] = list(self.apply(player[0], player[1], player))
        elif mode == "fixed" and player:
            ax, ay = self.cfg["anchor"]
            a, b, c, d = self.m
            self.cfg["t"] = [ax - (a * player[0] + b * player[1]),
                             ay - (c * player[0] + d * player[1])]
        self.cfg["mode"] = mode

    # -- calibration from clicks
    def solve(self) -> str:
        pts = self.cfg["points"]
        if not pts:
            return "Click your character's arrow on the in-game map."
        if self.mode == "follow":
            self.cfg["anchor"] = [pts[-1][0], pts[-1][1]]
            return "Follow mode: arrow position set. Scroll to match the map's scale."

        def one_point(p):
            a, b, c, d = self.m
            self.cfg["t"] = [p[0] - (a * p[2] + b * p[3]), p[1] - (c * p[2] + d * p[3])]

        if len(pts) == 1:
            one_point(pts[0])
            return "Point 1 set. Fly 50 m+ (diagonally if you can), reopen the map, click your arrow again."

        if len(pts) >= 3 and self._solve_affine(pts):
            return f"Calibrated from {len(pts)} points. Check the markers line up; Enter to save."

        p1, p2 = pts[0], pts[-1]
        dsx, dsy = p2[0] - p1[0], p2[1] - p1[1]
        dwx, dwy = p2[2] - p1[2], p2[3] - p1[3]
        ds, dw = math.hypot(dsx, dsy), math.hypot(dwx, dwy)
        if dw < 20 * UNITS_PER_M:
            pts.pop()
            one_point(pts[-1])
            return "You moved less than 20 m between clicks; that click was ignored. Fly farther, then click again."
        if ds < 10:
            self.set_mode("follow", None)
            self.cfg["anchor"] = [p2[0], p2[1]]
            return ("Your arrow didn't move on screen, so the map follows you. Switched to follow mode: "
                    "scroll to match scale, R/F to fix direction.")
        k = ds / dw
        best = None
        for o in self.ORIENTATIONS:
            ex = k * (o[0] * dwx + o[1] * dwy) - dsx
            ey = k * (o[2] * dwx + o[3] * dwy) - dsy
            err = math.hypot(ex, ey) / ds
            if best is None or err < best[0]:
                best = (err, o)
        err, o = best
        self.cfg["m"] = [k * v for v in o]
        one_point(p1)
        note = "" if err < 0.15 else " (map may be rotated; add a 3rd point to fix it)"
        return f"Calibrated from 2 points{note}. Check the markers line up; Enter to save."

    def _solve_affine(self, pts) -> bool:
        # least squares for s = A [wx, wy, 1]
        n = [[0.0] * 3 for _ in range(3)]
        rx, ry = [0.0] * 3, [0.0] * 3
        for sx, sy, wx, wy in pts:
            row = (wx, wy, 1.0)
            for i in range(3):
                for j in range(3):
                    n[i][j] += row[i] * row[j]
                rx[i] += row[i] * sx
                ry[i] += row[i] * sy
        solx, soly = solve3(n, rx), solve3(n, ry)
        if not solx or not soly:
            return False
        self.cfg["m"] = [solx[0], solx[1], soly[0], soly[1]]
        self.cfg["t"] = [solx[2], soly[2]]
        return True


def solve3(a, b):
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(3):
        piv = max(range(col, 3), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-9 * max(1.0, abs(m[col][col])) or abs(m[piv][col]) < 1e-12:
            return None
        m[col], m[piv] = m[piv], m[col]
        for r in range(3):
            if r != col:
                f = m[r][col] / m[col][col]
                for c in range(col, 4):
                    m[r][c] -= f * m[col][c]
    return [m[i][3] / m[i][i] for i in range(3)]

# --------------------------------------------------------------------------- shared state

class Model(QObject):
    changed = Signal()

    def __init__(self):
        super().__init__()
        self.player = None
        self.cubes = []          # [eid, npc, x, y, z, name]
        self.packets = 0
        self.status = "Starting…"
        self.status_error = False
        self.seen_ids = set()
        self.last_snapshot = 0.0
        # The game doesn't send camera direction, so facing comes from movement.
        # PositionTracker drops (0,0,0) and other entities' readings and smooths facing.
        self.tracker = PositionTracker()
        self.raw_player = None

    @property
    def heading(self):
        return self.tracker.heading

    @property
    def heading_time(self):
        return self.tracker.heading_time

    def on_snapshot(self, msg):
        self.raw_player = tuple(msg["player"]) if msg.get("player") else None
        self.tracker.update(self.raw_player)
        self.player = self.tracker.pos
        self.cubes = msg.get("cubes") or []
        self.packets = msg.get("pkts", self.packets)
        self.last_snapshot = time.monotonic()
        self.changed.emit()

    def on_status(self, text, is_error):
        self.status = text
        self.status_error = is_error
        self.changed.emit()

    def new_cube_ids(self):
        ids = {c[0] for c in self.cubes}
        fresh = ids - self.seen_ids
        self.seen_ids |= ids
        return fresh

    def nearest(self):
        if not self.player or not self.cubes:
            return None
        px, py, pz = self.player
        return min(self.cubes, key=lambda c: (c[2] - px) ** 2 + (c[3] - py) ** 2 + (c[4] - pz) ** 2)

    def nearest_dist_m(self):
        """Straight-line distance to the nearest cube in meters, or None."""
        near = self.nearest()
        if near is None:
            return None
        return math.dist(self.player, near[2:5]) / UNITS_PER_M


def arrow_wanted(model, arrow_cfg) -> bool:
    """The arrow pops up only while a cube is within its range (when switched on)."""
    if not arrow_cfg.get("visible", True):
        return False
    dist = model.nearest_dist_m()
    return dist is not None and dist <= float(arrow_cfg.get("range_m", 150.0))


def describe(cube, player):
    dx, dy, dz = ((cube[2] - player[0]) / UNITS_PER_M, (cube[3] - player[1]) / UNITS_PER_M,
                  (cube[4] - player[2]) / UNITS_PER_M)
    dist = math.sqrt(dx * dx + dy * dy + dz * dz)
    text = f"{dist:.0f} m"
    if abs(dz) >= 3:
        text += f" {'▲' if dz > 0 else '▼'}{abs(dz):.0f}"
    return dist, text


def play_alert():
    if sys.platform == "win32":
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
            return
        except Exception:
            pass
    for cand in ("/usr/share/sounds/freedesktop/stereo/message-new-instant.oga",
                 "/usr/share/sounds/freedesktop/stereo/bell.oga"):
        if os.path.exists(cand):
            for player in ("pw-play", "paplay"):
                if shutil.which(player):
                    subprocess.Popen([player, cand], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return
    QApplication.beep()


# --------------------------------------------------------------------------- windows

OVERLAY_FLAGS = (Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                 | Qt.WindowTransparentForInput | Qt.X11BypassWindowManagerHint)
INTERACTIVE_FLAGS = Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool


class OverlayWindow(QWidget):
    def __init__(self, model: Model):
        super().__init__(None, OVERLAY_FLAGS)
        self.model = model
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setAttribute(Qt.WA_OpaquePaintEvent, False)
        self.setAutoFillBackground(False)
        model.changed.connect(self.update)


def clear_transparent(p: QPainter, rect: QRectF):
    """Clear a translucent window's backing store before drawing on it.

    This matters for the full-screen map window: without an explicit
    clear, some X11 compositors can leave the widget's default opaque surface
    visible as a white screen.
    """
    p.save()
    p.setCompositionMode(QPainter.CompositionMode_Clear)
    p.fillRect(rect, QColor(0, 0, 0, 0))
    p.restore()


class RadarWindow(OverlayWindow):
    def __init__(self, model, cfg):
        super().__init__(model)
        self.cfg = cfg
        size = int(cfg["size"])
        self.resize(size, size + 34)
        scr = QGuiApplication.primaryScreen().geometry()
        x = cfg["x"] if cfg.get("x") is not None else scr.right() - size - 24
        self.move(int(x), int(cfg.get("y") or 80))

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        clear_transparent(p, self.rect())
        w = self.width()
        size = int(self.cfg["size"])
        cx, cy, r = w / 2, size / 2, size / 2 - 6
        rng = float(self.cfg["range_m"])
        m = self.model

        p.setPen(Qt.NoPen)
        p.setBrush(PANEL)
        p.drawEllipse(QPointF(cx, cy), r, r)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(255, 255, 255, 40), 1))
        p.drawEllipse(QPointF(cx, cy), r / 2, r / 2)
        p.drawLine(QPointF(cx, cy - r), QPointF(cx, cy + r))
        p.drawLine(QPointF(cx - r, cy), QPointF(cx + r, cy))
        p.setFont(QFont("Sans", 8, QFont.Bold))
        p.setPen(MUTED)
        p.drawText(QRectF(cx - 10, cy - r + 2, 20, 14), Qt.AlignCenter, "N")
        p.setFont(QFont("Sans", 7))
        p.drawText(QRectF(cx + r / 2 - 30, cy + 2, 60, 12), Qt.AlignCenter, f"{rng / 2:.0f} m")

        status = ""
        if m.player:
            p.setPen(QPen(PLAYER, 2))
            p.setBrush(QColor(57, 230, 160, 90))
            p.drawEllipse(QPointF(cx, cy), 5, 5)
            near = m.nearest()
            scale = (r - 12) / rng
            for cube in m.cubes:
                dx = (cube[2] - m.player[0]) / UNITS_PER_M
                dy = (cube[3] - m.player[1]) / UNITS_PER_M
                ox, oy = dy * scale, -dx * scale  # world +X is up, +Y is right
                mag = math.hypot(ox, oy)
                edge = mag > r - 12
                if edge:
                    ox, oy = ox * (r - 12) / mag, oy * (r - 12) / mag
                pt = QPointF(cx + ox, cy + oy)
                is_near = cube is near
                if is_near:
                    p.setPen(QPen(CUBE, 1.2, Qt.DashLine))
                    p.drawLine(QPointF(cx, cy), pt)
                draw_cube_marker(p, pt, 6 if is_near else 4.5, CUBE if not edge else CUBE_FAR)
            if near:
                dist, text = describe(near, m.player)
                more = len(m.cubes) - 1
                status = f"Cube {text}" + (f"   +{more} more" if more > 0 else "")
            else:
                status = "No cubes in view yet"
        elif m.packets:
            status = "Waiting for your position (use a skill or dash)"
        else:
            status = m.status

        p.setPen(Qt.NoPen)
        p.setBrush(PANEL)
        p.drawRoundedRect(QRectF(4, size + 2, w - 8, 28), 8, 8)
        p.setPen(QColor("#ff8f8f") if (m.status_error and not m.player) else INK)
        p.setFont(QFont("Sans", 9, QFont.Bold))
        p.drawText(QRectF(10, size + 2, w - 20, 28), Qt.AlignCenter | Qt.TextSingleLine,
                   p.fontMetrics().elidedText(status, Qt.ElideRight, w - 20))
        p.end()


def draw_cube_marker(p: QPainter, pt: QPointF, s: float, color: QColor):
    poly = QPolygonF([QPointF(pt.x(), pt.y() - s), QPointF(pt.x() + s, pt.y()),
                      QPointF(pt.x(), pt.y() + s), QPointF(pt.x() - s, pt.y())])
    p.setPen(QPen(QColor(0, 0, 0, 200), 1.5))
    p.setBrush(color)
    p.drawPolygon(poly)


# --------------------------------------------------------------------------- 3D guide arrow

def wrap_angle(a: float) -> float:
    return (a + math.pi) % (2 * math.pi) - math.pi


def guide_direction(player, cube, map_m, heading=None):
    """Where the arrow should point.

    Returns (yaw, pitch, dist_m, dz_m). yaw is radians, 0 = forward, positive =
    to the right. Forward is your movement heading if given, else map-up.
    Left/right goes through the map calibration matrix, so a mirrored or
    rotated in-game map gives the same sense of "left" as what you see on it.
    """
    a, b, c, d = map_m
    if abs(a * d - b * c) < 1e-12:
        a, b, c, d = 0.0, 1.0, -1.0, 0.0   # radar convention: +X up, +Y right

    def screen_angle(wx, wy):      # clockwise from screen-up (screen y grows down)
        sx, sy = a * wx + b * wy, c * wx + d * wy
        return math.atan2(sx, -sy)

    dx, dy, dz = (cube[2] - player[0], cube[3] - player[1], cube[4] - player[2])
    horiz = math.hypot(dx, dy)
    yaw = screen_angle(dx, dy) if horiz > 1e-6 else 0.0
    if heading:
        yaw -= screen_angle(*heading)
    pitch = math.atan2(dz, horiz) if (horiz or dz) else 0.0
    return (wrap_angle(yaw), pitch,
            math.sqrt(horiz * horiz + dz * dz) / UNITS_PER_M, dz / UNITS_PER_M)


def turn_hint(yaw: float, relative: bool) -> str:
    deg = math.degrees(yaw)
    side = "right" if deg > 0 else "left"
    a = abs(deg)
    if relative:
        if a < 15:
            return "Straight ahead"
        if a < 60:
            return f"Bear {side}"
        if a < 140:
            return f"Turn {side}"
        return "Behind you"
    names = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return f"Toward map {names[int(((deg % 360) + 22.5) // 45) % 8]}"


# Arrow outline in its own plane (x right, z forward), unit-ish size.
ARROW_OUTLINE = [(0.0, 1.05), (0.66, 0.18), (0.24, 0.18), (0.24, -0.95),
                 (0.0, -0.72), (-0.24, -0.95), (-0.24, 0.18), (-0.66, 0.18)]
ARROW_HALF_THICK = 0.13
GROUND_Y = -0.62
CAM_TILT = math.radians(38)   # camera looks down at the arrow from behind
CAM_DIST = 4.6
LIGHT = (-0.35, 0.85, -0.4)


def _norm(v):
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return tuple(x / n for x in v)


def _cross(u, v):
    return (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])


def _dot(u, v):
    return u[0] * v[0] + u[1] * v[1] + u[2] * v[2]


class ArrowProjector:
    """Tiny software 3D: rotate the arrow, then perspective-project it."""

    def __init__(self, cx, cy, focal):
        self.cx, self.cy, self.f = cx, cy, focal
        st, ct = math.sin(CAM_TILT), math.cos(CAM_TILT)
        self.cam = (0.0, CAM_DIST * st, -CAM_DIST * ct)
        self.fwd = (0.0, -st, ct)
        self.up = (0.0, ct, st)

    @staticmethod
    def rotate(pt, yaw, pitch, lift=0.0):
        x, y, z = pt
        cp, sp = math.cos(pitch), math.sin(pitch)
        y, z = y * cp + z * sp, -y * sp + z * cp
        cy, sy = math.cos(yaw), math.sin(yaw)
        x, z = x * cy + z * sy, -x * sy + z * cy
        return (x, y + lift, z)

    def project(self, pt):
        r = (pt[0] - self.cam[0], pt[1] - self.cam[1], pt[2] - self.cam[2])
        zc = max(_dot(r, self.fwd), 0.1)
        return QPointF(self.cx + self.f * r[0] / zc, self.cy - self.f * _dot(r, self.up) / zc), zc

    def faces(self, yaw, pitch, lift):
        """Visible faces far-to-near as (polygon, shade, depth)."""
        h = ARROW_HALF_THICK
        top = [self.rotate((x, h, z), yaw, pitch, lift) for x, z in ARROW_OUTLINE]
        bot = [self.rotate((x, -h, z), yaw, pitch, lift) for x, z in ARROW_OUTLINE]
        n = len(ARROW_OUTLINE)
        quads = [top, bot[::-1]]
        for i in range(n):
            j = (i + 1) % n
            quads.append([top[j], top[i], bot[i], bot[j]])
        light = _norm(LIGHT)
        out = []
        for q in quads:
            # Newell normal (robust for the concave top/bottom faces)
            nx = ny = nz = 0.0
            for k in range(len(q)):
                p0, p1 = q[k], q[(k + 1) % len(q)]
                nx += (p0[1] - p1[1]) * (p0[2] + p1[2])
                ny += (p0[2] - p1[2]) * (p0[0] + p1[0])
                nz += (p0[0] - p1[0]) * (p0[1] + p1[1])
            normal = _norm((nx, ny, nz))
            centre = tuple(sum(p[i] for p in q) / len(q) for i in range(3))
            to_cam = (self.cam[0] - centre[0], self.cam[1] - centre[1], self.cam[2] - centre[2])
            if _dot(normal, to_cam) <= 0:
                continue
            pts, depth = [], 0.0
            for p in q:
                sp, zc = self.project(p)
                pts.append(sp)
                depth += zc
            shade = 0.38 + 0.62 * max(0.0, _dot(normal, light))
            out.append((QPolygonF(pts), shade, depth / len(q)))
        out.sort(key=lambda f: -f[2])
        return out

    def ground(self, pts):
        return QPolygonF([self.project((x, GROUND_Y, z))[0] for x, z in pts])


def shade_color(base: QColor, k: float, alpha: int = 255) -> QColor:
    return QColor(min(255, int(base.red() * k)), min(255, int(base.green() * k)),
                  min(255, int(base.blue() * k)), alpha)


ARROW_NEAR = QColor("#39e6a0")
ARROW_FAR = QColor("#ff3b5c")
ARROW_IDLE = QColor("#8a98ad")


class GuideWindow(OverlayWindow):
    """A 3D arrow that points at the nearest cube, with distance and a plain hint."""

    TEXT_H = 58

    def __init__(self, model, cfg, map_cfg):
        super().__init__(model)
        self.cfg = cfg
        self.map_cfg = map_cfg
        size = int(cfg["size"])
        self.resize(size, size + self.TEXT_H)
        scr = QGuiApplication.primaryScreen().geometry()
        x = cfg["x"] if cfg.get("x") is not None else scr.center().x() - size // 2
        self.move(int(x), int(cfg.get("y") or 90))
        self.yaw = 0.0
        self.pitch = 0.0
        self.t0 = time.monotonic()
        self.anim = QTimer(self)
        self.anim.timeout.connect(self._step)
        self.anim.start(33)

    def _target(self):
        m = self.model
        near = m.nearest()
        if not (m.player and near):
            return None
        relative = self.cfg.get("mode", "heading") == "heading" and m.heading is not None
        yaw, pitch, dist, dz = guide_direction(m.player, near, self.map_cfg["m"],
                                               m.heading if relative else None)
        return yaw, max(-1.05, min(1.05, pitch)), dist, dz, relative

    def _step(self):
        if not self.isVisible():
            return
        tgt = self._target()
        if tgt:
            self.yaw = wrap_angle(self.yaw + wrap_angle(tgt[0] - self.yaw) * 0.2)
            self.pitch += (tgt[1] - self.pitch) * 0.2
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        clear_transparent(p, self.rect())
        w, size = self.width(), int(self.cfg["size"])
        m = self.model
        tgt = self._target()
        t = time.monotonic() - self.t0
        proj = ArrowProjector(w / 2, size * 0.58, size * 1.02)

        # soft backdrop so the arrow reads over any scenery
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(10, 16, 28, 120))
        p.drawEllipse(QPointF(w / 2, size * 0.55), size * 0.47, size * 0.38)

        # ground ring with a "forward" notch
        ring = [(1.25 * math.sin(a), 1.25 * math.cos(a))
                for a in (i * 2 * math.pi / 48 for i in range(48))]
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(255, 255, 255, 60), 1.5))
        p.drawPolygon(proj.ground(ring))
        p.setBrush(QColor(255, 255, 255, 110))
        p.setPen(Qt.NoPen)
        p.drawPolygon(proj.ground([(0, 1.5), (0.13, 1.3), (-0.13, 1.3)]))
        relative = bool(tgt and tgt[4])
        p.setFont(QFont("Sans", 7, QFont.Bold))
        p.setPen(MUTED)
        p.drawText(QRectF(0, size * 0.55 - size * 0.38 - 16, w, 13), Qt.AlignCenter,
                   "▲ YOUR FACING" if relative else "▲ MAP UP")

        if tgt:
            yaw, pitch, dist, dz, _ = tgt
            close = dist < 12
            base = ARROW_NEAR if close else (ARROW_FAR if dist > 40 else
                                             _mix(ARROW_NEAR, ARROW_FAR, (dist - 12) / 28))
            if relative and time.monotonic() - m.heading_time > 30:
                base = _mix(base, ARROW_IDLE, 0.45)   # facing may be stale
        else:
            yaw, pitch, base, close, dist, dz = self.yaw, 0.0, ARROW_IDLE, False, None, 0.0
        lift = 0.08 * math.sin(t * 3.0)

        # shadow on the ground (flattened copy of the arrow)
        shadow = []
        for x, z in ARROW_OUTLINE:
            rx, _, rz = ArrowProjector.rotate((x, 0, z), self.yaw, self.pitch)
            shadow.append((rx, rz))
        p.setBrush(QColor(0, 0, 0, 80))
        p.drawPolygon(proj.ground(shadow))

        if close:   # pulsing "you're here" ring
            k = (t * 1.2) % 1.0
            r = 0.4 + 1.1 * k
            pulse = [(r * math.sin(a), r * math.cos(a))
                     for a in (i * 2 * math.pi / 40 for i in range(40))]
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(57, 230, 160, int(200 * (1 - k))), 2.5))
            p.drawPolygon(proj.ground(pulse))

        for poly, shade, _ in proj.faces(self.yaw, self.pitch, lift):
            p.setPen(QPen(QColor(0, 0, 0, 170), 1.2))
            p.setBrush(shade_color(base, shade))
            p.drawPolygon(poly)

        # text block
        box = QRectF(6, size - 4, w - 12, self.TEXT_H - 2)
        p.setPen(Qt.NoPen)
        p.setBrush(PANEL)
        p.drawRoundedRect(box, 10, 10)
        if dist is not None:
            big = f"{dist:.0f} m"
            if abs(dz) >= 3:
                big += f"   {'▲' if dz > 0 else '▼'} {abs(dz):.0f} m {'up' if dz > 0 else 'down'}"
            hint = "You're there — look around" if close else turn_hint(yaw, relative)
            if not relative and self.cfg.get("mode", "heading") == "heading":
                hint += " · move to orient"
            more = len(m.cubes) - 1
            if more > 0:
                hint += f"   (+{more} more)"
        elif m.player:
            big, hint = "No cubes nearby", "Cubes appear here when the game reveals them"
        else:
            big = "Waiting for position"
            hint = "Use a skill or dash" if m.packets else m.status
        p.setFont(QFont("Sans", 13, QFont.Bold))
        p.setPen(INK)
        p.drawText(QRectF(box.left() + 8, box.top() + 4, box.width() - 16, 26), Qt.AlignCenter,
                   p.fontMetrics().elidedText(big, Qt.ElideRight, int(box.width() - 16)))
        p.setFont(QFont("Sans", 9, QFont.Bold))
        p.setPen(QColor("#ff8f8f") if (m.status_error and not m.player) else MUTED)
        p.drawText(QRectF(box.left() + 8, box.top() + 30, box.width() - 16, 20), Qt.AlignCenter,
                   p.fontMetrics().elidedText(hint, Qt.ElideRight, int(box.width() - 16)))
        p.end()


def _mix(c1: QColor, c2: QColor, k: float) -> QColor:
    k = max(0.0, min(1.0, k))
    return QColor(int(c1.red() + (c2.red() - c1.red()) * k),
                  int(c1.green() + (c2.green() - c1.green()) * k),
                  int(c1.blue() + (c2.blue() - c1.blue()) * k))


class MapLayer(OverlayWindow):
    def __init__(self, model, cfg, on_save):
        super().__init__(model)
        self.cfg = cfg
        self.tf = MapTransform(cfg)
        self.on_save = on_save
        self.calibrating = False
        self.calib_msg = ""
        self.drag_from = None
        self.setGeometry(QGuiApplication.primaryScreen().geometry())

    # -- mode switching
    def set_interactive(self, on: bool):
        visible = self.isVisible()
        self.setWindowFlags(INTERACTIVE_FLAGS if on else OVERLAY_FLAGS)
        self.setAttribute(Qt.WA_ShowWithoutActivating, not on)
        if visible or on:
            self.show()
        if on:
            self.raise_()
            self.activateWindow()
            self.setFocus()

    def start_calibration(self):
        self.calibrating = True
        self.calib_msg = self.tf.solve() if self.cfg["points"] else (
            "Open your in-game map, then click your character's arrow.")
        self.set_interactive(True)
        self.update()

    def finish_calibration(self):
        self.calibrating = False
        self.on_save()
        self.set_interactive(False)
        self.update()

    # -- input (calibration only)
    def mousePressEvent(self, e):
        if not self.calibrating:
            return
        if e.button() == Qt.LeftButton:
            if not self.model.player:
                self.calib_msg = "No position from the game yet. Use a skill or dash, then click again."
            else:
                pos = e.position()
                self.cfg["points"].append([pos.x(), pos.y(), self.model.player[0], self.model.player[1]])
                self.calib_msg = self.tf.solve()
        elif e.button() == Qt.RightButton:
            self.drag_from = e.position()
        self.update()

    def mouseMoveEvent(self, e):
        if self.calibrating and self.drag_from is not None:
            pos = e.position()
            self.tf.pan(pos.x() - self.drag_from.x(), pos.y() - self.drag_from.y())
            self.drag_from = pos
            self.update()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.RightButton:
            self.drag_from = None

    def wheelEvent(self, e):
        if self.calibrating:
            steps = e.angleDelta().y() / 120.0
            fine = e.modifiers() & Qt.ShiftModifier
            self.tf.scale((1.01 if fine else 1.06) ** steps, self.model.player)
            self.update()

    def keyPressEvent(self, e):
        if not self.calibrating:
            return
        k = e.key()
        pl = self.model.player
        if k in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Escape):
            self.finish_calibration()
            return
        if k == Qt.Key_R:
            self.tf.rotate90(pl)
        elif k == Qt.Key_F:
            self.tf.flip(pl)
        elif k == Qt.Key_M:
            self.tf.set_mode("fixed" if self.tf.mode == "follow" else "follow", pl)
            self.calib_msg = f"Mode: {self.tf.mode}."
        elif k == Qt.Key_Backspace:
            self.cfg["points"].clear()
            self.calib_msg = "Points cleared. Click your character's arrow."
        elif k in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down):
            step = 10 if e.modifiers() & Qt.ShiftModifier else 1
            dx = {Qt.Key_Left: -step, Qt.Key_Right: step}.get(k, 0)
            dy = {Qt.Key_Up: -step, Qt.Key_Down: step}.get(k, 0)
            self.tf.pan(dx, dy)
        self.update()

    # -- drawing
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        clear_transparent(p, self.rect())
        m = self.model
        if self.calibrating:
            p.fillRect(self.rect(), QColor(0, 0, 0, 1))  # tiny alpha so clicks land on us
        player = m.player
        if player:
            ps = self.tf.apply(player[0], player[1], player)
            if ps:
                p.setPen(QPen(PLAYER, 2.5))
                p.setBrush(Qt.NoBrush)
                p.drawEllipse(QPointF(*ps), 9, 9)
            p.setFont(QFont("Sans", 10, QFont.Bold))
            for cube in m.cubes:
                s = self.tf.apply(cube[2], cube[3], player)
                if not s:
                    continue
                pt = QPointF(*s)
                draw_cube_marker(p, pt, 8, CUBE)
                _, text = describe(cube, player)
                label = QRectF(pt.x() + 12, pt.y() - 10, 120, 20)
                p.setPen(QColor(0, 0, 0, 220))
                p.drawText(label.translated(1, 1), Qt.AlignLeft | Qt.AlignVCenter, text)
                p.setPen(INK)
                p.drawText(label, Qt.AlignLeft | Qt.AlignVCenter, text)
        if self.calibrating:
            for sx, sy, *_ in self.cfg["points"]:
                p.setPen(QPen(CALIB, 2))
                p.drawLine(QPointF(sx - 7, sy), QPointF(sx + 7, sy))
                p.drawLine(QPointF(sx, sy - 7), QPointF(sx, sy + 7))
            self._draw_help(p)
        p.end()

    def _draw_help(self, p: QPainter):
        lines = [
            "CALIBRATING MAP OVERLAY",
            self.calib_msg,
            "Left click: your arrow is here   ·   Right drag / arrows: move   ·   Wheel: scale (Shift = fine)",
            f"R: rotate 90°   ·   F: mirror   ·   M: fixed/follow (now {self.tf.mode})   ·   "
            "Backspace: clear points   ·   Enter: save",
        ]
        w = min(self.width() - 40, 980)
        box = QRectF((self.width() - w) / 2, 24, w, 100)
        path = QPainterPath()
        path.addRoundedRect(box, 10, 10)
        p.fillPath(path, QColor(10, 16, 28, 225))
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(CALIB, 1.5))
        p.drawPath(path)
        y = box.top() + 10
        for i, line in enumerate(lines):
            p.setFont(QFont("Sans", 11 if i < 2 else 9, QFont.Bold if i == 0 else QFont.Normal))
            p.setPen(CALIB if i == 0 else (INK if i == 1 else MUTED))
            p.drawText(QRectF(box.left() + 16, y, w - 32, 22), Qt.AlignLeft | Qt.AlignVCenter,
                       p.fontMetrics().elidedText(line, Qt.ElideRight, int(w - 32)))
            y += 22


# --------------------------------------------------------------------------- controller

class Bridge(QObject):
    """Signals other threads use to reach the Qt thread (the page, the hotkeys, the capture)."""
    command = Signal(str)
    snapshot = Signal(dict)
    status = Signal(str, bool)


class OverlayController:
    """Owns the overlay windows and the capture. Lives on the Qt thread."""

    def __init__(self, settings, parser_dir):
        from .capture.feed import CaptureFeed

        self.settings = settings
        self.cfg = settings.data["overlay"]     # live: the windows edit it in place
        self.model = Model()
        self.bridge = Bridge()
        self.bridge.command.connect(self._on_command)
        self.bridge.snapshot.connect(self._on_snapshot)
        self.bridge.status.connect(self._on_status)
        self.feed = CaptureFeed(parser_dir, self.bridge.snapshot.emit, self.bridge.status.emit)
        self.radar = RadarWindow(self.model, self.cfg["radar"])
        self.map = MapLayer(self.model, self.cfg["map"], self.save)
        self.guide = GuideWindow(self.model, self.cfg["arrow"], self.cfg["map"])
        self.last_reply = ""
        self.tick = QTimer()
        self.tick.timeout.connect(self._tick)  # keep "waiting" text fresh without new packets
        self.tick.start(500)

    # -- life cycle
    def start(self) -> None:
        if self.cfg.get("enabled", True):
            self.enable()

    def enable(self) -> None:
        from .parser_fetch import have_parser

        if not have_parser(self.feed.parser_dir):
            self.model.on_status("The cube parser is not downloaded yet (use Download on the page)", True)
            return
        self.radar.setVisible(bool(self.cfg["radar"].get("visible", True)))
        self._sync_arrow()
        if not self.feed.running:
            self.model.on_status("Starting capture…", False)
            self.feed.start(self.cfg.get("char", ""), self.cfg.get("elevate", "pkexec"))

    def disable(self) -> None:
        if self.map.calibrating:
            self.map.finish_calibration()
        for w in (self.radar, self.guide, self.map):
            w.hide()
        self.feed.stop()
        self.model.on_status("Overlay is off", False)

    def shutdown(self) -> None:
        if self.map.calibrating:
            self.map.finish_calibration()
        self.feed.stop()

    def save(self) -> None:
        self.settings.save()

    # -- thread-safe entry point
    def post(self, cmd: str) -> None:
        self.bridge.command.emit(cmd)

    def _on_command(self, cmd: str) -> None:
        try:
            self.last_reply = self.command(cmd)
        except Exception as e:  # a bad command must not take the app down
            log.error("overlay command %r failed: %s", cmd, e)

    def _sync_arrow(self) -> None:
        """Show the arrow while a cube is in range, hide it otherwise."""
        want = bool(self.cfg.get("enabled", True)) and arrow_wanted(self.model, self.cfg["arrow"])
        if want != self.guide.isVisible():
            self.guide.setVisible(want)

    def _on_snapshot(self, msg: dict) -> None:
        self.model.on_snapshot(msg)
        self._sync_arrow()
        fresh = self.model.new_cube_ids()
        if fresh and self.cfg.get("alert_sound", True) and self.model.player:
            play_alert()

    def _on_status(self, text: str, is_error: bool) -> None:
        self.model.on_status(text, is_error)
        (log.warning if is_error else log.info)("capture: %s", text)

    def _tick(self) -> None:
        self.radar.update()
        self.guide.update()

    # -- commands (Qt thread)
    def command(self, cmd: str) -> str:
        on = self.cfg.get("enabled", True)
        if cmd == "enable":
            self.cfg["enabled"] = True
            self.save()
            self.enable()
            return "overlay on"
        if cmd == "disable":
            self.cfg["enabled"] = False
            self.save()
            self.disable()
            return "overlay off"
        if cmd == "restart-capture":
            self.feed.stop()
            if on:
                self.enable()
            return "capture restarted"
        if not on:
            return "the overlay is off"
        if cmd == "map":
            if self.map.isVisible() and not self.map.calibrating:
                self.map.hide()
                return "map layer hidden"
            self.map.show()
            return "map layer shown"
        if cmd == "map-off":
            if self.map.calibrating:
                self.map.finish_calibration()
            self.map.hide()
            return "map layer hidden"
        if cmd == "calibrate":
            if self.map.calibrating:
                self.map.finish_calibration()
                return "calibration saved"
            self.map.start_calibration()
            return "calibration mode"
        if cmd == "calibrate-reset":
            self.cfg["map"]["points"].clear()
            self.save()
            self.map.update()
            return "calibration points cleared"
        if cmd == "radar":
            vis = not self.radar.isVisible()
            self.radar.setVisible(vis)
            self.cfg["radar"]["visible"] = vis
            self.save()
            return f"radar {'shown' if vis else 'hidden'}"
        if cmd == "arrow":
            # The setting arms the arrow; it pops up only while a cube is in range.
            on = not self.cfg["arrow"].get("visible", True)
            self.cfg["arrow"]["visible"] = on
            self.save()
            self._sync_arrow()
            rng = float(self.cfg["arrow"].get("range_m", 150.0))
            return f"arrow on (pops up within {rng:.0f} m of a cube)" if on else "arrow off"
        if cmd == "arrow-mode":
            mode = "north" if self.cfg["arrow"].get("mode", "heading") == "heading" else "heading"
            self.cfg["arrow"]["mode"] = mode
            self.save()
            self.guide.update()
            return f"arrow points relative to {'your movement' if mode == 'heading' else 'map-up'}"
        return f"unknown command: {cmd}"

    # -- read from any thread
    def status(self) -> dict:
        m = self.model
        return {"enabled": bool(self.cfg.get("enabled", True)), "capturing": self.feed.running,
                "status": m.status, "error": m.status_error,
                "player": list(m.player) if m.player else None, "cubes": len(m.cubes),
                "packets": m.packets, "facing_known": m.heading is not None,
                "readings_dropped": m.tracker.rejected,
                "map_visible": self.map.isVisible(), "calibrating": self.map.calibrating,
                "calibration_points": len(self.cfg["map"]["points"]), "map_mode": self.cfg["map"]["mode"],
                "radar_visible": bool(self.cfg["radar"].get("visible", True)),
                "arrow_visible": bool(self.cfg["arrow"].get("visible", True)),
                "arrow_showing": self.guide.isVisible(),
                "arrow_range_m": float(self.cfg["arrow"].get("range_m", 150.0)),
                "arrow_mode": self.cfg["arrow"].get("mode", "heading")}
