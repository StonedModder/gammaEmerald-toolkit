"""Fast travel using the game's own teleport volumes.

HOW IT WORKS, and why it is done this way:

`BP_GE_TeleportVolume_C` is the actor that moves you between maps. Setting its
`Brendan` pointer (+0x2E8) and `isOverlapping?` (+0x2F0), then taking a step,
makes the game run its own transition -- streaming the destination in and
placing the player. VERIFIED: travelled from Route 101 to MeteorFalls and the
player could walk in all four directions on arrival.

TWO THINGS THAT DO NOT WORK, recorded so they are not re-tried:

  * Retargeting the destination. `LevelToLoad` (+0x330) is an FSoftObjectPath
    -- package FName at +0x338, asset FName at +0x340 -- and rewriting those
    DOES change what the field reads back. It does NOT change where you go:
    copying volume B's destination into volume A still sent the player to A's
    original map. The reference is resolved before the volume runs.

  * Moving the volume to the player. It looks like a shortcut (the volumes sit
    tens of thousands of units away) but the arrival placement is computed from
    the volume, so the player lands inside geometry -- the "stuck in a wall"
    bug. Trigger volumes where they stand.

  * Moving the player onto the volume by writing their transform. It does move
    them, and then they fall out of the world -- Z traced from 64 down through
    -5,937 to -23,555 while the destination streamed in, so travel "succeeded"
    into an empty zone. The player has to STAY where they are; see nav.py for
    the full account of why no memory write can move them safely.

So travel is limited to the destinations the current level's volumes point at.
That is the game's own connectivity: hop to a neighbour, then use that level's
volumes to go further.
"""
from __future__ import annotations

import struct
import time

from . import nav

VOLUME_CLASS = "BP_GE_TeleportVolume_C"
LEVEL_TO_LOAD = 0x330      # FSoftObjectPath: +0x08 package FName, +0x10 asset
BRENDAN = 0x2E8
IS_OVERLAPPING = 0x2F0
STEPS_BEFORE_TP = 0x2E4


def volumes(game):
    """Every teleport volume in the loaded level, with where it goes."""
    gp = game.gp
    out = []
    for o in game.actors_of_class(VOLUME_CLASS):
        raw = gp.rpm(o + LEVEL_TO_LOAD, 32)
        pkg = asset = None
        if raw and len(raw) >= 24:
            try:
                pkg = game.resolve_name(*struct.unpack_from("<II", raw, 8))
                asset = game.resolve_name(*struct.unpack_from("<II", raw, 16))
            except Exception:
                pass
        out.append({
            "addr": o,
            "name": game.obj_name(o),
            "package": pkg,
            "map": asset or (pkg.rsplit("/", 1)[-1] if pkg else None),
            "pos": nav.location(gp, o),
        })
    return out


def destinations(game, from_pos=None):
    """Maps reachable from here: the NEAREST volume for each destination.

    Deduping on "first seen" hid the exit that mattered -- MeteorFalls has two
    volumes to Route 115, one 128 units from the player and one 40,000 away, and
    keeping the far one made travel look broken.
    """
    best = {}
    for v in volumes(game):
        if not v["map"] or not v["pos"]:
            continue
        cur = best.get(v["map"])
        if cur is None:
            best[v["map"]] = v
        elif from_pos is not None:
            if nav.dist2d(from_pos, v["pos"]) < nav.dist2d(from_pos, cur["pos"]):
                best[v["map"]] = v
    return sorted(best.values(), key=lambda v: v["map"])


def disarm_stale(game) -> int:
    """Clear `isOverlapping?` on every volume; returns how many were set.

    A volume left armed by an interrupted travel makes the player look frozen
    -- no direction moves them, in any map, until the flag is cleared. Cheap
    insurance, and the only way out if a previous run died mid-travel.
    """
    gp = game.gp
    cleared = 0
    for v in game.actors_of_class(VOLUME_CLASS):
        if gp.rpm(v + IS_OVERLAPPING, 1) == b"\x01":
            gp.wpm(v + IS_OVERLAPPING, b"\x00")
            cleared += 1
    return cleared


def loaded_worlds(game):
    gp = game.gp
    return {(game.obj_name(o) or "") for o in game.actors_of_class("World")
            if gp.read_u64(o + 0x30)}


def travel(game, gi, pawn_addr, volume_addr, timeout: float = 90.0,
           on_event=None) -> dict:
    """Fast-travel through a teleport volume and report where the player lands.

    The player never moves under our control: the volume is told they are
    standing in it, and the game runs its own transition and places them. That
    placement is the entire point -- it is what puts the player on solid ground
    at the far side instead of falling through an unloaded map.
    """
    gp = game.gp

    def emit(kind, **kw):
        if on_event:
            on_event({"kind": kind, **kw})

    info = next((v for v in volumes(game) if v["addr"] == volume_addr), None)
    if info is None:
        return {"ok": False, "error": "no such teleport volume"}
    if not info["pos"]:
        return {"ok": False, "error": "teleport volume has no position"}

    walker = nav.Walker(gp, game, gi, pawn_addr)
    start = walker.where()
    before = loaded_worlds(game)
    emit("travel", map=info["map"], stage="teleporting",
         distance=round(nav.dist2d(start, info["pos"])) if start else None)

    # Tell the volume the player is standing in it, and leave the player where
    # they are. The volume does the rest, and the GAME places the player at the
    # far side -- which is the only way the landing Z is right. Measured from
    # 89,445 units away: fired after 2 steps, arrived on solid ground, able to
    # walk in all four directions.
    #
    # PULSE the flag, never hold it. While `isOverlapping?` is true the game
    # treats the player as mid-transition and ignores movement entirely, so a
    # loop that re-armed it every pass froze the player and then waited forever
    # for a step that could not happen. Arm, take one step, disarm.
    disarm_stale(game)
    deadline = time.time() + timeout
    changed = None
    nudges = ("d", "a", "w", "s")
    i = 0
    try:
        while time.time() < deadline:
            gp.wpm(volume_addr + BRENDAN, struct.pack("<Q", pawn_addr))
            gp.wpm(volume_addr + IS_OVERLAPPING, b"\x01")
            walker.step(nudges[i % len(nudges)])
            i += 1
            game.refresh()
            now = loaded_worlds(game)
            if now != before:
                changed = now
                break
            gp.wpm(volume_addr + IS_OVERLAPPING, b"\x00")
            time.sleep(0.15)
    finally:
        if changed is None:
            # a half-armed volume is what leaves the player unable to move
            gp.wpm(volume_addr + IS_OVERLAPPING, b"\x00")

    if not changed:
        emit("travel", map=info["map"], stage="could not reach it")
        return {"ok": False, "map": info["map"],
                "error": "the teleport volume did not fire"}

    time.sleep(3.5)          # let the destination finish streaming in
    game.refresh()
    arrived_worlds = sorted(set(loaded_worlds(game)) - set(before))

    from .hunt import Player
    p = Player.find(game)
    pos = nav.location(gp, p.addr) if p else None
    stuck = False
    if p:
        stuck = nav.Walker(gp, game, gi, p.addr).stuck()
    emit("travel", map=info["map"], stage="arrived", worlds=arrived_worlds,
         stuck=stuck)
    return {
        "ok": True,
        "map": info["map"],
        "worlds": arrived_worlds,
        "arrived": [round(v) for v in pos] if pos else None,
        "pawn": hex(p.addr) if p else None,
        "stuck": stuck,
    }
