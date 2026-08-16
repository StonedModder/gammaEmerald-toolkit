"""Scan writable heap for the authoritative int64 money value.

There is no stable module-relative money offset. The workflow matches Cheat
Engine: exact-value scan, spend or earn, narrow, then write the one survivor.
Writing a 4-byte value or writing while many candidates remain is refused —
a false-positive money write has frozen the game.
"""
from __future__ import annotations

import struct

from .memory import GameProcess

CHUNK = 8 * 1024 * 1024
MAX_KEEP = 8000


def iter_aligned_qwords(block: bytes, region_base: int, value: int):
    """Yield addresses of little-endian int64 `value` on 8-byte alignment."""
    needle = struct.pack("<q", value)
    position = 0
    while True:
        position = block.find(needle, position)
        if position < 0:
            return
        if position % 8 == 0:
            yield region_base + position
        position += 1


class MoneyScanner:
    def __init__(self, gp: GameProcess):
        self.gp = gp
        self.candidates: dict[int, int] = {}
        self.last_scan: int | None = None

    def _read(self, addr, n) -> bytes:
        return self.gp.rpm(addr, n) or b""

    def status(self) -> dict:
        return {
            "count": len(self.candidates),
            "last": self.last_scan,
            "ready": len(self.candidates) == 1,
            "address": hex(next(iter(self.candidates))) if len(self.candidates) == 1 else None,
        }

    def reset(self) -> dict:
        self.candidates = {}
        self.last_scan = None
        return self.status()

    def scan(self, value: int) -> dict:
        value = int(value)
        found: dict[int, int] = {}
        for region_base, region_size in self.gp.writable_private_regions():
            offset = 0
            while offset < region_size:
                count = min(CHUNK, region_size - offset)
                block = self._read(region_base + offset, count)
                if len(block) < 8:
                    break
                for addr in iter_aligned_qwords(block, region_base + offset, value):
                    found[addr] = value
                    if len(found) >= MAX_KEEP:
                        self.candidates = found
                        self.last_scan = value
                        return {**self.status(), "truncated": True}
                if len(block) < count:
                    break
                offset += max(count - 7, 1)
        self.candidates = found
        self.last_scan = value
        return {**self.status(), "truncated": False}

    def narrow(self, value: int) -> dict:
        """Keep only candidates that now hold `value` (after spend/earn)."""
        if not self.candidates:
            raise RuntimeError("scan the current money amount first")
        value = int(value)
        kept: dict[int, int] = {}
        for addr in list(self.candidates):
            raw = self._read(addr, 8)
            if len(raw) != 8:
                continue
            if struct.unpack("<q", raw)[0] == value:
                kept[addr] = value
        self.candidates = kept
        self.last_scan = value
        return self.status()

    def write(self, amount: int) -> dict:
        if len(self.candidates) != 1:
            raise RuntimeError(
                f"need exactly one candidate to write, have {len(self.candidates)}")
        amount = int(amount)
        addr = next(iter(self.candidates))
        blob = struct.pack("<q", amount)
        n = self.gp.wpm(addr, blob)
        if n != 8:
            raise RuntimeError(f"short write at {addr:#x}")
        raw = self._read(addr, 8)
        if raw != blob:
            raise RuntimeError(f"verification failed at {addr:#x}")
        self.candidates = {addr: amount}
        self.last_scan = amount
        return {"ok": True, "address": hex(addr), "amount": amount}


# ---------------------------------------------------------------- direct read
# The scan above is the generic fallback. It is no longer the way money is
# found, because the game turned out to expose it by reflection:
#
#   ItemInventorySystem.Money   int32
#
# VERIFIED on a live purchase at the Oldale Pokemart. Setting this field to
# 999,999 and buying ten Potions left it at 996,999 and the shop's own money
# text changed to "996,999" to match -- the game spends THIS value.
#
# Two decoys were ruled out the same way:
#   BP_Brendan_C.pokeDollars      never changes; it still held the class default
#                                 (3000) while the shop showed 1,500
#   PokemonBoxSystem.PlayerMoney  tracks money but writing it does nothing
#
# The offset is looked up through the class's own property table rather than
# hardcoded, so a patch that moves the field does not silently write garbage.
MONEY_CLASS = "ItemInventorySystem"
MONEY_FIELD = "Money"


def money_site(game):
    """(object address, field offset) for the live money, or None."""
    cls = game.class_ptr(MONEY_CLASS)
    if not cls:
        return None
    offset = None
    for prop in (game.class_properties(cls) or []):
        if prop.get("name") == MONEY_FIELD and prop.get("type") == "IntProperty":
            offset = prop["offset"]
            break
    if offset is None:
        return None
    for obj in game.actors_of_class(MONEY_CLASS):
        return obj, offset
    return None


def read_money(game):
    site = money_site(game)
    if not site:
        return None
    raw = game.gp.rpm(site[0] + site[1], 4)
    return struct.unpack("<i", raw)[0] if raw else None


def write_money(game, amount: int) -> dict:
    """Set the player's money. Refuses values the game cannot hold."""
    amount = int(amount)
    if not 0 <= amount <= 999_999_999:
        raise ValueError("money must be between 0 and 999,999,999")
    site = money_site(game)
    if not site:
        raise RuntimeError(
            "could not find the money field — open the game to a loaded save")
    before = read_money(game)
    game.gp.wpm(site[0] + site[1], struct.pack("<i", amount))
    after = read_money(game)
    return {"before": before, "money": after, "address": hex(site[0] + site[1])}
