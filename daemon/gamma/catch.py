"""Force the next ball to catch by writing the fields the catch code already uses.

PokeballCatchingLibrary.cpp logs `Automatic catch (a=%d >= 255)` and
`shakes=%d/4, caught=%s`. The names below are the ones in that library:
OutCatchResult, CatchResult, BaseCatchRate, BallModifier. They are looked
up through UE reflection, same as the party reader, so nothing is hardcoded
by offset and nothing executable is patched.

While the switch is on, a background loop writes those fields on the live
battle objects. The original bytes are put back when it is turned off.
"""
from __future__ import annotations

import struct
import threading

from .wild import BATTLE_MANAGER, BM_ENEMY_MON, MON_SPECIES_DATA, POKEMON_ACTOR

BOOL_FIELDS = ("CatchResult", "OutCatchResult", "bCaught")
INT_FIELDS = ("CatchRate", "BaseCatchRate", "BallModifier", "CatchRateModifier")
WATCH_CLASSES = (BATTLE_MANAGER, POKEMON_ACTOR, "BP_PokeballPARENT_C")
SUPER_STRUCT = 0x40
INT_VALUE = 255


class CatchHook:
    def __init__(self, gp, game):
        self.gp = gp
        self.game = game
        self.enabled = False
        self.sites: list[dict] = []
        self._saved: dict[tuple[int, int], bytes] = {}
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self._writes = 0

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "sites": list(self.sites),
            "writes": self._writes,
        }

    def set(self, enabled: bool = True) -> dict:
        if not enabled:
            return self.clear()
        if self.enabled:
            return self.status()
        self._stop.clear()
        self.enabled = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self.status()

    def clear(self) -> dict:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=2.0)
        self._thread = None
        with self._lock:
            for addr, original in self._saved.items():
                try:
                    self.gp.wpm(addr[0], original)
                except Exception:
                    pass
            self._saved = {}
        self.enabled = False
        self.sites = []
        return self.status()

    def _loop(self):
        while not self._stop.wait(0.15):
            try:
                self._tick()
            except Exception:
                pass

    def _tick(self):
        if self.game is None:
            return
        seen = []
        for cname in WATCH_CLASSES:
            try:
                objs = self.game.actors_of_class(cname)
            except Exception:
                objs = []
            for obj in objs:
                names = self._paint(obj)
                if names:
                    seen.append({"name": cname, "addr": hex(obj), "fields": names})
        species = self._enemy_species()
        if species:
            names = self._paint(species)
            if names:
                seen.append({"name": "SpeciesData", "addr": hex(species),
                             "fields": names})
        if seen:
            self.sites = seen[:12]

    def _enemy_species(self) -> int:
        try:
            managers = self.game.actors_of_class(BATTLE_MANAGER)
        except Exception:
            return 0
        for mgr in managers:
            mon = self.gp.read_u64(mgr + BM_ENEMY_MON)
            if not mon:
                continue
            species = self.gp.read_u64(mon + MON_SPECIES_DATA)
            if species:
                return species
        return 0

    def _paint(self, obj: int) -> list[str]:
        wrote = []
        for name, typ, off in self._props(obj):
            if name in BOOL_FIELDS and typ == "BoolProperty":
                data = b"\x01"
            elif name in INT_FIELDS and typ == "IntProperty":
                data = struct.pack("<i", INT_VALUE)
            elif name in INT_FIELDS and typ == "ByteProperty":
                data = bytes([INT_VALUE])
            else:
                continue
            if self._write(obj + off, data):
                wrote.append(name)
        if wrote:
            self._writes += 1
        return wrote

    def _write(self, addr: int, data: bytes) -> bool:
        key = (addr, len(data))
        with self._lock:
            if key not in self._saved:
                original = self.gp.rpm(addr, len(data))
                if not original or len(original) != len(data) or original == data:
                    return False
                self._saved[key] = original
            return self.gp.wpm(addr, data) == len(data)

    def _props(self, obj: int) -> list[tuple[str, str, int]]:
        cls = self.gp.read_u64(obj + 0x10)
        if not cls:
            return []
        out = []
        seen: set[int] = set()
        cur = cls
        for _ in range(8):
            if not cur or cur in seen:
                break
            seen.add(cur)
            try:
                for prop in self.game.class_properties(cur):
                    name = prop.get("name") or ""
                    if name in BOOL_FIELDS or name in INT_FIELDS:
                        out.append((name, prop.get("type") or "",
                                    int(prop.get("offset") or 0)))
            except Exception:
                pass
            cur = self.gp.read_u64(cur + SUPER_STRUCT)
        return out
