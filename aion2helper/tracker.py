"""Turns the parser's raw position readings into a steady position and facing.

The raw stream is noisy in two ways (seen in real captures):
  * readings of (0, 0, 0) or close to it;
  * readings of another entity mixed in, tens of metres away, that bounce back.
Both used to drive the arrow directly. This tracker keeps one track:

  * a reading that is reachable from the track at a plausible speed is accepted;
  * anything else is held as a "candidate". If several candidates in a row agree
    with each other (or the track has gone quiet), that was a real teleport and the
    track restarts there; otherwise they were someone else and are dropped;
  * facing is the direction over the last few metres of accepted path, not the
    last single step, and it is smoothed so one wobble can't flip it.

The game only sends your position with skills, dashes and zone changes, so while
you just walk the raw position freezes. Between readings the tracker estimates
your own movement (dead reckoning): while your client is sending movement packets
(``note_movement``), ``estimate`` moves along your facing at your walking speed,
which is learned from how far real readings say you went. Every real reading
snaps the estimate back onto it. ``pos`` stays the last real reading.

No Qt here, so it can be tested on its own.
"""
from __future__ import annotations

import math
import time
from collections import deque

NULL_CM = 100.0             # |x| and |y| under 1 m: the parser's "no position"
MAX_SPEED_CMPS = 1500.0     # 15 m/s: faster "moves" in real captures were other entities
GAP_CAP_S = 1.5             # a long silence doesn't widen the gate further than this
GATE_SLACK_CM = 400.0       # extra room for jitter and update timing
CONFIRM_READINGS = 3        # agreeing candidates needed to accept a jump
CONFIRM_SPREAD_CM = 800.0   # ...and how close together they must be
STALE_S = 8.0               # no accepted reading for this long: trust candidates sooner
HEADING_MIN_CM = 200.0      # need this much straight-line travel before facing updates
HEADING_WINDOW_CM = 600.0   # facing looks back along about this much path
HEADING_SMOOTH = 0.5        # 0..1, share of each new facing estimate that is taken
WALK_SPEED_CMPS = 650.0     # starting guess for walking speed (the parser's own figure)
MIN_WALK_CMPS = 300.0       # learned speed stays within these
MAX_WALK_CMPS = 1200.0
MOVING_S = 0.35             # a movement packet this recently means you're still moving
MAX_ESTIMATE_CM = 30000.0   # never estimate more than 300 m past the last real reading


class PositionTracker:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.pos = None             # accepted position (x, y, z), cm
        self.pos_time = 0.0         # last time a reading agreed with pos (repeats count)
        self._changed = 0.0         # last time pos actually moved to a new reading
        self.heading = None         # unit (dx, dy)
        self.heading_time = 0.0
        self.rejected = 0           # how many readings were dropped (for status)
        self._path = deque()        # recent accepted points, for facing
        self._cands = []
        # dead reckoning
        self.estimate = None        # best guess of where you are now (x, y, z), cm
        self.speed = WALK_SPEED_CMPS
        self._last_move = -1e9      # when the last movement packet was seen
        self._adv_time = 0.0        # estimate integrated up to this time
        self._moved_s = 0.0         # seconds spent moving since the last real reading
        self._est_cm = 0.0          # distance estimated since the last real reading

    # -- public
    def note_movement(self):
        """Your client sent a movement packet: you are moving right now."""
        now = self.clock()
        self.advance(now)
        self._last_move = now

    def advance(self, now=None):
        """Move the estimate forward to now. Returns True if it moved."""
        now = self.clock() if now is None else now
        start, self._adv_time = self._adv_time, now
        # only the part of [start, now] during which movement packets were still coming
        moving = max(0.0, min(now, self._last_move + MOVING_S) - max(start, self._last_move))
        if self.pos is None or moving <= 0.0:
            return False
        self._moved_s += moving
        if self.heading is None or self._est_cm >= MAX_ESTIMATE_CM:
            return False
        step = min(self.speed * moving, MAX_ESTIMATE_CM - self._est_cm)
        self._est_cm += step
        ex, ey, ez = self.estimate
        self.estimate = (ex + self.heading[0] * step, ey + self.heading[1] * step, ez)
        return True

    def update(self, raw):
        """Feed one raw reading (or None). Returns True if the tracked position changed."""
        if raw is None or (abs(raw[0]) < NULL_CM and abs(raw[1]) < NULL_CM):
            return False
        now = self.clock()
        self.advance(now)
        raw = tuple(float(v) for v in raw)
        if self.pos is None:
            self._reset(raw, now)
            return True
        if raw == self.pos:
            self.pos_time = now
            return False
        # Silence while standing still says you can't be far; silence while walking
        # (no skills used) means you may have gone a long way, so widen the gate by it.
        # Measured from the last new reading: the capture repeats a frozen position in
        # every snapshot, and those repeats say nothing about how far you've walked.
        dt = min(GAP_CAP_S + self._moved_s, max(0.05, now - self._changed))
        gate = MAX_SPEED_CMPS * dt + GATE_SLACK_CM
        if _dist2d(raw, self.pos) <= gate:
            self._cands.clear()
            self._accept(raw, now)
            return True
        return self._candidate(raw, now)

    # -- internals
    def _candidate(self, raw, now):
        # keep only earlier candidates that this one could have come from
        self._cands = [c for c in self._cands
                       if _dist2d(c[0], raw) <= CONFIRM_SPREAD_CM + MAX_SPEED_CMPS * (now - c[1])]
        if not self._cands or self._cands[-1][0] != raw:   # repeats of one reading count once
            self._cands.append((raw, now))
        stale = now - self.pos_time > STALE_S
        if stale or len(self._cands) >= CONFIRM_READINGS:
            self._reset(raw, now)       # teleport, zone change, or we were tracking the wrong thing
            return True
        self.rejected += 1
        return False

    def _reset(self, raw, now):
        self.pos, self.pos_time, self._changed = raw, now, now
        self.heading = None
        self._path.clear()
        self._path.append(raw)
        self._cands.clear()
        self._snap_estimate(raw)

    def _snap_estimate(self, raw):
        self.estimate = raw
        self._moved_s = 0.0
        self._est_cm = 0.0

    def _learn_speed(self, raw):
        # how fast you really went while the movement packets said you were walking
        if self._moved_s < 1.0:
            return
        seen = _dist2d(raw, self.pos) / self._moved_s
        if MIN_WALK_CMPS * 0.5 <= seen <= MAX_WALK_CMPS * 1.5:
            self.speed = min(MAX_WALK_CMPS, max(MIN_WALK_CMPS, 0.5 * self.speed + 0.5 * seen))

    def _accept(self, raw, now):
        self._learn_speed(raw)
        self._snap_estimate(raw)
        self.pos, self.pos_time, self._changed = raw, now, now
        self._path.append(raw)
        # keep about HEADING_WINDOW_CM of path behind the newest point
        total = 0.0
        pts = list(self._path)
        keep_from = 0
        for i in range(len(pts) - 1, 0, -1):
            total += _dist2d(pts[i], pts[i - 1])
            if total >= HEADING_WINDOW_CM:
                keep_from = i - 1
                break
        for _ in range(keep_from):
            self._path.popleft()
        self._update_heading(now)

    def _update_heading(self, now):
        start, end = self._path[0], self._path[-1]
        dx, dy = end[0] - start[0], end[1] - start[1]
        d = math.hypot(dx, dy)
        if d < HEADING_MIN_CM:
            return
        new = (dx / d, dy / d)
        if self.heading is None:
            self.heading = new
        else:
            # blend on the circle, so 350° and 10° average to 0°, not 180°
            a0 = math.atan2(self.heading[1], self.heading[0])
            a1 = math.atan2(new[1], new[0])
            diff = (a1 - a0 + math.pi) % (2 * math.pi) - math.pi
            if abs(diff) > math.radians(120):
                a = a1                  # a real U-turn: follow it right away
            else:
                a = a0 + diff * HEADING_SMOOTH
            self.heading = (math.cos(a), math.sin(a))
        self.heading_time = now


def _dist2d(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])
