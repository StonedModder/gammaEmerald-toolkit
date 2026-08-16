"""Starter soft-reset shiny hunter, and the odds controls that make it testable.

THE STARTER SHINY MECHANISM (verified live 2026-08-15):
  `BP_GE_PickStarterPlayer_C` owns it. The scene rolls when the starter screen
  loads, then exposes the result per species:
      ShinyFrame       +0x3E0  int   the rolled value (e.g. 477 on a dud)
      ShinyStarterID   +0x3E4  int   0 Treecko / 1 Torchic / 2 Mudkip
      isShinyTreecko?  +0x3E8  bool
      isShinyTorchic?  +0x3E9  bool
      isShinyMudkip?   +0x3EA  bool
  Writing isShinyTreecko? = True and confirming produced a shiny (teal) Treecko
  with the shiny marker on its HP bar. That is the working cheat.

  Because the flags are readable BEFORE the pick is confirmed, a hunt reads the
  roll the moment the screen opens and soft-resets a dud without ever accepting a
  Pokemon. The save is never written.

DEAD END, recorded so it is not re-tried:
  `BP_Brendan_C.ShinyRate` (+0xA0C, 1023) and `isStarterShiny?` (+0xA31) look like
  the mechanism and are not. Setting ShinyRate to 0 BEFORE the starter scene loaded
  and confirming still produced a normal Treecko, and isStarterShiny? never flipped.
  Those fields belong to some other path (overworld//wild), not the starter.

  Separately, the `EX_FloatConst 0.01` sites in shiny.py are wild-encounter odds
  and also do not affect the starter.

All offsets VERIFIED live against the EA build; see layouts.py for method.
"""
from __future__ import annotations

import struct
import time
from pathlib import Path
from dataclasses import dataclass, field, asdict


# --- BP_Brendan_C property offsets (EA / UE 5.6, read via reflection) --------
BRENDAN = {
    "ShinyRate":       (0xA0C, "i"),
    "ShinyFrame":      (0xA10, "i"),
    "isStarterShiny?": (0xA31, "b"),
    "ShinySteps":      (0xC5C, "i"),
    "WildLevel":       (0xE2C, "i"),
}

# Facing Birch's bag, left → right. Slot is how many times to tap Right after
# pinning the cursor on the leftmost ball.
STARTERS = (
    {"id": "treecko", "name": "Treecko", "slot": 0},
    {"id": "torchic", "name": "Torchic", "slot": 1},
    {"id": "mudkip",  "name": "Mudkip",  "slot": 2},
)
STARTER_BY_ID = {s["id"]: s for s in STARTERS}

PAWN_CLASSES = (
    "BP_Brendan_C",
    "BP_GE_PickStarterPlayer_C",
    "BP_PickStarterPlayerDEMO_C",
    "BP_SuitcaseStarter_C",
    "BP_PickStarterPlayer_C",
)
FALLBACK_PAWNS = PAWN_CLASSES[1:]


def odds_from_rate(rate: int) -> str:
    """ShinyRate is a rand(0..rate)==0 bound, so the human odds are 1/(rate+1)."""
    if rate is None:
        return "?"
    if rate <= 0:
        return "1/1 (forced)"
    return f"1/{rate + 1:,}"


# --- BP_GE_PickStarterPlayer_C: the REAL starter shiny mechanism ------------
# VERIFIED live 2026-08-15: writing isShinyTreecko? = True and confirming the pick
# produced a shiny (teal) Treecko with the shiny marker on its HP bar. The scene
# rolls these flags when it loads -- ShinyFrame held 477 on a non-shiny roll -- so
# a hunt can read the flag the moment the starter screen appears and reset without
# ever confirming a dud.
STARTER_SCENE_CLASS = "BP_GE_PickStarterPlayer_C"
SCENE_OFFSETS = {
    "ShinyFrame": (0x3E0, "i"),
    "ShinyStarterID": (0x3E4, "i"),
    "isShinyTreecko?": (0x3E8, "b"),
    "isShinyTorchic?": (0x3E9, "b"),
    "isShinyMudkip?": (0x3EA, "b"),
}
STARTER_FLAG = {"treecko": "isShinyTreecko?",
                "torchic": "isShinyTorchic?",
                "mudkip": "isShinyMudkip?"}
STARTER_SCENE_ID = {"treecko": 0, "torchic": 1, "mudkip": 2}


class StarterScene:
    """The live starter-selection controller. Exists only while the pick is open."""

    def __init__(self, game, addr):
        self.game = game
        self.gp = game.gp
        self.addr = addr

    @classmethod
    def _from_obj(cls, game, o):
        if (game.class_name(o) or "") != STARTER_SCENE_CLASS:
            return None
        if (game.obj_name(o) or "").startswith("Default__"):
            return None
        return cls(game, o)

    @classmethod
    def find_in_level(cls, game, layout, level):
        """Scan ULevel::Actors (hundreds), not GUObjectArray (258k)."""
        ap = game.gp.read_u64(level + layout.level_actors)
        cnt = game.gp.read_u32(level + layout.level_actors_count)
        if not ap or not (0 < cnt < 100000):
            return None
        raw = game.gp.rpm(ap, cnt * 8)
        if not raw:
            return None
        for i in range(cnt):
            actor = struct.unpack_from("<Q", raw, i * 8)[0]
            if actor:
                hit = cls._from_obj(game, actor)
                if hit:
                    return hit
        return None

    @classmethod
    def find(cls, game):
        # pointer-compare scan: resolving a class NAME per object over 258k
        # objects was seconds per call, and this runs in a poll loop
        for o in game.actors_of_class(STARTER_SCENE_CLASS, limit=8):
            hit = cls._from_obj(game, o)
            if hit:
                return hit
        return None

    def alive(self) -> bool:
        try:
            return (self.game.class_name(self.addr) or "") == STARTER_SCENE_CLASS
        except Exception:
            return False

    def get(self, name):
        off, kind = SCENE_OFFSETS[name]
        raw = self.gp.rpm(self.addr + off, 4 if kind == "i" else 1)
        if not raw:
            return None
        return struct.unpack("<i", raw)[0] if kind == "i" else bool(raw[0])

    def set(self, name, value) -> bool:
        off, kind = SCENE_OFFSETS[name]
        blob = struct.pack("<i", int(value)) if kind == "i" else bytes([1 if value else 0])
        return self.gp.wpm(self.addr + off, blob) == len(blob)

    def snapshot(self) -> dict:
        d = {k: self.get(k) for k in SCENE_OFFSETS}
        d["addr"] = hex(self.addr)
        return d

    def is_shiny(self, starter: str):
        return self.get(STARTER_FLAG[starter])

    def force(self, starter: str) -> bool:
        ok = self.set(STARTER_FLAG[starter], True)
        self.set("ShinyStarterID", STARTER_SCENE_ID[starter])
        return ok


class Player:
    """Typed access to the live BP_Brendan_C instance."""

    def __init__(self, game, addr: int, class_name: str = "BP_Brendan_C"):
        self.game = game
        self.gp = game.gp
        self.addr = addr
        self.class_name = class_name

    def alive(self) -> bool:
        try:
            cn = self.game.class_name(self.addr) or ""
            name = self.game.obj_name(self.addr) or ""
            return cn in PAWN_CLASSES and not name.startswith("Default__")
        except Exception:
            return False

    @classmethod
    def _from_obj(cls, game, o):
        cn = game.class_name(o) or ""
        name = game.obj_name(o) or ""
        if name.startswith("Default__"):
            return None
        if cn == "BP_Brendan_C":
            return cls(game, o, cn), "brendan"
        if cn in FALLBACK_PAWNS:
            return cls(game, o, cn), "fallback"
        return None

    @classmethod
    def find_in_worlds(cls, game, layout, worlds):
        """Scan ULevel::Actors of cached UWorlds — hundreds of actors, not 250k objects."""
        fallback = None
        for w in worlds or ():
            try:
                if (game.class_name(w) or "") != "World":
                    continue
                pl = game.gp.read_u64(w + layout.world_persistent_level)
                if not pl:
                    continue
                ap = game.gp.read_u64(pl + layout.level_actors)
                cnt = game.gp.read_u32(pl + layout.level_actors_count)
                if not ap or not (0 < cnt < 100000):
                    continue
                raw = game.gp.rpm(ap, cnt * 8)
                if not raw:
                    continue
                for i in range(cnt):
                    actor = struct.unpack_from("<Q", raw, i * 8)[0]
                    if not actor:
                        continue
                    hit = cls._from_obj(game, actor)
                    if not hit:
                        continue
                    player, kind = hit
                    if kind == "brendan":
                        return player
                    if fallback is None:
                        fallback = player
            except Exception:
                continue
        return fallback

    @classmethod
    def find(cls, game, worlds_out=None):
        """Live pawn, not the CDO. Prefer Brendan; fall back to the suitcase picker.

        Also records UWorld pointers into worlds_out so later lookups can use
        find_in_worlds instead of walking GUObjectArray again.

        Uses the pointer-compare scan rather than resolving a class name per
        object -- the old form walked 258k objects reading three fields each and
        took the better part of half a minute.
        """
        if worlds_out is not None:
            worlds_out.extend(game.actors_of_class("World"))
        fallback = None
        for cname in PAWN_CLASSES:
            for o in game.actors_of_class(cname):
                hit = cls._from_obj(game, o)
                if not hit:
                    continue
                player, kind = hit
                if kind == "brendan":
                    return player
                if fallback is None:
                    fallback = player
        return fallback

    def get(self, prop: str):
        off, kind = BRENDAN[prop]
        raw = self.gp.rpm(self.addr + off, 4 if kind == "i" else 1)
        if not raw:
            return None
        return struct.unpack("<i", raw)[0] if kind == "i" else bool(raw[0])

    def set(self, prop: str, value) -> bool:
        off, kind = BRENDAN[prop]
        blob = struct.pack("<i", int(value)) if kind == "i" else bytes([1 if value else 0])
        return self.gp.wpm(self.addr + off, blob) == len(blob)

    @property
    def shiny_rate(self):
        return self.get("ShinyRate")

    @property
    def is_starter_shiny(self):
        return self.get("isStarterShiny?")

    def snapshot(self) -> dict:
        d = {k: self.get(k) for k in BRENDAN}
        d["odds"] = odds_from_rate(d.get("ShinyRate"))
        d["class"] = self.class_name
        d["addr"] = hex(self.addr)
        return d


@dataclass
class HuntStats:
    """Live hunt numbers. Per-starter counters persist across target changes so a
    session's history is not lost when you switch which starter you are after."""
    target: str = "treecko"
    attempts: int = 0
    started: float = field(default_factory=time.time)
    last_attempt: float | None = None
    found: bool = False
    found_at: int | None = None
    status: str = "idle"
    odds: str = "natural"
    shiny_rate: int | None = None
    shiny_frame: int | None = None
    error: str | None = None
    forced: bool = False

    # per-starter: resets attempted, shinies found, and the roll count of each find
    resets: dict = field(default_factory=lambda: {s["id"]: 0 for s in STARTERS})
    shinies: dict = field(default_factory=lambda: {s["id"]: 0 for s in STARTERS})
    found_on: dict = field(default_factory=lambda: {s["id"]: [] for s in STARTERS})
    last_reset_secs: float | None = None
    reset_secs: list = field(default_factory=list)
    phases: dict = field(default_factory=dict)   # step -> (samples, total secs)

    @property
    def elapsed(self) -> float:
        return time.time() - self.started

    @property
    def per_hour(self) -> float:
        """Resets per hour. Uses measured cycle time once we have real samples,
        because wall-clock elapsed includes setup and would understate the rate."""
        if self.reset_secs:
            avg = sum(self.reset_secs) / len(self.reset_secs)
            return 3600.0 / avg if avg > 0 else 0.0
        e = self.elapsed
        return (self.attempts / e * 3600.0) if e > 1 else 0.0

    @property
    def avg_reset(self) -> float:
        return (sum(self.reset_secs) / len(self.reset_secs)) if self.reset_secs else 0.0

    def record_attempt(self, starter: str, shiny: bool, frame=None):
        self.attempts += 1
        self.resets[starter] = self.resets.get(starter, 0) + 1
        self.last_attempt = time.time()
        self.shiny_frame = frame
        if shiny:
            self.shinies[starter] = self.shinies.get(starter, 0) + 1
            self.found_on.setdefault(starter, []).append(self.resets[starter])
            self.found = True
            self.found_at = self.resets[starter]

    def record_cycle(self, secs: float):
        self.last_reset_secs = secs
        self.reset_secs.append(secs)
        if len(self.reset_secs) > 50:
            self.reset_secs.pop(0)

    def phase(self, name: str, secs: float):
        """Running mean per step of the cycle. Without this the only number is the
        total, and a slow cycle gives no clue which step ate it."""
        n, tot = self.phases.get(name, (0, 0.0))
        self.phases[name] = (n + 1, tot + secs)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["elapsed"] = round(self.elapsed, 1)
        d["per_hour"] = round(self.per_hour, 1)
        d["avg_reset"] = round(self.avg_reset, 1)
        d["phases"] = {k: round(t / n, 1) for k, (n, t) in self.phases.items() if n}
        d["total_resets"] = sum(self.resets.values())
        d["total_shinies"] = sum(self.shinies.values())
        return d


class StarterHunter:
    """Soft-reset loop for the starter.

    Deliberately conservative: every step waits on an OBSERVED state change rather
    than a fixed sleep, because a fixed sleep is how these loops silently desync and
    spend an hour mashing keys at a menu that never opened.
    """

    def __init__(self, game, layout, gi, player=None, on_event=None,
                 starter="torchic", open_bag=True, force_shiny=False):
        self.game = game
        self.gp = game.gp
        self.L = layout
        self.input = gi
        # Lazy on purpose: constructing used to run a full 240k-object scan, which
        # blocked the hunt.start RPC long enough that the UI looked frozen and the
        # reply never arrived. ensure_in_game() finds the pawn when it is needed.
        self.player = player
        self.on_event = on_event or (lambda *_: None)
        spec = STARTER_BY_ID.get(str(starter).lower()) or STARTERS[1]
        self.starter_id = spec["id"]
        self.starter_slot = spec["slot"]
        self.open_bag = bool(open_bag)
        self.force_shiny = bool(force_shiny)
        self.reattach = None       # callable(hunter) -> bool, set by the daemon
        self.exe_path = None       # pathlib.Path to PokemonEmerald.exe
        self.scene = None
        self.worlds = []       # UWorld addrs from attach; used to scan level actors
        self.stats = HuntStats(target=self.starter_id)
        self._stop = False
        self._pawncls = {}     # FUObjectItem bytes -> class ptr, see find_pawn
        self._scene_wait_t0 = 0.0

    def configure(self, starter=None, open_bag=None, force_shiny=None):
        # NB: never touch reattach/exe_path here. Clearing them (as this used to)
        # disarmed the recovery path every time the starter was switched.
        if force_shiny is not None:
            self.force_shiny = bool(force_shiny)
        if starter is not None:
            spec = STARTER_BY_ID.get(str(starter).lower()) or STARTERS[1]
            self.starter_id = spec["id"]
            self.starter_slot = spec["slot"]
            self.stats.target = self.starter_id
        if open_bag is not None:
            self.open_bag = bool(open_bag)

    # ------------------------------------------------------------ observation
    def widget_count(self, name: str) -> int:
        return len(self.game.actors_of_class(name, skip_cdo=False))

    def wait_for(self, predicate, timeout=15.0, poll=0.25, what="state"):
        end = time.time() + timeout
        while time.time() < end:
            if self._stop:
                return False
            try:
                if predicate():
                    return True
            except Exception:
                pass
            time.sleep(poll)
        self.emit("timeout", {"waiting_for": what})
        return False

    def emit(self, kind, data=None):
        payload = {"kind": kind, "stats": self.stats.as_dict()}
        if data:
            payload.update(data)
        self.on_event(payload)

    # ----------------------------------------------------------------- control
    def set_rate(self, rate: int) -> bool:
        ok = self.player.set("ShinyRate", rate)
        self.stats.shiny_rate = self.player.shiny_rate
        self.stats.odds = odds_from_rate(self.stats.shiny_rate)
        self.emit("odds", {})
        return ok

    def refresh_stats(self):
        if self.force_shiny:
            self.stats.odds = "1/1 (forced)"
            self.stats.shiny_rate = 0
            return
        if self.player:
            self.stats.shiny_rate = self.player.shiny_rate
            self.stats.odds = odds_from_rate(self.stats.shiny_rate)

    def stop(self):
        self._stop = True

    # -------------------------------------------------------------------- loop
    def select_starter(self, starter=None):
        """Move the cursor to a specific ball.

        Arrow keys DO work here, unlike the pause menu (which is mouse-only) --
        verified: RIGHT from Treecko lands on Torchic. Pin left first so the
        starting position never matters.
        """
        sid = starter or self.starter_id
        slot = STARTER_BY_ID[sid]["slot"]
        for _ in range(3):
            if self._stop:
                return
            self.input.tap("left", settle=0.22)
        for _ in range(slot):
            if self._stop:
                return
            self.input.tap("right", settle=0.22)

    # IA_ResetGame is bound to LeftShift chorded with R (IA_UseKeyItem). Posting
    # VK_LSHIFT (0xA0) specifically matters -- generic VK_SHIFT (0x10) does not
    # match the binding and silently does nothing.
    VK_LSHIFT, VK_R = 0xA0, 0x52

    def in_world(self) -> bool:
        """True when a live player pawn exists. Two reads, no scan.

        This replaced an object-count threshold that was simply wrong: after the
        first save load the count stays ~257,900 even back at the main menu, so
        "at title" read as False forever and every reset looked like a failure.
        """
        return bool(self.player and self.player.alive())

    def find_pawn(self, window=24000, deep=False):
        """Fast pawn lookup: compare the CLASS POINTER, newest objects first.

        Player.find resolves a name per object and costs ~7s over 258k objects.
        Comparing one pointer per object and scanning backwards (a freshly spawned
        pawn lands at the end of the array) is much cheaper.

        The remaining cost is one ReadProcessMemory per object to fetch its class
        pointer -- 24,000 syscalls, ~6s, paid on every poll of the load wait. Those
        objects barely change between polls, so each object's class is memoised.

        The cache key is the object's whole 32-byte FUObjectItem, not its address:
        UE recycles array slots, and a slot's SerialNumber lives in those bytes, so
        a recycled slot simply misses the cache instead of returning the class of
        the object that used to be there.
        """
        cls = self._class_ptr("BP_Brendan_C")
        if cls:
            g = self.game
            total = g.gobjects["num_elements"]
            per = 65536
            scanned = 0
            cache = self._pawncls
            read_u64 = self.gp.read_u64
            for ci in range(g.gobjects["num_chunks"] - 1, -1, -1):
                chunk = g._chunks[ci]
                n = min(per, total - ci * per)
                if n <= 0:
                    continue
                items = self.gp.rpm(chunk, n * 32)
                if not items:
                    continue
                for k in range(n - 1, -1, -1):
                    base = k * 32
                    o = struct.unpack_from("<Q", items, base)[0]
                    scanned += 1
                    if o:
                        slot = bytes(items[base:base + 32])
                        cp = cache.get(slot)
                        if cp is None:
                            cp = read_u64(o + 0x10) or 0
                            if len(cache) < 400000:
                                cache[slot] = cp
                        if cp == cls:
                            name = self.game.obj_name(o) or ""
                            if not name.startswith("Default__"):
                                self.player = Player(self.game, o, "BP_Brendan_C")
                                return self.player
                    if scanned >= window:
                        break
                if scanned >= window:
                    break
        # No full-scan fallback by default. Falling through to Player.find on every
        # miss made each poll cost ~24s, which starved the load loop entirely.
        if deep:
            p = Player.find(self.game)
            if p:
                self.player = p
            return p
        return None

    def shift_reset(self, timeout=30) -> bool:
        """In-game soft reset: SHIFT+R. No relaunch, no focus steal.

        Roughly 9s back to the main menu versus ~60s to restart the process, which
        is most of the hunt's cycle time.
        """
        if not self.in_world():
            return True                     # already out of the world
        self.stats.status = "soft reset (shift+R)"
        self.emit("reset")
        self.input.release_alt()
        from .input import key_lparam
        self.input._post(0x0100, self.VK_LSHIFT, key_lparam(self.VK_LSHIFT))
        time.sleep(0.08)
        self.input._post(0x0100, self.VK_R, key_lparam(self.VK_R))
        time.sleep(0.12)
        self.input._post(0x0101, self.VK_R, key_lparam(self.VK_R, up=True))
        time.sleep(0.08)
        self.input._post(0x0101, self.VK_LSHIFT, key_lparam(self.VK_LSHIFT, up=True))
        # the pawn is destroyed on the way out -- cheapest possible confirmation
        return self.wait_for(lambda: not self.in_world(), timeout=timeout,
                             poll=0.3, what="soft reset")

    def ensure_in_game(self, timeout=180) -> bool:
        """Get to a loaded save from the title or the main menu.

        Gated on real UI state, not timing: an Enter sent while the game is still
        on the splash is swallowed, and pressing blind meant the save simply never
        loaded. Only ever presses while OUT of the world, so it can never walk into
        the starter dialogue and accept a Pokemon.
        """
        if self.in_world() or self.find_pawn():
            return True

        end_t = time.time() + timeout
        while time.time() < end_t and not self._stop:
            # 1. wait for the title/menu to actually exist
            t = time.time()
            self.stats.status = "waiting for title"
            self.emit("attempt")
            if not self.wait_for(lambda: self.widget_exists("W_GE_PressStart_C"),
                                 timeout=90, poll=0.4, what="title screen"):
                continue
            self.stats.phase("title", time.time() - t)

            # 2. Press Start, then 3. Continue. Note W_GE_MainMenu_C EXISTS even at
            # the Press Start screen (constructed but hidden), so it cannot be used
            # to tell the two apart -- same trap as W_GE_Pause_C. Just press both.
            t = time.time()
            self.stats.status = "press start"
            self.emit("attempt")
            self.input.tap("enter", settle=1.2)
            self.stats.status = "loading save"
            self.emit("attempt")
            self.input.tap("enter", settle=1.2)     # Continue (top item)

            # Poll the cheap bounded scan. The deep scan is a LAST resort: it walks
            # every object and costs ~23s, so firing it early meant the pawn could
            # appear a second later and go unnoticed until the deep scan finished.
            #
            # Re-press Enter periodically instead of waiting out the whole deadline.
            # An Enter sent a moment too early is simply swallowed, and when that
            # happened the loop sat idle for a full 60s before starting over -- it
            # was doubling the reload phase. Safe to repeat: this only ever runs
            # while OUT of the world, so there is no starter dialogue to walk into.
            deadline = time.time() + 60
            next_deep = time.time() + 25
            next_nudge = time.time() + 8
            while time.time() < deadline and not self._stop:
                if self.find_pawn():
                    break
                now = time.time()
                if now >= next_nudge:
                    next_nudge = now + 8
                    self.input.tap("enter", settle=0.05)
                if now >= next_deep:
                    next_deep = now + 25
                    if self.find_pawn(deep=True):
                        break
                time.sleep(0.25)
            self.stats.phase("load", time.time() - t)
            if self.player and self.player.alive():
                time.sleep(0.35)             # bag is talkable; scene wait covers the rest
                self.scene = None
                # NB: do NOT clear _clscache here. Blueprint classes are loaded
                # once per process and keep their address across save loads, but
                # re-finding one costs a full 258k-object walk (~6s) -- which was
                # being paid on every single reset.
                return True
        return False

    def attempt(self) -> bool:
        """One starter roll. Returns True if the target starter rolled shiny.

        The scene rolls its shiny flags when it loads, so the result is readable
        BEFORE the pick is confirmed. A dud is therefore abandoned without ever
        accepting a Pokemon, which is both faster and leaves the save untouched.
        """
        self.stats.status = "opening the bag"
        self.emit("attempt", {"starter": self.starter_id})

        # One Enter, then poll. Do NOT re-tap while waiting: extra Enter walks
        # the bag dialogue and can accept Treecko. The old 1.2s settle plus a
        # 9s GUObjectArray name-walk is what made the first bag open feel stuck
        # — especially after a reset, when the first scan started before the
        # suitcase actor existed and had to run again.
        self.scene = None
        self._scene_wait_t0 = time.time()
        if self.open_bag:
            self.input.tap("enter", settle=0.08)

        if not self.wait_for(self._find_scene, timeout=12, poll=0.05,
                             what="starter screen"):
            raise RuntimeError("starter screen did not open")

        if self.force_shiny:
            self.scene.force(self.starter_id)
            self.emit("attempt", {"forced": True})

        shiny = bool(self.scene.is_shiny(self.starter_id))
        self.stats.record_attempt(self.starter_id, shiny, self.scene.get("ShinyFrame"))
        self.stats.forced = self.force_shiny
        self.stats.status = "SHINY!" if shiny else "not shiny"
        self.emit("result", {"shiny": shiny, "starter": self.starter_id,
                             "scene": self.scene.snapshot()})

        if shiny:
            # Claim the TARGET. The cursor sits on the leftmost ball by default, so
            # confirming blind here hands you Treecko no matter what you asked for
            # -- that exact bug claimed a plain Treecko during a Torchic run.
            self.stats.status = "claiming " + self.starter_id
            self.emit("attempt")
            # Opening the bag drops us straight into the dialogue for whichever
            # ball is highlighted (Treecko by default), and arrow keys do nothing
            # inside a dialogue. So decline back to the ball screen FIRST, then
            # move the cursor, then accept. Skipping this claimed a plain Treecko
            # during two separate Torchic runs.
            self.leave_starter_prompt()
            self.select_starter()
            self.input.tap("enter", settle=1.1)      # ball -> "would you like"
            # Confirm until the prompt is actually gone. A single blind Enter left
            # the Yes/No dialog still open on a verified-shiny Torchic run, so the
            # claim silently did not complete.
            for _ in range(4):
                if self._stop:
                    break
                self.input.tap("enter", settle=1.2)
                if not self.choice_open():
                    break
            self.stats.status = "claimed " + self.starter_id
        return shiny

    def _levels_to_scan(self):
        """ULevels that can hold the suitcase actor: pawn outer + world persistent."""
        levels = []
        addr = self.player.addr if self.player else 0
        for _ in range(8):
            if not addr:
                break
            outer = self.gp.read_u64(addr + self.L.obj_outer)
            if not outer:
                break
            cn = self.game.class_name(outer) or ""
            if cn == "Level":
                levels.append(outer)
            elif cn == "World":
                pl = self.gp.read_u64(outer + self.L.world_persistent_level)
                if pl:
                    levels.append(pl)
            addr = outer
        for w in self.worlds or ():
            try:
                pl = self.gp.read_u64(w + self.L.world_persistent_level)
                if pl:
                    levels.append(pl)
            except Exception:
                continue
        seen = set()
        out = []
        for lv in levels:
            if lv and lv not in seen:
                seen.add(lv)
                out.append(lv)
        return out

    def _find_scene(self):
        """Locate the live starter controller.

        Prefer ULevel::Actors (hundreds of pointers, milliseconds). A full
        GUObjectArray walk with class_name() is ~9s and is how the first bag
        open of a cycle used to stall: the scan started before the actor
        existed, missed, then ran again.

        Do not replace this with a bounded newest-first GObjects scan — the
        suitcase is not in the recent slots, and that path was measured at 25s.
        """
        for level in self._levels_to_scan():
            s = StarterScene.find_in_level(self.game, self.L, level)
            if s:
                self.scene = s
                return True
        # Unknown sublevel, or the pawn outer chain did not yield a Level yet.
        # Wait a beat so a slow walk cannot hide an actor that just spawned.
        if time.time() - self._scene_wait_t0 < 1.0:
            return False
        s = StarterScene.find(self.game)
        if s:
            self.scene = s
            return True
        return False

    # ------------------------------------------------------------- soft reset
    # CALIBRATED against the EA build by driving the game and watching memory.
    # The pause menu is an icon grid that ignores posted arrow keys, so Exit and
    # Yes are reached by clicking. Coordinates are fractions of the client area so
    # they survive a resolution change; measured at 1920x1080 client.
    EXIT_XY = (194 / 1920, 910 / 1080)     # pause menu, door icon bottom-left
    YES_XY = (1102 / 1920, 626 / 1080)     # "Return to the Title Screen?" -> YES
    NO_XY = (1692 / 1920, 616 / 1080)      # starter prompt -> NO (decline)
    # the three pokeballs in front of Birch's bag, left to right. Measured from a
    # client-area capture at 1920x1080 and stored as fractions so they survive a
    # resolution change. Arrow keys do NOT move this cursor -- it is mouse driven.
    YES_XY_PROMPT = (1692 / 1920, 508 / 1080)   # starter prompt -> YES
    BALL_XY = {"treecko": (440 / 1920, 690 / 1080),
               "torchic": (960 / 1920, 690 / 1080),
               "mudkip":  (1480 / 1920, 690 / 1080)}

    def click_frac(self, fx, fy, settle=None):
        w, h = self.input.client_size()
        self.input.click(int(w * fx), int(h * fy), settle=settle)

    def widget_exists(self, class_name: str, window: int = 80000) -> bool:
        """Is a widget of this class live right now?

        Scans the newest `window` object slots, newest-first. A full 258k walk with
        name resolution costs ~7s and is unusable in a poll loop; comparing the
        class POINTER over the newest slots costs ~0.2s per 24k. The window has to
        be generous though -- title widgets are NOT in the newest few thousand, and
        an 8k window made the title never register.
        """
        target = self._class_ptr(class_name)
        if not target:
            return False
        g = self.game
        total = g.gobjects["num_elements"]
        per = 65536
        scanned = 0
        for ci in range(g.gobjects["num_chunks"] - 1, -1, -1):
            chunk = g._chunks[ci]
            n = min(per, total - ci * per)
            if n <= 0:
                continue
            items = self.gp.rpm(chunk, n * 32)
            if not items:
                continue
            for k in range(n - 1, -1, -1):
                p = struct.unpack_from("<Q", items, k * 32)[0]
                scanned += 1
                if p and self.gp.read_u64(p + 0x10) == target:
                    return True
                if scanned >= window:
                    return False
        return False

    def choice_open(self) -> bool:
        """True when the starter Yes/No buttons are on screen. Pressing Enter
        blind at this point ACCEPTS the starter -- which once cost a whole run."""
        return self.widget_exists("W_GE_ChoiceButton_C")

    def _class_ptr(self, class_name: str):
        cache = getattr(self, "_clscache", None)
        if cache is None:
            cache = self._clscache = {}
        hit = cache.get(class_name)
        if hit:
            # Verify before trusting. A Blueprint class can be collected and
            # reloaded across a save load, which moves its UClass -- and a stale
            # pointer matches no object at all, so every widget check silently
            # answered False and the title wait burned its whole 90s timeout.
            # One name read is far cheaper than the 258k-object walk it avoids.
            if (self.game.obj_name(hit) or "") == class_name:
                return hit
            cache.pop(class_name, None)
        # A miss costs a full 258k-object walk (~6s). Polling for a class that is
        # not there yet paid that on every single tick. Remember misses too, but
        # only for a few seconds, so a class that appears mid-boot is still seen.
        neg = getattr(self, "_clsmiss", None)
        if neg is None:
            neg = self._clsmiss = {}
        if time.time() - neg.get(class_name, 0) < 5.0:
            return 0
        # the shared lookup caches the UClass address and validates it, so this
        # no longer walks the whole object array on every miss
        found = self.game.class_ptr(class_name)
        if found:
            cache[class_name] = found
            neg.pop(class_name, None)
        else:
            neg[class_name] = time.time()
        return found

    # ---------------------------------------------------------- cheap signals
    # widget_exists costs ~7s (a read per object), far too slow to poll. These are
    # O(1) or near it, and were calibrated live: in-game the process holds ~257,900
    # UObjects, at the title ~240,600 -- a ~17k swing that is unmistakable.
    def object_count(self) -> int:
        return self.gp.read_u32(self.game.gobjects["addr"] + 0x10 + 0x14)

    def in_game(self) -> bool:
        """True while a live player pawn exists. Two reads, no scan."""
        return bool(self.player and self.player.alive())

    def leave_starter_prompt(self):
        """Get from the starter dialogue back to the ball-selection screen.

        This matters: `esc` does NOT open the pause menu while the starter
        dialogue is up, but it does from ball selection. Without this the reset
        silently does nothing and the hunt wedges.
        """
        if not self.choice_open():
            self.input.tap("enter", settle=0.9)   # dialogue -> Yes/No
        self.click_frac(*self.NO_XY, settle=0.5)  # select No
        self.input.tap("enter", settle=1.5)       # decline -> ball selection

    def soft_reset(self) -> bool:
        """Quit to the title screen and reload the save.

        Each step waits on an observed state change rather than a fixed sleep,
        because a fixed sleep is exactly how these loops desync and spend an hour
        mashing keys at a menu that never opened.
        """
        self.stats.status = "soft reset"
        self.emit("reset")

        in_game_count = self.object_count()

        # a dud leaves us at the starter dialogue, where esc is swallowed
        if self.scene and self.scene.alive():
            self.leave_starter_prompt()

        # pause -> Exit -> confirm prompt. These steps are fast and were verified
        # by hand, so they use settles; the expensive checks are on the transitions.
        self.input.tap("esc", settle=1.0)
        self.click_frac(*self.EXIT_XY, settle=0.6)
        self.input.tap("enter", settle=1.0)
        self.click_frac(*self.YES_XY, settle=0.6)
        self.input.tap("enter", settle=1.0)

        # leaving the world destroys the pawn -- an O(1) way to know we got out
        if not self.wait_for(lambda: not self.in_game(), timeout=25,
                             poll=0.4, what="return to title"):
            return False
        title_count = self.object_count()

        self.input.tap("enter", settle=1.4)          # Press Start -> main menu
        self.input.tap("enter", settle=2.0)          # Continue (top item) -> load

        # loading rebuilds the world: object count climbs back toward in-game level
        target = title_count + max(4000, (in_game_count - title_count) // 2)
        if not self.wait_for(lambda: self.object_count() >= target, timeout=40,
                             poll=0.5, what="save loaded"):
            return False
        time.sleep(1.5)                              # let actors finish spawning

        # the pawn is a new object after a reload; the old pointer is dangling
        self._clscache = {}
        self.player = Player.find(self.game) or self.player
        self.stats.status = "reset done"
        self.emit("reset")
        return True

    # ------------------------------------------------------------ hard reset
    # The in-game quit-to-title path does NOT work from the starter scene: `esc`
    # is swallowed there, and the W_GE_Pause_C object existing is not proof the
    # menu is visible (UMG keeps it constructed but hidden -- that false positive
    # cost several debugging cycles). Relaunching the process is the only reliable
    # way to re-roll, so that is what a hunt does.
    def hard_reset(self) -> bool:
        """Restart the game and load the save. Requires a reattach callback."""
        if not self.reattach:
            raise RuntimeError("hard_reset needs a reattach callback")
        # Refuse BEFORE killing anything. Without this check a hunter built with
        # no exe_path killed the game, then crashed on exe_path.parent -- leaving
        # the user with no game and no bot.
        if not self.exe_path or not Path(self.exe_path).exists():
            self.emit("error", {"error": "no game exe to relaunch; not killing it"})
            return False
        self.stats.status = "restarting game"
        self.emit("reset")
        try:
            import subprocess
            subprocess.run(["taskkill", "/IM", "PokemonEmerald.exe", "/F"],
                           capture_output=True)
        except Exception:
            pass
        time.sleep(3.0)
        try:
            from .versions import spawn_game, resolve_game_binary
            self.exe_path = resolve_game_binary(self.exe_path)
            spawn_game(self.exe_path)
        except Exception as e:
            self.emit("error", {"error": "relaunch failed: %r" % (e,)})
            return False

        # wait for the new process, then rebuild every handle: the PID, the window
        # and every UObject address are different now
        deadline = time.time() + 200
        while time.time() < deadline and not self._stop:
            time.sleep(2.0)
            try:
                if not self.reattach(self):
                    continue
                # attaching to a half-booted process gives a tiny object array and
                # a class table that is not populated yet; wait for it to settle
                if self.object_count() > 150000:
                    break
            except Exception:
                continue
        else:
            return False

        self.scene = None
        self._clscache = {}
        return self.ensure_in_game()

    def run(self, max_attempts=0, rate=None):
        self._stop = False
        self.stats = HuntStats(target=self.starter_id)
        if rate is not None:
            self.set_rate(rate)
        self.refresh_stats()
        self.emit("start")
        if not self.ensure_in_game():
            self.stats.status = "no save loaded"
            self.stats.error = "could not reach the overworld"
            self.emit("error", {"error": self.stats.error})
            return self.stats
        try:
            while not self._stop:
                cycle_t0 = time.time()
                if self.attempt():
                    self.stats.found = True
                    self.stats.found_at = self.stats.attempts
                    self.stats.status = "found shiny"
                    self.emit("found")
                    return self.stats
                if max_attempts and self.stats.attempts >= max_attempts:
                    self.stats.status = "gave up"
                    self.emit("done")
                    return self.stats
                self.stats.phase("roll", time.time() - cycle_t0)
                # SHIFT+R first: ~9s versus ~60s for a relaunch. Relaunch stays as
                # the recovery path for when the soft reset does not take.
                t = time.time()
                ok = self.shift_reset()
                self.stats.phase("reset", time.time() - t)
                t = time.time()
                ok = ok and self.ensure_in_game()
                self.stats.phase("reload", time.time() - t)
                self.stats.record_cycle(time.time() - cycle_t0)
                self.emit("cycle")
                if not ok and self.reattach:
                    ok = self.hard_reset()
                if not ok:
                    self.stats.status = "reset failed"
                    self.stats.error = "could not get back to a fresh roll"
                    self.emit("error", {"error": self.stats.error})
                    return self.stats
        except Exception as e:
            self.stats.error = repr(e)
            self.stats.status = "error"
            self.emit("error", {"error": repr(e)})
        return self.stats
