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


# Code offsets, per build. The bug-fix update moved every one of them by a
# different amount (the exe grew 9 KB and functions shifted independently), so
# there is no single delta to apply.
#
# The bug-fix set was recovered from the Early Access set rather than guessed:
#   INIT and GET_BY_DEX by matching a 48-byte code signature across the two exes
#   HOOK by finding the call whose target IS the relocated INIT -- 13 call sites
#     do, and the right one is the only one with the shiny-roll store at +0x54
#     (context matched 148/160 bytes; the runner-up matched 17)
#   SHINY_ROLL and SHINY_RAND sit at the same offsets from HOOK and INIT as
#     before (+0x54 and +0x31B), which is itself a check that the right
#     functions were found.
#
# The 2026-08-18 exe kept that whole block and slid it +0x4A0. Confirmed in the
# file: hook is still `call INIT`, roll/rand bytes match, and get_by_dex still
# looks up the TMap at this+0x180 with stride 0x30.
#
# Which set to use is decided by reading the bytes in the live process, not by
# a version string or a file hash: a build we have never seen fails loudly
# instead of writing a jump into the middle of an unrelated instruction.
BUILD_OFFSETS = {
    "ea-2026-08-15": {
        "hook": 0x0A65AEC5, "init": 0x0A66D300, "get_by_dex": 0x0A6674B0,
        "shiny_roll": 0x0A65AF19, "shiny_rand": 0x0A66D61B,
        "species_db_vtable": 0x126B3E80,
    },
    "bugfix-2026-08-17": {
        "hook": 0x0A657F95, "init": 0x0A669B60, "get_by_dex": 0x0A664EA0,
        "shiny_roll": 0x0A657FE9, "shiny_rand": 0x0A669E7B,
        "species_db_vtable": None,      # found by class instead; see _owner_by_class
    },
    "ea-2026-08-18": {
        "hook": 0x0A658435, "init": 0x0A66A000, "get_by_dex": 0x0A665340,
        "shiny_roll": 0x0A658489, "shiny_rand": 0x0A66A31B,
        "species_db_vtable": None,
    },
}

# The subsystem that owns the dex -> species map. Looking it up by class works on
# any build, which the vtable scan did not: the vtable moved in the bug-fix
# update and scanning every writable page for it took the better part of a
# minute anyway.
SPECIES_OWNER_CLASS = "PokemonManagerSubsystem"
SPECIES_DB_MAP_OFF = 0x180

SPECIES_DB_READY_OFF = 0x250

# The instruction at the hook site is `call INIT`, and a call encodes a
# DISTANCE. That distance differs per build because INIT moved, so the expected
# bytes are computed from each build's own offsets rather than stored -- the
# stored constant matched Early Access only, and rejected the bug-fix build
# whose identical instruction reads E8 C6 1B 01 00.
HOOK_CALL_LEN = 5
SHINY_ROLL_ORIG = bytes.fromhex("88 83 64 01 00 00")
SHINY_RAND_ORIG = bytes.fromhex("41 88 87 64 01 00 00")

# WILD SHINY ODDS. Disassembling around the shiny store shows how the game
# actually rolls it -- there is no probability constant sitting in a Blueprint:
#
#     mov  edi, [rsp+0x80]        ; N, the denominator, passed in by the caller
#     test edi, edi
#     jle  <always shiny>
#     call rand ; and eax, 0x7fff
#     lea  ecx, [rdi-1]           ; roll = min(int(rand01 * N), N-1)
#     cmp  eax, ecx ; cmovl ecx, eax
#     test ecx, ecx ; sete al     ; shiny iff the roll landed on 0  => 1/N
#     mov  [rbx+0x164], al        ; bIsShiny
#
# Overwriting the LOAD of N with an immediate therefore gives exact odds: the
# 7-byte `mov edi,[rsp+0x80]` becomes a 5-byte `mov edi, N` plus two nops.
#
# The old approach searched Blueprint bytecode for a 0.01 float. On the bug-fix
# build that matched two NPC "Footsteps" functions and nothing else, so the
# odds control was patching animation timings and reporting success.
WILD_ODDS_LOAD = bytes.fromhex("8b bc 24 80 00 00 00")   # mov edi, [rsp+0x80]
WILD_ODDS_WINDOW = 0x80          # how far above the store to look for it
WILD_ODDS_DEFAULT = 4096         # what the game rolls when left alone

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

    def __init__(self, gp: GameProcess, game=None):
        self.gp = gp
        # set by the session on attach; lets the species subsystem be found by
        # class instead of by scanning for a vtable that moves between builds
        self.game = game
        self.enabled = False
        self.dex = None
        self.label = ""
        self.shiny = False
        self.cave = 0

    def _read(self, addr, n) -> bytes:
        return self.gp.rpm(addr, n) or b""

    # ------------------------------------------------------------- build id
    @staticmethod
    def hook_orig(off: dict) -> bytes:
        """The untouched `call INIT` bytes for this build."""
        distance = off["init"] - (off["hook"] + HOOK_CALL_LEN)
        return bytes([0xE8]) + struct.pack("<i", distance)

    def offsets(self) -> dict:
        """The offset set whose instructions are actually present, cached.

        Checked against the live process so an unknown build is reported rather
        than patched blindly. A build that already has our hook installed still
        resolves, because the shiny stores are left alone when shiny is off --
        so the check accepts either the original bytes or our jump.
        """
        cached = getattr(self, "_offsets", None)
        if cached:
            return cached
        problems = []
        for build, off in BUILD_OFFSETS.items():
            checks = ((off["hook"], self.hook_orig(off)),
                      (off["shiny_roll"], SHINY_ROLL_ORIG),
                      (off["shiny_rand"], SHINY_RAND_ORIG))
            ok = True
            for rva, original in checks:
                cur = self._read(self.base + rva, len(original))
                if cur != original and not (cur and cur[0] == 0xE9):
                    ok = False
                    problems.append("%s @%#x: %s" % (build, rva, cur.hex(" ")))
                    break
            if ok:
                self._offsets = dict(off, build=build)
                return self._offsets
        raise RuntimeError(
            "this game build is not recognised, so the encounter hook was not "
            "installed. Tried: " + "; ".join(problems))

    @property
    def build(self) -> str:
        return self.offsets().get("build", "?")

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
        off = self.offsets()
        cur = self._read(self.base + off['hook'], HOOK_CALL_LEN)
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

    # ------------------------------------------------------- wild odds
    def wild_odds_site(self) -> int:
        """Address of the `mov edi,[rsp+0x80]` that loads the denominator.

        Located relative to the shiny store rather than stored as its own
        per-build offset, so it follows the site we already validate.
        """
        cached = getattr(self, "_odds_site", None)
        if cached:
            return cached
        off = self.offsets()
        start = off["shiny_roll"] - WILD_ODDS_WINDOW
        window = self._read(self.base + start, WILD_ODDS_WINDOW)
        at = window.rfind(WILD_ODDS_LOAD)
        if at < 0:
            # already patched? our own `mov edi, imm32` + two nops
            at = window.rfind(b"\xbf")
            if at < 0 or window[at + 5:at + 7] != b"\x90\x90":
                raise RuntimeError(
                    "could not find the shiny denominator in this build, so the "
                    "odds were left alone")
        self._odds_site = self.base + start + at
        return self._odds_site

    def wild_odds(self) -> dict:
        """What the wild roll currently uses: {denominator, patched}."""
        site = self.wild_odds_site()
        cur = self._read(site, 7)
        if cur == WILD_ODDS_LOAD:
            return {"denominator": WILD_ODDS_DEFAULT, "patched": False}
        if cur[:1] == b"\xbf" and cur[5:7] == b"\x90\x90":
            return {"denominator": struct.unpack("<I", cur[1:5])[0], "patched": True}
        return {"denominator": None, "patched": False}

    def set_wild_odds(self, denominator: int) -> dict:
        """Make wild encounters shiny 1 time in `denominator`.

        1 means every wild Pokemon is shiny. Only the wild side is affected:
        measured with the odds at 1/1, the encountered Zigzagoon came out shiny
        and the player's own Treecko did not. (Forcing shiny through the
        encounter hook is different -- that writes 1 unconditionally, and does
        catch the player's party.)
        """
        n = int(denominator)
        if not 1 <= n <= 0x7FFFFFFF:
            raise ValueError("odds must be between 1 and 2147483647")
        site = self.wild_odds_site()
        patch = b"\xbf" + struct.pack("<I", n) + b"\x90\x90"
        self.gp.write_code(site, patch)
        if self._read(site, 7) != patch:
            raise RuntimeError("the odds patch did not stick")
        return {"denominator": n, "patched": True, "site": hex(site)}

    def clear_wild_odds(self) -> dict:
        """Put the original denominator load back."""
        site = self.wild_odds_site()
        if self._read(site, 7) != WILD_ODDS_LOAD:
            self.gp.write_code(site, WILD_ODDS_LOAD)
        return {"denominator": WILD_ODDS_DEFAULT, "patched": False}

    def clear(self) -> dict:
        off = self.offsets()
        patches = (
            (self.base + off["hook"], self.hook_orig(off), bytes.fromhex("48 83 EC 50")),
            (self.base + off["shiny_roll"], SHINY_ROLL_ORIG, bytes.fromhex("B0 01")),
            (self.base + off["shiny_rand"], SHINY_RAND_ORIG, bytes.fromhex("B0 01")),
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
        # Ask the object system first: it is instant, and it is right even when
        # a cache written by an earlier session points at a stale address.
        # Consulting the cache first cost ~65s, because a stale hit fell through
        # to the whole-heap scan.
        owner = self._owner_by_class()
        if owner:
            result = self._validate_database(owner, target_dex)
            if result:
                data, num, maximum, _unique, target_index = result
                if target_index < 0:
                    raise DexNotFoundError(
                        f"species database is loaded, but regional dex {target_dex} is absent")
                return owner, data, num, target_index
            if allow_uninitialized:
                return owner, 0, 0, -1
            # The subsystem exists and its table is empty: the species have not
            # been loaded yet. Scanning the heap cannot change that, so say so now.
            raise DatabaseNotLoadedError(
                "the species database has not been filled in yet -- load a save "
                "or open the Pokedex once, then try again")

        cached = self._load_database_cache(target_dex, allow_uninitialized)
        if cached:
            if cached[3] < 0:
                raise DexNotFoundError(
                    f"species database is loaded, but regional dex {target_dex} is absent")
            return cached
        # Fallback for a build whose class name we do not know. This is instant and
        # build-independent; the page scan below is the fallback for a build
        # whose class name we do not know.
        expected_vtable = self.base + (self.offsets().get("species_db_vtable") or 0)
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

    def _owner_by_class(self) -> int:
        """The species subsystem, found through the object array.

        An instance with an EMPTY map still counts. The game fills the table
        lazily -- at the title screen and just after a load it is genuinely
        empty -- and returning 0 for that sent the caller into a whole-heap
        scan that took 64 seconds to conclude the same thing.
        """
        game = getattr(self, "game", None)
        if game is None:
            return 0
        fallback = 0
        try:
            for obj in game.actors_of_class(SPECIES_OWNER_CLASS):
                head = self._read(obj + SPECIES_DB_MAP_OFF, 16)
                if len(head) != 16:
                    continue
                data, num, maximum = struct.unpack("<Qii", head)
                if data and 0 < num <= maximum <= 4096:
                    return obj                      # populated: the real thing
                fallback = fallback or obj          # present but not filled yet
        except Exception:
            pass
        return fallback

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
        want_vtable = self.offsets().get("species_db_vtable")
        if want_vtable and vtable != self.base + want_vtable:
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
        off = self.offsets()
        hook = self.base + off["hook"]
        roll = self.base + off["shiny_roll"]
        random = self.base + off["shiny_rand"]
        expected = ((hook, self.hook_orig(off)), (roll, SHINY_ROLL_ORIG),
                    (random, SHINY_RAND_ORIG))
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
        stub += struct.pack("<Q", self.base + off["get_by_dex"])
        stub += bytes.fromhex(
            "FF D0 48 8B 54 24 28 48 85 C0 48 0F 45 D0 "
            "48 8B 4C 24 20 4C 8B 44 24 30 4C 8B 4C 24 38 "
            "4C 8B 54 24 40 4C 8B 5C 24 48 48 83 C4 50 48 B8"
        )
        stub += struct.pack("<Q", self.base + off["init"])
        stub += b"\xFF\xD0\x48\xB8" + struct.pack("<Q", hook + HOOK_CALL_LEN) + b"\xFF\xE0"
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
