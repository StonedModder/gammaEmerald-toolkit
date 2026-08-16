"""List the party and flip a slot's persistent shiny flag.

Ported from cheatExamples/party_shiny_cli.py. Only the record + UUID copies are
written — the battle actor's isShiny is rebuilt from the party record.
"""
from __future__ import annotations

import os
import re
import struct
from datetime import datetime
from pathlib import Path

from .memory import GameProcess


SLOT_SIZE = 0x180
LEVEL_OFF = 0x34
IVS_OFF = 0x44
TRAINER_OFF = 0xC8
SHINY_OFF = 0x164
UUID_OFF = 0x170
PARTY_CAPACITY = 16
CHUNK = 8 * 1024 * 1024

CACHE_PATH = (
    Path(os.environ.get("LOCALAPPDATA", Path.home()))
    / "GammaToolkit" / "party.txt"
)
BACKUP_DIR = (
    Path(os.environ.get("LOCALAPPDATA", Path.home()))
    / "GammaToolkit" / "party_backups"
)

KNOWN_PRIMARY_NAMES = {
    0x000014FB: "Mudkip",
    0x0000C3BF: "Poochyena",
    0x00047F98: "Lotad",
    0x000E0913: "Ralts",
    0x00096893: "Beldum",
}
KNOWN_ADJACENT_NAMES = {
    0x00972372: "Poochyena",
    0x00C6EAFD: "Beldum",
}


class PokemonRecord:
    def __init__(self, address: int, raw: bytes):
        self.address = address
        self.raw = raw
        self.primary = struct.unpack_from("<I", raw, 0x28)[0]
        self.adjacent = struct.unpack_from("<I", raw, 0x2C)[0]
        self.level = struct.unpack_from("<I", raw, LEVEL_OFF)[0]
        self.ivs = struct.unpack_from("<6I", raw, IVS_OFF)
        self.trainers = struct.unpack_from("<2I", raw, TRAINER_OFF)
        self.shiny = raw[SHINY_OFF]
        self.uuid = raw[UUID_OFF: UUID_OFF + 16]

    @property
    def name(self) -> str:
        return KNOWN_PRIMARY_NAMES.get(
            self.primary,
            KNOWN_ADJACENT_NAMES.get(self.adjacent, f"species-{self.primary:#x}"),
        )

    @property
    def uuid_hex(self) -> str:
        return self.uuid.hex().upper()

    def as_dict(self, slot: int) -> dict:
        return {
            "slot": slot,
            "name": self.name,
            "level": self.level,
            "shiny": bool(self.shiny),
            "key": hex(self.primary),
            "uuid": self.uuid_hex,
            "address": hex(self.address),
        }


def parse_party_bytes(gp_read, data: int, count: int) -> list[PokemonRecord] | None:
    if not (1 <= count <= 6 and data >= 0x10000 and data % 8 == 0):
        return None
    raw = gp_read(data, count * SLOT_SIZE)
    if not raw or len(raw) != count * SLOT_SIZE:
        return None
    records: list[PokemonRecord] = []
    uuids: set[bytes] = set()
    for index in range(count):
        slot = raw[index * SLOT_SIZE: (index + 1) * SLOT_SIZE]
        record = PokemonRecord(data + index * SLOT_SIZE, slot)
        friendship = struct.unpack_from("<I", slot, 0x160)[0]
        number = struct.unpack_from("<I", slot, 0x30)[0]
        if not (
            0 < record.primary < 0x10000000
            and number == 0
            and 1 <= record.level <= 100
            and all(iv <= 31 for iv in record.ivs)
            and record.shiny in (0, 1)
            and friendship <= 255
            and record.uuid not in (b"\x00" * 16, b"\xFF" * 16)
            and record.uuid not in uuids
        ):
            return None
        uuids.add(record.uuid)
        records.append(record)
    return records


class PartyTool:
    def __init__(self, gp: GameProcess):
        self.gp = gp
        self._party: list[PokemonRecord] | None = None

    def _read(self, addr, n) -> bytes:
        return self.gp.rpm(addr, n) or b""

    def list(self, rescan: bool = False) -> dict:
        if rescan:
            self._party = None
            try:
                CACHE_PATH.unlink()
            except OSError:
                pass
        party = self.find_party()
        return {
            "count": len(party),
            "slots": [p.as_dict(i) for i, p in enumerate(party, 1)],
        }

    def set_shiny(self, slot: int, shiny: bool) -> dict:
        party = self.find_party()
        if not 1 <= slot <= len(party):
            raise ValueError(f"slot must be between 1 and {len(party)}")
        target = party[slot - 1]
        copies = self.find_uuid_copies(target)
        if not copies:
            raise RuntimeError("validated UUID copies of that Pokemon were not found")
        value = 1 if shiny else 0
        backup = self._backup(copies)
        for record in copies:
            n = self.gp.wpm(record.address + SHINY_OFF, bytes([value]))
            if n != 1:
                raise RuntimeError(f"write failed at {record.address + SHINY_OFF:#x}")
        for record in copies:
            if self._read(record.address + SHINY_OFF, 1) != bytes([value]):
                raise RuntimeError(f"verification failed at {record.address + SHINY_OFF:#x}")
        self._party = None
        return {
            "slot": slot,
            "name": target.name,
            "shiny": bool(shiny),
            "copies": len(copies),
            "backup": str(backup),
        }

    def find_party(self) -> list[PokemonRecord]:
        cached = self._load_cache()
        if cached:
            self._party = cached
            return cached
        tail_pattern = re.compile(b"[\x01-\x06]\x00\x00\x00[\x01-\x10]\x00\x00\x00")
        groups: dict[tuple[bytes, ...], list[list[PokemonRecord]]] = {}
        seen_headers: set[int] = set()
        for region_base, region_size in self.gp.writable_private_regions():
            offset = 0
            while offset < region_size:
                count = min(CHUNK, region_size - offset)
                block = self._read(region_base + offset, count)
                if len(block) < 16:
                    break
                for match in tail_pattern.finditer(block):
                    position = match.start()
                    if position < 8 or position % 8:
                        continue
                    num, maximum = struct.unpack_from("<ii", block, position)
                    if not (1 <= num <= 6 and num <= maximum <= PARTY_CAPACITY):
                        continue
                    header = region_base + offset + position - 8
                    if header in seen_headers:
                        continue
                    seen_headers.add(header)
                    data = struct.unpack_from("<Q", block, position - 8)[0]
                    records = parse_party_bytes(self._read, data, num)
                    if records:
                        key = tuple(record.uuid for record in records)
                        groups.setdefault(key, []).append(records)
                if len(block) < count:
                    break
                offset += max(count - 15, 1)
        if not groups:
            raise RuntimeError("no validated party TArray was found")
        ranked = sorted(
            groups.values(),
            key=lambda copies: (len(copies[0]), len(copies)),
            reverse=True,
        )
        party = ranked[0][0]
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(
            f"{self.gp.pid}\n{party[0].address:#x}\n{len(party)}\n", encoding="ascii")
        self._party = party
        return party

    def _load_cache(self) -> list[PokemonRecord] | None:
        try:
            lines = CACHE_PATH.read_text(encoding="ascii").splitlines()
            if len(lines) != 3 or int(lines[0]) != self.gp.pid:
                return None
            data, count = int(lines[1], 0), int(lines[2])
        except (OSError, ValueError):
            return None
        return parse_party_bytes(self._read, data, count)

    @staticmethod
    def same_identity(candidate: PokemonRecord, target: PokemonRecord) -> bool:
        return (
            candidate.uuid == target.uuid
            and candidate.primary == target.primary
            and candidate.adjacent == target.adjacent
            and candidate.level == target.level
            and candidate.ivs == target.ivs
            and candidate.trainers == target.trainers
        )

    def find_uuid_copies(self, target: PokemonRecord) -> list[PokemonRecord]:
        matches: dict[int, PokemonRecord] = {}
        needle = target.uuid
        for region_base, region_size in self.gp.writable_private_regions():
            offset = 0
            while offset < region_size:
                count = min(CHUNK, region_size - offset)
                block = self._read(region_base + offset, count)
                if len(block) < len(needle):
                    break
                position = 0
                while True:
                    position = block.find(needle, position)
                    if position < 0:
                        break
                    uuid_address = region_base + offset + position
                    record_address = uuid_address - UUID_OFF
                    raw = self._read(record_address, SLOT_SIZE)
                    if len(raw) == SLOT_SIZE:
                        candidate = PokemonRecord(record_address, raw)
                        if self.same_identity(candidate, target):
                            matches[record_address] = candidate
                    position += 1
                if len(block) < count:
                    break
                offset += max(count - len(needle) + 1, 1)
        return sorted(matches.values(), key=lambda record: record.address)

    def _backup(self, records: list[PokemonRecord]) -> Path:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        folder = BACKUP_DIR / stamp
        folder.mkdir(parents=True, exist_ok=True)
        for record in records:
            path = folder / f"{record.uuid_hex}_{record.address:016X}.bin"
            path.write_bytes(record.raw)
        return folder
