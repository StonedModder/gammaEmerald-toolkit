"""Wild-encounter shiny hunting: walk into grass, read the roll, run if it is a dud.

THE WILD SHINY MECHANISM (offsets read from the game's own reflection data, not
copied from anywhere):

  BP_BattleManager_C                one persistent instance, alive out of battle
      isWildPokemonSpawn?   +0x37C  bool   a wild spawn is being set up
      WildPokemonLevel      +0x500  int
      isShiny?              +0x528  bool   THE wild shiny result
  BP_GE_Wild_Overworld_Pokemon_C    exists only while an encounter is up
      isShiny?              +0x828  bool
      Level                 +0x898  int
  BP_BattlePlayerGAMMA_C
      isShinyEncounter?     +0x475  bool

Baseline verified with no battle running: the manager exists with
isWildPokemonSpawn?=False, WildPokemonLevel=0, isShiny?=False, and there are
zero wild-overworld actors. So "an encounter is up" is a real state change, and
the manager's fixed address makes it cheap to poll -- the same shape as the
starter scene in hunt.py.

WHAT IS AND IS NOT VERIFIED
  Verified live: the offsets above, the baseline values, a write round-trip on
  isShiny? (False -> True -> False), grass-tile discovery, walking to grass, and
  pacing.
  NOT verified: the encounter/flee half. The save this was built against sits
  before the starter is chosen, so the game refuses to generate wild battles at
  all -- pacing in grass produces nothing no matter how long it runs. Everything
  below the encounter detection is written from the reflection data and is
  unproven until it runs on a save that is far enough in.

MAP TELEPORT, a dead end recorded so it is not re-tried:
  BP_GE_TeleportVolume_C exposes LevelToLoad (+0x330) as an FSoftObjectPath --
  package FName at +0x338, asset FName at +0x340 -- and rewriting those two
  FNames does change what the field reads back as. It does NOT change where the
  volume sends you: the soft reference is already resolved by the time the
  volume runs, so the game still loads the original destination.
  What DOES work: writing the volume's Brendan pointer (+0x2E8), setting
  isOverlapping? (+0x2F0) and stepping makes the game perform its own level
  transition on demand. Useful, but it only ever goes where that volume already
  went, so it is not arbitrary map travel and is not exposed as such.
"""
from __future__ import annotations

import struct
import time
from dataclasses import dataclass, field, asdict

from . import nav

ROUTE_DATA = "RouteData"
# RouteData, from reflection. GrassEncounters is a TArray<WildEncounterSlot>,
# and WildEncounterSlot is {FSoftObjectPath Species; int32 Min; int32 Max;
# int32 Weight} on a 0x38 stride -- the soft path puts the asset FName at +0x10.
ROUTE_OFFSETS = {"RouteName": 0x30, "EncounterRate": 0x74, "GrassEncounters": 0x78}
SLOT_STRIDE = 0x38
SLOT_ASSET_FNAME = 0x10
SLOT_MIN, SLOT_MAX, SLOT_WEIGHT = 0x28, 0x2C, 0x30

# The per-Pokemon state lives on the `Pokemon` PARENT class, not on the
# Blueprint -- BP_PokemonMaster_C only adds battle presentation on top, which is
# why reflecting the Blueprint alone found nothing. VERIFIED live on a real
# Route 101 encounter: DA_Wurmple, nature Gentle, IVs 11/5/14/28/12/19.
POKEMON_ACTOR = "BP_PokemonMaster_C"
MON_SPECIES_DATA = 0x2B8   # -> PokemonSpeciesData object, its name is DA_<Species>
MON_IS_SHINY = 0x384
MON_NATURE = 0x3B8
MON_IVS = 0x3F8            # PokemonIVs: HP, Atk, Def, SpA, SpD, Spe (int32 each)

IV_NAMES = ("HP", "Atk", "Def", "SpA", "SpD", "Spe")
NATURES = ("Hardy", "Lonely", "Brave", "Adamant", "Naughty", "Bold", "Docile",
           "Relaxed", "Impish", "Lax", "Timid", "Hasty", "Serious", "Jolly",
           "Naive", "Modest", "Mild", "Quiet", "Bashful", "Rash", "Calm",
           "Gentle", "Sassy", "Careful", "Quirky")

BATTLE_MANAGER = "BP_BattleManager_C"
# The manager's two combatant pointers, verified during a live forced encounter:
# the player's active Pokemon, then the opponent. These are what identify the
# WILD Pokemon -- scanning for Pokemon actors also returns stale ones.
BM_PLAYER_MON = 0x2B8
BM_ENEMY_MON = 0x2C0
WILD_ACTOR = "BP_GE_Wild_Overworld_Pokemon_C"
BATTLE_PLAYER = "BP_BattlePlayerGAMMA_C"

MANAGER_OFFSETS = {
    "isWildPokemonSpawn?": (0x37C, "b"),
    "WildPokemonLevel": (0x500, "i"),
    "isShiny?": (0x528, "b"),
}
WILD_OFFSETS = {"isShiny?": (0x828, "b"), "Level": (0x898, "i")}

# Widgets the game puts up for a wild battle. Presence of the transition widget
# is the earliest sign an encounter has started.
BATTLE_WIDGETS = ("W_TwistTile_TransitionWildGrass_C", "W_GE_BattleHUD_C",
                  "W_BattleHUD_C", "W_BattleActions_C")


def _read(gp, addr, off, kind):
    raw = gp.rpm(addr + off, 4 if kind == "i" else 1)
    if not raw:
        return None
    return struct.unpack("<i", raw)[0] if kind == "i" else bool(raw[0])


def loaded_worlds(game):
    """Names of the levels currently loaded.

    Called on every hunt step (via in_battle), so it uses the pointer-compare
    scan -- the name-resolving version cost seconds per call and made each step
    of the hunt crawl.
    """
    gp = game.gp
    return {(game.obj_name(o) or "") for o in game.actors_of_class("World")
            if gp.read_u64(o + 0x30)}


def battle_map_names(lib):
    """Every battle arena the game ships, read from the pak.

    A battle swaps in one of these levels. Which one depends on the route --
    Route 101 uses MAP_WoodsBattle, the desert uses MAP_DesertBattle, and some
    are not even named "Battle" (MAP_Cemetery) -- so this is taken from the
    MAPS/BATTLEMAPS folder rather than pattern-matched on the name.
    """
    if lib is None:
        return set()
    return {p.rsplit("/", 1)[-1][:-len(".umap")] for p in lib.paths()
            if "/MAPS/BATTLEMAPS/" in p and p.lower().endswith(".umap")}


def in_battle(game, battle_maps) -> bool:
    """True while a battle arena is loaded.

    VERIFIED live: out of battle the loaded worlds were
    [MAP_Hoenn_Persistant, MAP_LittleRoot, MAP_Route101]; during a wild battle
    MAP_WoodsBattle was added. BP_BattleManager_C's own fields stayed False/0
    throughout, so they are NOT a battle signal -- that mistake made the hunter
    pace through a live encounter reporting "no encounter".
    """
    return bool(battle_maps and (loaded_worlds(game) & battle_maps))


def route_table(game, route_obj):
    """[(species, min_lvl, max_lvl, weight)] for a route's grass, or []."""
    gp = game.gp
    raw = gp.rpm(route_obj + ROUTE_OFFSETS["GrassEncounters"], 16)
    if not raw:
        return []
    data, num, _cap = struct.unpack("<QII", raw)
    if not data or not (0 < num < 64):
        return []
    blob = gp.rpm(data, num * SLOT_STRIDE)
    if not blob:
        return []
    out = []
    for k in range(num):
        e = blob[k * SLOT_STRIDE:(k + 1) * SLOT_STRIDE]
        try:
            species = game.resolve_name(*struct.unpack_from("<II", e, SLOT_ASSET_FNAME))
        except Exception:
            species = "?"
        mn, mx, wt = struct.unpack_from("<iii", e, SLOT_MIN)
        out.append({"species": species.removeprefix("DA_"), "min": mn,
                    "max": mx, "weight": wt})
    return out


def routes(game):
    """Every loaded RouteData, by name."""
    out = {}
    for o in game.actors_of_class(ROUTE_DATA):
        out[game.obj_name(o) or ""] = o
    return out


def route_for_world(game, world_names):
    """Match the loaded overworld level to its RouteData.

    MAP_Route101 -> Route_101; the two sides name things differently, so the
    comparison is on the alphanumerics that survive both.

    Several levels are loaded at once (the persistent world, the town AND the
    route), so a first-match wins picks whichever came out of the object array
    first -- that returned Route_LittleRoot, whose grass table is empty, while
    standing in Route 101's grass. A route that actually HAS encounters wins.
    """
    known = routes(game)

    def norm(x):
        return "".join(c for c in x.lower() if c.isalnum())

    matches = []
    for w in world_names:
        wn = norm(w).removeprefix("map")
        for nm, o in known.items():
            if norm(nm).removeprefix("route") == wn.removeprefix("route"):
                matches.append((nm, o))
    if not matches:
        return None, None
    for nm, o in matches:
        if route_table(game, o):
            return nm, o
    return matches[0]


def actor_world(game, actor: int) -> str:
    """Name of the streamed level an actor belongs to.

    Outer(actor) is the ULevel ("PersistentLevel"), and Outer(level) is the
    world -- MAP_Route115, MAP_PetalburgWoods and so on. The player pawn is no
    use for this: it lives in MAP_Hoenn_Persistant wherever you walk.
    """
    gp = game.gp
    level = gp.read_u64(actor + 0x20)
    world = gp.read_u64(level + 0x20) if level else 0
    return (game.obj_name(world) or "") if world else ""


ROUTE_SUBSYSTEM = "RouteSubsystem"
CURRENT_ROUTE = 0x50            # RouteSubsystem::CurrentRoute (UObject*)


def current_route(game):
    """The route the GAME says the player is on, or (None, None).

    This is the whole answer, and it is why the panel used to lag an area
    behind. Matching loaded levels cannot work: walking from Route 103 to
    Meteor Falls leaves five worlds resident -- the persistent one, the route
    you left, the one you arrived in, and two neighbours -- so first-match-wins
    kept reporting Route_103. The grass tiles nearest the player are a decent
    second opinion, but a cave with no grass has none to give.
    """
    gp = game.gp
    for sub in game.actors_of_class(ROUTE_SUBSYSTEM):
        obj = gp.read_u64(sub + CURRENT_ROUTE)
        if obj:
            name = game.obj_name(obj)
            if name:
                return name, obj
    return None, None


def _same_place(route_name: str, world_names) -> bool:
    """Route_115 belongs to MAP_Route115, allowing for the naming differences."""
    def norm(x):
        return "".join(c for c in x.lower() if c.isalnum()).removeprefix("map")
    want = norm(route_name).removeprefix("route")
    return any(norm(w).removeprefix("route") == want for w in world_names)


def route_for_position(game, pos, world_names, max_distance=12000.0):
    """Where the player is: the game's own answer, then grass, then levels."""
    from . import nav

    name, obj = current_route(game)
    # The game keeps the last route when you walk somewhere that has none --
    # Meteor Falls has no RouteData at all -- and reporting Route 115's grass
    # while standing in a cave offers species you cannot catch there. Trust it
    # only while that route's own map is still loaded.
    if obj and name and _same_place(name, world_names):
        return name, obj

    if pos:
        nearest_d, nearest_world = None, ""
        for actor, tile_pos in nav.grass_tiles(game):
            d = nav.dist2d(pos, tile_pos)
            if nearest_d is None or d < nearest_d:
                nearest_d, nearest_world = d, actor_world(game, actor)
        if nearest_world and nearest_d is not None and nearest_d <= max_distance:
            name, obj = route_for_world(game, [nearest_world])
            if obj:
                return name, obj
    return route_for_world(game, world_names)


def read_mon(game, actor: int):
    """Species, shiny, nature and IVs of one battle Pokemon."""
    gp = game.gp
    nat = gp.rpm(actor + MON_NATURE, 1)
    sh = gp.rpm(actor + MON_IS_SHINY, 1)
    iv = gp.rpm(actor + MON_IVS, 24)
    if not (nat and sh and iv):
        return None
    sd = gp.read_u64(actor + MON_SPECIES_DATA)
    species = (game.obj_name(sd) or "?") if sd else "?"
    return {
        "addr": hex(actor),
        "species": species.removeprefix("DA_"),
        "shiny": bool(sh[0]),
        "nature": NATURES[nat[0]] if nat[0] < len(NATURES) else nat[0],
        "ivs": dict(zip(IV_NAMES, struct.unpack("<6i", iv))),
    }


def battle_mons(game):
    """Every Pokemon actor currently in a battle."""
    out = []
    for o in game.actors_of_class(POKEMON_ACTOR):
        m = read_mon(game, o)
        if m:
            out.append(m)
    return out


class BattleManager:
    """The persistent wild-battle manager."""

    def __init__(self, game, addr):
        self.game = game
        self.gp = game.gp
        self.addr = addr

    @classmethod
    def find(cls, game):
        found = game.actors_of_class(BATTLE_MANAGER, limit=1)
        return cls(game, found[0]) if found else None

    def alive(self) -> bool:
        try:
            return (self.game.class_name(self.addr) or "") == BATTLE_MANAGER
        except Exception:
            return False

    def get(self, name):
        off, kind = MANAGER_OFFSETS[name]
        return _read(self.gp, self.addr, off, kind)

    def set(self, name, value) -> bool:
        off, kind = MANAGER_OFFSETS[name]
        blob = struct.pack("<i", int(value)) if kind == "i" else bytes([1 if value else 0])
        return self.gp.wpm(self.addr + off, blob) == len(blob)

    def snapshot(self) -> dict:
        d = {k: self.get(k) for k in MANAGER_OFFSETS}
        d["addr"] = hex(self.addr)
        return d

    def wild_up(self) -> bool:
        return bool(self.get("isWildPokemonSpawn?"))

    def is_shiny(self) -> bool:
        return bool(self.get("isShiny?"))

    def force_shiny(self) -> bool:
        """Write the shiny flag, to prove the loop without waiting on luck."""
        return self.set("isShiny?", True)


@dataclass
class WildStats:
    encounters: int = 0
    shinies: int = 0
    started: float = field(default_factory=time.time)
    status: str = "idle"
    found: bool = False
    found_at: int | None = None
    last_level: int | None = None
    error: str | None = None
    forced: bool = False
    steps: int = 0
    last_mon: dict | None = None
    seen: dict = field(default_factory=dict)   # species -> how many times
    encounter_secs: list = field(default_factory=list)

    @property
    def elapsed(self) -> float:
        return time.time() - self.started

    @property
    def per_hour(self) -> float:
        if self.encounter_secs:
            avg = sum(self.encounter_secs) / len(self.encounter_secs)
            return 3600.0 / avg if avg > 0 else 0.0
        e = self.elapsed
        return (self.encounters / e * 3600.0) if e > 1 else 0.0

    def as_dict(self) -> dict:
        d = asdict(self)
        d["elapsed"] = round(self.elapsed, 1)
        d["per_hour"] = round(self.per_hour, 1)
        d["avg_encounter"] = round(
            sum(self.encounter_secs) / len(self.encounter_secs), 1
        ) if self.encounter_secs else 0.0
        return d


class WildHunter:
    """Find grass, pace in it, and read the roll on every encounter."""

    def __init__(self, game, layout, gi, pawn_addr=None, on_event=None,
                 force_shiny=False, battle_maps=None, route=None, filters=None):
        self.game = game
        self.gp = game.gp
        self.layout = layout
        self.input = gi
        self.pawn = pawn_addr
        self.on_event = on_event
        self.force_shiny = force_shiny
        self.stats = WildStats()
        self.battle_maps = battle_maps or set()
        self.route = route            # RouteData for the area, for the species list
        self.filters = filters or {}  # {"nature": "Adamant", "min_ivs": {...}}
        self.manager = None
        self.walker = None
        self.tiles = []
        self._stop = False

    # ------------------------------------------------------------------ util
    def emit(self, kind, data=None):
        if not self.on_event:
            return
        ev = {"kind": kind, "stats": self.stats.as_dict()}
        if data:
            ev.update(data)
        self.on_event(ev)

    def stop(self):
        self._stop = True

    def ensure_manager(self):
        if self.manager and self.manager.alive():
            return self.manager
        self.manager = BattleManager.find(self.game)
        return self.manager

    def wild_actors(self):
        return nav.actors_of_class(self.game, WILD_ACTOR, limit=8)

    def in_encounter(self) -> bool:
        """True while a battle is on screen.

        VERIFIED live, and this replaced a detector that was simply wrong:
        BP_BattleManager_C's isWildPokemonSpawn?/isShiny? stayed False right
        through a real wild battle, so the hunter paced inside an encounter
        reporting "no encounter". A battle swaps in one of the arenas from
        MAPS/BATTLEMAPS, and that IS observable.
        """
        return in_battle(self.game, self.battle_maps)

    def encounter_mon(self, wait: float = 4.0):
        """The WILD Pokemon in the current battle, or None if it cannot be read.

        The battle manager points straight at both combatants -- the player's
        active Pokemon and the opponent -- so the opponent is read rather than
        guessed. The pointer is filled in slightly after the battle map loads,
        hence the short poll.

        There is deliberately NO fallback to scanning for Pokemon actors. Actors
        from earlier battles stay in the object array, so a scan returns several
        and any rule for picking among them eventually picks a dead one: the old
        rule ("the one in this route's encounter table") returned a STALE
        Poochyena while a forced Beldum was live on screen, and reported it as
        not shiny. Guessing wrong here loses a shiny; returning None does not.
        """
        deadline = time.time() + max(0.0, wait)
        while True:
            mgr = self.manager_addr()
            if mgr:
                enemy = self.gp.read_u64(mgr + BM_ENEMY_MON)
                if enemy and self.game.class_name(enemy) == POKEMON_ACTOR:
                    mon = read_mon(self.game, enemy)
                    if mon:
                        return mon
            if time.time() >= deadline:
                return None
            time.sleep(0.15)
            self.game.refresh()

    def manager_addr(self):
        found = self.game.actors_of_class(BATTLE_MANAGER, limit=1)
        return found[0] if found else 0

    def encounter_shiny(self):
        m = self.encounter_mon()
        return m["shiny"] if m else None

    def encounter_level(self):
        mgr = self.ensure_manager()
        lvl = mgr.get("WildPokemonLevel") if mgr else None
        return lvl or None

    # -------------------------------------------------------------- movement
    def refresh_tiles(self):
        self.tiles = nav.grass_tiles(self.game)
        return self.tiles

    def goto_grass(self, timeout: float = 120.0) -> bool:
        """Walk to the nearest patch of grass. True once standing on one."""
        if self.walker is None:
            self.walker = nav.Walker(self.gp, self.game, self.input, self.pawn)
        pos = self.walker.where()
        if pos is None:
            self.stats.error = "no player position"
            return False
        if not self.tiles:
            self.refresh_tiles()
        if not self.tiles:
            self.stats.error = "no grass in this level"
            self.stats.status = "no grass here"
            self.emit("error", {"error": self.stats.error})
            return False
        if nav.on_grass(pos, self.tiles):
            self.stats.status = "in the grass"
            return True

        target = nav.nearest(pos, self.tiles)
        self.stats.status = "walking to grass"
        self.emit("walk", {"target": [round(v) for v in target[1]],
                           "distance": round(nav.dist2d(pos, target[1]))})
        ok = self.walker.walk_to(target[1])
        pos = self.walker.where()
        if ok or (pos and nav.on_grass(pos, self.tiles)):
            self.stats.status = "in the grass"
            return True
        self.stats.error = "could not reach grass"
        self.emit("error", {"error": self.stats.error})
        return False

    def wanted(self, mon: dict | None, level: int | None = None) -> bool:
        """Does this encounter match the optional filters?

        Filters are opt-in: with none set every encounter counts, which is the
        plain shiny hunt. Species, nature and per-stat IV minimums all read off
        the live Pokemon actor -- VERIFIED against a real encounter.
        """
        f = self.filters or {}
        if not f:
            return True
        if mon is None:
            return False

        want_species = f.get("species")
        if want_species:
            wants = {str(w).lower().removeprefix("da_") for w in want_species}
            if mon["species"].lower() not in wants:
                return False

        want_nature = f.get("nature")
        if want_nature:
            wants = {str(n).lower() for n in
                     (want_nature if isinstance(want_nature, (list, tuple, set))
                      else [want_nature])}
            if str(mon["nature"]).lower() not in wants:
                return False

        if f.get("shiny_only") and not mon["shiny"]:
            return False

        for stat, minimum in (f.get("min_ivs") or {}).items():
            key = stat if stat in mon["ivs"] else stat.title()
            if mon["ivs"].get(key, -1) < int(minimum):
                return False

        total = f.get("min_iv_total")
        if total and sum(mon["ivs"].values()) < int(total):
            return False

        lo, hi = f.get("min_level"), f.get("max_level")
        if level is not None:
            if lo and level < lo:
                return False
            if hi and level > hi:
                return False
        return True

    # ------------------------------------------------------------------ hunt
    def flee(self, timeout: float = 150.0) -> bool:
        """End the encounter.

        VERIFIED live: pressing Enter repeatedly ends a wild battle (20 presses,
        ~93s including the attack and faint animations). Down-then-Enter was
        tried first and was unreliable -- it fled once and timed out twice --
        so this uses the pattern that actually worked, gated on the arena
        unloading rather than a press count.
        """
        self.stats.status = "ending the encounter"
        self.emit("flee")
        end = time.time() + timeout
        while time.time() < end and not self._stop:
            self.input.tap("enter", settle=0.7)
            self.game.refresh()
            if not self.in_encounter():
                return True
        return not self.in_encounter()

    def available_species(self, pos=None):
        """What can appear in the grass here, straight from the route's table.

        `pos` (the player) picks the route by where they stand; without it this
        falls back to whichever loaded level matches first, which goes wrong as
        soon as more than one route is streamed in.
        """
        name, obj = route_for_position(self.game, pos, loaded_worlds(self.game))
        if not obj:
            return {"route": None, "encounters": []}
        rate = struct.unpack("<f", self.gp.rpm(obj + ROUTE_OFFSETS["EncounterRate"], 4))[0]
        return {"route": name, "rate": round(rate, 1),
                "encounters": route_table(self.game, obj)}


    def attempt(self, pace_timeout: float = 90.0) -> bool | None:
        """One encounter. True if shiny, False if a dud, None if none appeared."""
        if not self.goto_grass():
            return None

        self.stats.status = "pacing for an encounter"
        self.emit("pace")
        t0 = time.time()
        got = self.walker.pace(until=self.in_encounter, timeout=pace_timeout)
        if not got:
            self.stats.status = "no encounter"
            self.emit("idle", {"waited": round(time.time() - t0)})
            return None

        # let the encounter finish being set up before reading the roll
        time.sleep(0.6)
        if self.force_shiny:
            m = self.ensure_manager()
            if m:
                m.force_shiny()
                self.emit("attempt", {"forced": True})

        mon = self.encounter_mon()
        shiny = bool(mon and mon["shiny"])
        lvl = self.encounter_level()
        self.stats.encounters += 1
        self.stats.last_level = lvl
        self.stats.last_mon = mon
        if mon:
            self.stats.seen[mon["species"]] = self.stats.seen.get(mon["species"], 0) + 1
        self.stats.encounter_secs.append(time.time() - t0)
        if len(self.stats.encounter_secs) > 50:
            self.stats.encounter_secs.pop(0)

        # A hunt stops on a shiny; filters narrow WHICH shiny (or which mon)
        # counts, so a plain shiny hunt sets none and keeps every one.
        keep = shiny and self.wanted(mon, lvl)
        # An unreadable encounter is NOT the same as a dud -- say so rather than
        # letting it look like a confirmed non-shiny.
        self.stats.status = ("SHINY!" if keep
                             else "shiny, filtered out" if shiny
                             else "not shiny" if mon
                             else "could not read the encounter")
        self.emit("result", {"shiny": shiny, "level": lvl, "mon": mon,
                             "matched": keep})

        if keep:
            self.stats.shinies += 1
            self.stats.found = True
            self.stats.found_at = self.stats.encounters
            self.emit("found")
            return True
        self.flee()
        return False

    def run(self, max_encounters: int = 0):
        self._stop = False
        self.stats = WildStats()
        self.stats.forced = self.force_shiny
        self.emit("start")
        try:
            while not self._stop:
                r = self.attempt()
                if r is True:
                    self.stats.status = "found a shiny"
                    return self.stats
                if r is None and self.stats.error:
                    return self.stats
                if max_encounters and self.stats.encounters >= max_encounters:
                    self.stats.status = "gave up"
                    self.emit("done")
                    return self.stats
        except Exception as e:
            self.stats.error = repr(e)
            self.stats.status = "error"
            self.emit("error", {"error": repr(e)})
        return self.stats
