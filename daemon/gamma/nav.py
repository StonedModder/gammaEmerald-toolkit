"""Overworld navigation: where things are, and how to walk to them.

MEASURED against the EA build, not guessed:

  AActor::RootComponent            +0x1B8   (engine reflection)
  USceneComponent::RelativeLocation +0x140  FVector of *doubles* (UE5)
  grid step                         64.0 world units
  w = +X   s = -X   d = +Y   a = -Y

Movement is an Enhanced Input **Axis2D** (`IA_PlayerMovement`) bound to WASD.
Axis bindings read keyboard STATE, so posted WM_KEYDOWNs move the player
nowhere -- arrows drive menus only. `GameInput.bg_hold` sets the state via
AttachThreadInput, which is what actually makes the player walk without the
game having focus.
"""
from __future__ import annotations

import math
import struct
import time

ROOT_COMPONENT = 0x1B8
RELATIVE_LOCATION = 0x140
GRID = 64.0

GRASS_CLASS = "BP_GrassTile_C"

# axis -> (key for +, key for -)
AXIS_KEYS = {0: ("w", "s"), 1: ("d", "a")}


def location(gp, actor: int):
    """World position of an actor, or None."""
    if not actor:
        return None
    root = gp.read_u64(actor + ROOT_COMPONENT)
    if not root:
        return None
    raw = gp.rpm(root + RELATIVE_LOCATION, 24)
    if not raw or len(raw) < 24:
        return None
    return struct.unpack("<ddd", raw)


def actors_of_class(game, class_name: str, limit: int = 4000):
    """Live actors of a class, skipping the CDO.

    Delegates to the pointer-compare scan: resolving a class NAME per object
    over 258k objects took tens of seconds per call.
    """
    return game.actors_of_class(class_name, limit=limit)


def grass_tiles(game):
    """[(actor, (x, y, z))] for every grass tile in the loaded level."""
    gp = game.gp
    out = []
    for o in actors_of_class(game, GRASS_CLASS):
        p = location(gp, o)
        if p:
            out.append((o, p))
    return out


def dist2d(a, b) -> float:
    """Horizontal distance only.

    The tile mesh sits on the ground and the player's origin is ~51 units above
    it, so a 3D distance says you are 51 units from the tile you are standing
    on. On a grid game the vertical component is never meaningful.
    """
    return math.hypot(a[0] - b[0], a[1] - b[1])


def nearest(point, tiles):
    """The closest (actor, pos) to `point`, horizontally. None if there are none."""
    if not tiles:
        return None
    return min(tiles, key=lambda t: dist2d(point, t[1]))


def on_grass(point, tiles) -> bool:
    """True when the player is standing on a grass tile."""
    n = nearest(point, tiles)
    return bool(n) and dist2d(point, n[1]) < GRID * 0.5


def snap(v: float) -> float:
    return round(v / GRID) * GRID


class Walker:
    """Grid walking with stuck detection.

    Deliberately dumb: step along one axis at a time and watch the position
    actually change. There is no pathfinder here because the routes are open
    grids -- and a walker that *notices* it is blocked is more useful than one
    that believes a path it cannot verify.
    """

    def __init__(self, gp, game, gi, pawn_addr: int, on_step=None):
        self.gp = gp
        self.game = game
        self.gi = gi
        self.pawn = pawn_addr
        self.on_step = on_step
        self.hold = 0.40         # long enough for one 64-unit step
        self.settle = 0.45

    def where(self):
        return location(self.gp, self.pawn)

    def step(self, key: str) -> tuple | None:
        """One step. Returns the new position, or None if it did not move."""
        before = self.where()
        self.gi.bg_hold(key, self.hold)
        time.sleep(self.settle)
        after = self.where()
        if self.on_step:
            self.on_step(after)
        if not before or not after:
            return None
        moved = dist2d(before, after) > GRID * 0.4
        return after if moved else None

    def walk_axis(self, axis: int, target: float, max_steps: int = 40) -> bool:
        """Close the gap on one axis. False if blocked before arriving."""
        plus, minus = AXIS_KEYS[axis]
        blocked = 0
        for _ in range(max_steps):
            pos = self.where()
            if pos is None:
                return False
            delta = target - pos[axis]
            if abs(delta) < GRID * 0.5:
                return True
            if self.step(plus if delta > 0 else minus) is None:
                blocked += 1
                if blocked >= 3:
                    return False
            else:
                blocked = 0
        return False

    def walk_to(self, target, max_steps: int = 80) -> bool:
        """Walk to a world position, going around what blocks the way.

        Greedy with detours, not "one axis then the other": routes are full of
        fences and ledges, and a position where BOTH preferred directions are
        blocked is normal. Measured on Route 101, the player could move south
        and west but not north or east from a single tile, which defeats any
        fixed axis order.

        Blocked (cell, direction) pairs are remembered so the walker does not
        keep shouldering the same fence, and a sideways move is allowed even
        though it does not reduce the distance -- that is what gets around a
        wall. No full pathfinder: this is a grid with sparse obstacles, and a
        walker that knows when it is stuck beats one that pretends otherwise.
        """
        blocked: set[tuple] = set()
        for _ in range(max_steps):
            pos = self.where()
            if pos is None:
                return False
            if dist2d(pos, target) < GRID * 0.5:
                return True

            cell = (snap(pos[0]), snap(pos[1]))
            dx, dy = target[0] - pos[0], target[1] - pos[1]
            wants = []
            if abs(dx) >= GRID * 0.5:
                wants.append((abs(dx), "w" if dx > 0 else "s"))
            if abs(dy) >= GRID * 0.5:
                wants.append((abs(dy), "d" if dy > 0 else "a"))
            wants.sort(reverse=True)                 # close the bigger gap first
            order = [k for _d, k in wants]
            # detours: sideways first, backwards only as a last resort
            order += [k for k in ("a", "d", "w", "s") if k not in order]

            moved = False
            for key in order:
                if (cell, key) in blocked:
                    continue
                if self.step(key) is not None:
                    moved = True
                    break
                blocked.add((cell, key))
            if not moved:
                return False
        pos = self.where()
        return pos is not None and dist2d(pos, target) < GRID

    def stuck(self) -> bool:
        """True when the player cannot move in ANY direction.

        Worth checking after a level change: arriving inside geometry leaves the
        player walled in, which looks like the game has frozen. Every direction
        is tried, and the player is walked back if one of them worked, so the
        check itself does not move them.
        """
        for key, back in (("w", "s"), ("a", "d")):
            if self.step(key) is not None:
                self.step(back)
                return False
            if self.step(back) is not None:
                self.step(key)
                return False
        return True

    def pace(self, steps: int = 2, until=None, timeout: float = 120.0):
        """Walk back and forth, which is how encounters are farmed.

        Stops early when `until()` says so, so an encounter is noticed the
        moment it starts rather than after a fixed number of steps.
        """
        t0 = time.time()
        i = 0
        while time.time() - t0 < timeout:
            key = ("d", "a")[(i // max(1, steps)) % 2]
            self.step(key)
            i += 1
            if until and until():
                return True
        return False
