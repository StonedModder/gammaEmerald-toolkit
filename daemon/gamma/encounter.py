"""Force the next wild encounter to a regional dex number, optionally shiny.

Ported from cheatExamples/force_shiny_encounter.py onto the attached GameProcess.
RVAs are for the tested EA Win64 build; install() refuses to write if the original
instruction bytes do not match.
"""
from __future__ import annotations

import json
import os
import struct
from pathlib import Path

from .memory import (
    MEM_COMMIT, PAGE_GUARD, READABLE, GameProcess, MEMORY_BASIC_INFORMATION,
    VirtualQueryEx,
)
from ctypes import wintypes
import ctypes


HOOK_OFF = 0x0A65AEC5
INIT_OFF = 0x0A66D300
GET_BY_DEX_OFF = 0x0A6674B0
SHINY_ROLL_OFF = 0x0A65AF19
SHINY_RAND_OFF = 0x0A66D61B
SPECIES_DB_VTABLE_OFF = 0x126B3E80
SPECIES_DB_READY_OFF = 0x250

HOOK_ORIG = bytes.fromhex("E8 36 24 01 00")
SHINY_ROLL_ORIG = bytes.fromhex("88 83 64 01 00 00")
SHINY_RAND_ORIG = bytes.fromhex("41 88 87 64 01 00 00")

MAX_REGIONAL_DEX = 448
MAP_STRIDE = 0x30
CHUNK = 8 * 1024 * 1024

KNOWN_DEX = {
    "treecko": 1,
    "torchic": 4,
    "mudkip": 7,
    "poochyena": 10,
    "taillow": 25,
    "ralts": 29,
    "alakazam": 42,
    "beldum": 199,
    "mew": 300,
}

CACHE_PATH = (
    Path(os.environ.get("LOCALAPPDATA", Path.home()))
    / "GammaToolkit" / "species_db.txt"
)
# name -> regional dex, learned from the running game and kept between sessions:
# the table only exists once the game has loaded species, so without this the
# numbers would be unavailable every time the app is opened at the title screen.
SPECIES_CACHE = CACHE_PATH.with_name("species_dex.json")


class DatabaseNotLoadedError(RuntimeError):
    pass


class DexNotFoundError(RuntimeError):
    pass


def parse_dex(text) -> int:
    value = str(text).strip()
    try:
        dex = int(value, 10) if value.isdecimal() else int(value, 0)
    except ValueError as exc:
        raise ValueError(f"invalid dex number {text!r}") from exc
    if not 1 <= dex <= MAX_REGIONAL_DEX:
        raise ValueError(f"dex number must be between 1 and {MAX_REGIONAL_DEX}")
    return dex


def resolve_pokemon(name_or_dex: str, explicit_dex=None) -> tuple[str, int]:
    label = str(name_or_dex or "").strip()
    if explicit_dex is not None and str(explicit_dex).strip() != "":
        return label or str(explicit_dex), parse_dex(explicit_dex)
    key = label.lower()
    if key in KNOWN_DEX:
        return label, KNOWN_DEX[key]
    return label, parse_dex(label)


def relative(source_after_instruction: int, target: int) -> bytes:
    distance = target - source_after_instruction
    if not -(1 << 31) <= distance < (1 << 31):
        raise RuntimeError("code cave is outside rel32 range")
    return struct.pack("<i", distance)


class EncounterHook:
    """Install / clear the wild-encounter species (and optional shiny) patch."""

    def __init__(self, gp: GameProcess):
        self.gp = gp
        self.enabled = False
        self.dex = None
        self.label = ""
        self.shiny = False
        self.cave = 0

    def _read(self, addr, n) -> bytes:
        return self.gp.rpm(addr, n) or b""

    @property
    def base(self) -> int:
        return self.gp.module_base

    def status(self) -> dict:
        hooked = self._hooked()
        self.enabled = hooked
        return {
            "enabled": hooked,
            "dex": self.dex,
            "label": self.label,
            "shiny": self.shiny,
            "cave": hex(self.cave) if self.cave else None,
            "known": [{"id": k, "dex": v} for k, v in KNOWN_DEX.items()],
            "max_dex": MAX_REGIONAL_DEX,
        }

    def _hooked(self) -> bool:
        cur = self._read(self.base + HOOK_OFF, len(HOOK_ORIG))
        return bool(cur) and cur[0] == 0xE9

    def set(self, dex: int, shiny: bool = False, label: str = "") -> dict:
        dex = parse_dex(dex)
        if self._hooked():
            self.clear()
        owner, data, count, index = self.find_database(dex, allow_uninitialized=True)
        cave = self.install(owner, dex, shiny)
        self.enabled = True
        self.dex = dex
        self.label = label or str(dex)
        self.shiny = bool(shiny)
        self.cave = cave
        return {
            "enabled": True,
            "dex": dex,
            "label": self.label,
            "shiny": self.shiny,
            "owner": hex(owner),
            "entries": count,
            "dex_index": index,
            "cave": hex(cave),
        }

    def clear(self) -> dict:
        patches = (
            (self.base + HOOK_OFF, HOOK_ORIG, bytes.fromhex("48 83 EC 50")),
            (self.base + SHINY_ROLL_OFF, SHINY_ROLL_ORIG, bytes.fromhex("B0 01")),
            (self.base + SHINY_RAND_OFF, SHINY_RAND_ORIG, bytes.fromhex("B0 01")),
        )
        restored = 0
        for address, original, signature in patches:
            current = self._read(address, len(original))
            if current == original:
                continue
            if len(current) < 5 or current[0] != 0xE9:
                raise RuntimeError(f"unknown patch at {address:#x}; refusing to overwrite it")
            target = address + 5 + struct.unpack("<i", current[1:5])[0]
            if self._read(target, len(signature)) != signature:
                raise RuntimeError(
                    f"jump at {address:#x} is not this tool's hook; refusing to clear it")
            self.gp.write_code(address, original)
            restored += 1
        self.enabled = False
        self.cave = 0
        return {"enabled": False, "restored": restored}

    # ---------------------------------------------------------- species DB
    def find_database(self, target_dex: int, allow_uninitialized: bool = False):
        cached = self._load_database_cache(target_dex, allow_uninitialized)
        if cached:
            if cached[3] < 0:
                raise DexNotFoundError(
                    f"species database is loaded, but regional dex {target_dex} is absent")
            return cached
        expected_vtable = self.base + SPECIES_DB_VTABLE_OFF
        pattern = struct.pack("<Q", expected_vtable)
        candidates = []
        owners = []
        for region_base, region_size in self.gp.writable_private_regions():
            offset = 0
            while offset < region_size:
                count = min(CHUNK, region_size - offset)
                block = self._read(region_base + offset, count)
                if len(block) < 8:
                    break
                position = 0
                while True:
                    position = block.find(pattern, position)
                    if position < 0:
                        break
                    owner = region_base + offset + position
                    owner_info = self._validate_database_owner(owner)
                    if owner_info:
                        flags, _internal_index, name_index, initialized = owner_info
                        rank = (
                            int(name_index == 0xB38CC),
                            int(flags == 0),
                            int(initialized != 0),
                        )
                        owners.append((rank, owner))
                    result = self._validate_database(owner, target_dex)
                    if result:
                        data, num, maximum, unique, target_index = result
                        candidates.append((unique, owner, data, num, maximum, target_index))
                    position += 8
                if len(block) < count:
                    break
                offset += max(count - 7, 1)
        if not candidates and allow_uninitialized and owners:
            owners.sort(reverse=True)
            best_rank = owners[0][0]
            best = sorted({owner for rank, owner in owners if rank == best_rank})
            if len(best) != 1:
                raise DatabaseNotLoadedError(
                    "found multiple uninitialized species-manager objects; "
                    "open and close the Pokedex once")
            owner = best[0]
            self._save_database_cache(owner, 0)
            return owner, 0, 0, -1
        if not candidates:
            raise DatabaseNotLoadedError(
                "the species database is not initialized — open the Pokedex once, then close it")
        candidates.sort(reverse=True)
        matching = [c for c in candidates if c[5] >= 0]
        _, owner, data, num, maximum, target_index = matching[0] if matching else candidates[0]
        pointer_rva = self._find_module_owner_pointer(owner)
        self._save_database_cache(owner, pointer_rva)
        if target_index < 0:
            raise DexNotFoundError(
                f"species database is loaded, but regional dex {target_dex} is absent")
        return owner, data, num, target_index

    def species_map(self, game) -> dict:
        """{species name: regional dex number} straight from the game.

        The dex number is the only thing the hook actually needs, and it was
        being typed in by hand against a table of nine hardcoded species. It is
        not hidden: each record in the species database is the dex number
        followed by the FName of its data asset (`DA_Treecko`), so the whole
        mapping reads out in one go.

        Only species with a record here can be forced -- the four in the pak
        without one (MissingNo, Gecqua and two misspelled leftovers) are unused
        placeholders.
        """
        cached = getattr(self, "_species", None)
        if cached:
            return cached

        owner = self._database_owner()
        header = self._read(owner + 0x180, 16) if owner else b""
        data, _num, maximum = (
            struct.unpack("<Qii", header) if len(header) == 16 else (0, 0, 0))
        records = (self._read(data, maximum * MAP_STRIDE)
                   if data and 0 < maximum <= 4096 else b"")
        if len(records) != maximum * MAP_STRIDE:
            records = b""

        out = {}
        for index in range(0, len(records) // MAP_STRIDE):
            record = records[index * MAP_STRIDE:(index + 1) * MAP_STRIDE]
            dex = struct.unpack_from("<I", record, 0)[0]
            if not 1 <= dex <= MAX_REGIONAL_DEX:
                continue
            try:
                asset = game.resolve_name(*struct.unpack_from("<II", record, 16))
            except Exception:
                continue
            if asset and asset.startswith("DA_"):
                out[asset[3:]] = dex

        # The table is EMPTY until the game loads species -- on a cold boot at
        # the title screen there is nothing to read, which is the whole reason
        # the dex number used to be typed by hand. Dex numbers never change for
        # a build, so the first successful read is kept and reused forever.
        if out:
            self._species = out
            self._save_species_cache(out)
            return out

        fallback = self._load_species_cache()
        if fallback:
            self._species = fallback
        return fallback

    def _database_owner(self) -> int:
        """The species manager object, or 0."""
        for attempt in (1, 2):
            try:
                return self.find_database(1, allow_uninitialized=True)[0]
            except DexNotFoundError:
                # a cached owner from an earlier launch can point at a table
                # that no longer holds dex 1; drop it and rescan once
                if attempt == 1:
                    try:
                        CACHE_PATH.unlink()
                    except OSError:
                        pass
                    continue
                return 0
            except DatabaseNotLoadedError:
                return 0
        return 0

    def _save_species_cache(self, table: dict) -> None:
        try:
            SPECIES_CACHE.parent.mkdir(parents=True, exist_ok=True)
            SPECIES_CACHE.write_text(json.dumps(table, sort_keys=True),
                                     encoding="ascii")
        except OSError:
            pass

    def _load_species_cache(self) -> dict:
        try:
            table = json.loads(SPECIES_CACHE.read_text(encoding="ascii"))
        except (OSError, ValueError):
            return {}
        return {str(k): int(v) for k, v in table.items()
                if isinstance(v, int) and 1 <= v <= MAX_REGIONAL_DEX}

    def _load_database_cache(self, target_dex, allow_uninitialized):
        try:
            lines = CACHE_PATH.read_text(encoding="ascii").splitlines()
            if len(lines) < 3:
                return None
            saved_pid, saved_base, saved_owner = int(lines[0]), int(lines[1], 0), int(lines[2], 0)
            pointer_rva = int(lines[3], 0) if len(lines) >= 4 else 0
        except (OSError, ValueError):
            return None
        if pointer_rva:
            raw_owner = self._read(self.base + pointer_rva, 8)
            if len(raw_owner) == 8:
                owner = struct.unpack("<Q", raw_owner)[0]
                result = self._validate_database(owner, target_dex)
                if result:
                    data, num, _maximum, _unique, target_index = result
                    self._save_database_cache(owner, pointer_rva)
                    return owner, data, num, target_index
        if saved_pid != self.gp.pid or saved_base != self.base:
            return None
        owner = saved_owner
        result = self._validate_database(owner, target_dex)
        if not result:
            if allow_uninitialized and self._validate_database_owner(owner):
                return owner, 0, 0, -1
            return None
        data, num, _maximum, _unique, target_index = result
        if not pointer_rva:
            pointer_rva = self._find_module_owner_pointer(owner)
            self._save_database_cache(owner, pointer_rva)
        return owner, data, num, target_index

    def _save_database_cache(self, owner: int, pointer_rva: int) -> None:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(
            f"{self.gp.pid}\n{self.base:#x}\n{owner:#x}\n{pointer_rva:#x}\n",
            encoding="ascii")

    def _validate_database_owner(self, owner: int):
        raw = self._read(owner, SPECIES_DB_READY_OFF + 1)
        if len(raw) != SPECIES_DB_READY_OFF + 1:
            return None
        vtable = struct.unpack_from("<Q", raw, 0)[0]
        flags, internal_index = struct.unpack_from("<II", raw, 8)
        class_pointer = struct.unpack_from("<Q", raw, 0x10)[0]
        name_index = struct.unpack_from("<I", raw, 0x18)[0]
        initialized = raw[SPECIES_DB_READY_OFF]
        if vtable != self.base + SPECIES_DB_VTABLE_OFF:
            return None
        if flags & 0x10:
            return None
        if not (0 < internal_index < 0x10000000 and 0 < name_index < 0x10000000):
            return None
        if class_pointer < 0x10000 or class_pointer % 8 != 0:
            return None
        if len(self._read(class_pointer, 0x20)) != 0x20:
            return None
        data, num, maximum = struct.unpack_from("<Qii", raw, 0x180)
        if not (0 <= num <= maximum <= 2048):
            return None
        if maximum == 0:
            if data != 0:
                return None
        elif data < 0x10000 or data % 8 != 0:
            return None
        return flags, internal_index, name_index, initialized

    def _validate_database(self, owner: int, target_dex: int):
        header = self._read(owner + 0x180, 16)
        if len(header) != 16:
            return None
        data, num, maximum = struct.unpack("<Qii", header)
        if not (data >= 0x10000 and data % 8 == 0 and 40 <= num <= maximum <= 2048):
            return None
        records = self._read(data, maximum * MAP_STRIDE)
        if len(records) != maximum * MAP_STRIDE:
            return None
        keys: set[int] = set()
        target_index = -1
        for index in range(maximum):
            dex = struct.unpack_from("<I", records, index * MAP_STRIDE)[0]
            if 1 <= dex <= MAX_REGIONAL_DEX:
                keys.add(dex)
                if dex == target_dex:
                    target_index = index
        if len(keys) < 40 or len(keys) * 2 < num:
            return None
        return data, num, maximum, len(keys), target_index

    def _find_module_owner_pointer(self, owner: int) -> int:
        pattern = struct.pack("<Q", owner)
        address = self.base
        info = MEMORY_BASIC_INFORMATION()
        while address < 0x00007FFFFFFF0000:
            if not VirtualQueryEx(
                self.gp.handle, wintypes.LPCVOID(address),
                ctypes.byref(info), ctypes.sizeof(info)
            ):
                break
            region_base = int(info.BaseAddress or 0)
            region_size = int(info.RegionSize)
            if int(info.AllocationBase or 0) != self.base:
                break
            protection = int(info.Protect) & 0xFF
            if (info.State == MEM_COMMIT and protection in READABLE
                    and not (info.Protect & PAGE_GUARD)):
                offset = 0
                while offset < region_size:
                    count = min(CHUNK, region_size - offset)
                    block = self._read(region_base + offset, count)
                    if len(block) < 8:
                        break
                    position = block.find(pattern)
                    while position >= 0:
                        absolute = region_base + offset + position
                        if absolute % 8 == 0:
                            return absolute - self.base
                        position = block.find(pattern, position + 1)
                    if len(block) < count:
                        break
                    offset += max(count - 7, 1)
            next_address = region_base + region_size
            if next_address <= address:
                break
            address = next_address
        return 0

    def install(self, owner: int, dex: int, shiny: bool) -> int:
        hook = self.base + HOOK_OFF
        roll = self.base + SHINY_ROLL_OFF
        random = self.base + SHINY_RAND_OFF
        expected = ((hook, HOOK_ORIG), (roll, SHINY_ROLL_ORIG), (random, SHINY_RAND_ORIG))
        for address, original in expected:
            current = self._read(address, len(original))
            if current != original:
                raise RuntimeError(
                    f"instruction mismatch at {address:#x}: {current.hex(' ')}; "
                    "clear the hook first, or this is a different game build")

        cave = self.gp.allocate_near(hook)
        stub = bytearray.fromhex(
            "48 83 EC 50 "
            "48 89 4C 24 20 48 89 54 24 28 "
            "4C 89 44 24 30 4C 89 4C 24 38 "
            "4C 89 54 24 40 4C 89 5C 24 48 48 B9"
        )
        stub += struct.pack("<Q", owner)
        stub += b"\xBA" + struct.pack("<I", dex) + b"\x48\xB8"
        stub += struct.pack("<Q", self.base + GET_BY_DEX_OFF)
        stub += bytes.fromhex(
            "FF D0 48 8B 54 24 28 48 85 C0 48 0F 45 D0 "
            "48 8B 4C 24 20 4C 8B 44 24 30 4C 8B 4C 24 38 "
            "4C 8B 54 24 40 4C 8B 5C 24 48 48 83 C4 50 48 B8"
        )
        stub += struct.pack("<Q", self.base + INIT_OFF)
        stub += b"\xFF\xD0\x48\xB8" + struct.pack("<Q", hook + len(HOOK_ORIG)) + b"\xFF\xE0"
        self.gp.wpm(cave, bytes(stub))

        shiny1, shiny2 = cave + 0x200, cave + 0x240
        if shiny:
            first = bytearray.fromhex("B0 01 88 83 64 01 00 00 E9 00 00 00 00")
            first[9:13] = relative(shiny1 + len(first), roll + len(SHINY_ROLL_ORIG))
            second = bytearray.fromhex("B0 01 41 88 87 64 01 00 00 E9 00 00 00 00")
            second[10:14] = relative(shiny2 + len(second), random + len(SHINY_RAND_ORIG))
            self.gp.wpm(shiny1, bytes(first))
            self.gp.wpm(shiny2, bytes(second))

        try:
            if shiny:
                roll_jump = b"\xE9" + relative(roll + 5, shiny1) + b"\x90"
                rand_jump = b"\xE9" + relative(random + 5, shiny2) + b"\x90\x90"
                self.gp.write_code(roll, roll_jump)
                self.gp.write_code(random, rand_jump)
            hook_jump = b"\xE9" + relative(hook + 5, cave)
            self.gp.write_code(hook, hook_jump)
        except Exception:
            self.clear()
            raise
        return cave
