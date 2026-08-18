"""List the party and flip a slot's persistent shiny flag.

FOUND BY REFLECTION, NOT BY SCANNING. The first version (ported from
cheatExamples/party_shiny_cli.py) searched every writable private region for a
TArray header that looked like a party. On the bug-fix build that is 8.36 GB
across 57,367 regions: it took 101 seconds, the UI sat on "scanning…", and what
it eventually locked onto was not the party at all -- one slot, a near-zero
UUID, and the name "species-0x5d", because species names came from a hardcoded
table of five.

The game keeps the party where you would expect:

    PokemonBoxSystem.Party      TArray<FPokemon>, 384 bytes per entry
      +0x00 SpeciesData         FSoftObjectPath -> .../DA_Treecko
      +0x34 Level    +0x74 Nature   +0x44 six IVs
      +0x164 bIsShiny            +0x170 UniqueID

Every one of those offsets is looked up BY NAME through UE reflection at
runtime rather than written down here, so the same code works on both builds
and should survive the next update. Reading the live party this way takes
milliseconds.
"""
from __future__ import annotations

import os
import struct
from datetime import datetime
from pathlib import Path

BOX_CLASS = "PokemonBoxSystem"
PARTY_FIELD = "Party"
PARTY_CAPACITY = 16

BACKUP_DIR = (
    Path(os.environ.get("LOCALAPPDATA", Path.home()))
    / "GammaToolkit" / "party_backups"
)


def _species_name(game, gp, entry: int, off: int) -> str:
    """Species from the FSoftObjectPath: '.../DA_Treecko' -> 'Treecko'.

    The old table knew five species by a hash of PokemonID and rendered
    everything else as "species-0x5d". The asset path is always there.
    """
    raw = gp.rpm(entry + off, 24)
    if not raw or len(raw) < 24:
        return "?"
    try:
        asset = game.resolve_name(*struct.unpack_from("<II", raw, 16))
        if not asset:
            pkg = game.resolve_name(*struct.unpack_from("<II", raw, 8)) or ""
            asset = pkg.rsplit("/", 1)[-1]
    except Exception:
        return "?"
    if not asset:
        return "?"
    return asset[3:] if asset.startswith("DA_") else asset


class PokemonRecord:
    """One party slot, read through the field offsets reflection gave us."""

    def __init__(self, game, gp, address: int, fields: dict):
        self.address = address
        self.gp = gp

        def i32(name, default=0):
            off = fields.get(name)
            if off is None:
                return default
            raw = gp.rpm(address + off, 4)
            return struct.unpack("<i", raw)[0] if raw and len(raw) == 4 else default

        def u8(name, default=0):
            off = fields.get(name)
            if off is None:
                return default
            raw = gp.rpm(address + off, 1)
            return raw[0] if raw else default

        self.level = i32("Level")
        self.shiny = u8("bIsShiny")
        self.fainted = u8("bIsFainted")
        self.nature = u8("Nature")
        self.gender = u8("Gender")
        self.friendship = i32("Friendship")
        self.ivs = tuple(i32(n) for n in (
            "HP_IV", "Attack_IV", "Defense_IV",
            "SpecialAttack_IV", "SpecialDefense_IV", "Speed_IV"))
        uid_off = fields.get("UniqueID")
        self.uuid = (gp.rpm(address + uid_off, 16) or b"") if uid_off is not None else b""
        self.name = _species_name(game, gp, address, fields.get("SpeciesData", 0))

    @property
    def uuid_hex(self) -> str:
        return self.uuid.hex().upper()

    def as_dict(self, slot: int) -> dict:
        return {
            "slot": slot,
            "name": self.name,
            "level": self.level,
            "shiny": bool(self.shiny),
            "key": self.name,
            "uuid": self.uuid_hex,
            "address": hex(self.address),
            "nature": self.nature,
            "ivs": list(self.ivs),
            "fainted": bool(self.fainted),
        }


class PartyTool:
    """The party, via PokemonBoxSystem. Needs the UE wrapper for reflection."""

    def __init__(self, gp, game=None):
        self.gp = gp
        self.game = game
        self._layout = None

    # ------------------------------------------------------------- layout
    def _resolve(self) -> dict:
        """{box, party_off, stride, fields} -- every offset looked up by name."""
        if self._layout:
            return self._layout
        if self.game is None:
            raise RuntimeError("the party reader needs an attached game")
        gp = self.gp
        best = None
        for box in self.game.actors_of_class(BOX_CLASS):
            cls = gp.read_u64(box + 0x10)
            if not cls:
                continue
            prop = self._property(cls, PARTY_FIELD)
            if prop is None:
                continue
            off, (stride, struct_obj) = prop
            if not stride or not struct_obj:
                continue
            head = gp.rpm(box + off, 16)
            if not head or len(head) < 16:
                continue
            ptr, num, _max = struct.unpack("<Qii", head)
            if not (0 <= num <= PARTY_CAPACITY):
                continue
            fields = {p["name"]: p["offset"]
                      for p in self.game.class_properties(struct_obj)}
            layout = {"box": box, "party_off": off, "stride": stride,
                      "fields": fields}
            # more than one instance exists (one is the template); the one
            # actually holding Pokemon is the live save
            if num > 0 and ptr:
                self._layout = layout
                return layout
            best = best or layout
        if best is None:
            raise RuntimeError(
                "could not find the party. Load a save first — the box system "
                "only exists once a game is in progress.")
        self._layout = best
        return best

    def _property(self, cls: int, name: str):
        """(offset, (inner_elem_size, inner_struct)) for a named array property."""
        gp = self.gp
        prop = gp.read_u64(cls + 0x50)
        seen = 0
        while prop and seen < 400:
            hdr = gp.rpm(prop, 0x50)
            if not hdr or len(hdr) < 0x50:
                return None
            nmi, nmn = struct.unpack_from("<II", hdr, 0x20)
            if self.game.resolve_name(nmi, nmn) == name:
                off = struct.unpack_from("<i", hdr, 0x44)[0]
                inner = gp.read_u64(prop + 0x78)          # FArrayProperty::Inner
                if not inner:
                    return None
                ib = gp.rpm(inner, 0x50)
                if not ib or len(ib) < 0x50:
                    return None
                elem = struct.unpack_from("<I", ib, 0x34)[0]
                # FStructProperty::Struct sits just past the FProperty header
                sobj = 0
                for cand in (0x78, 0x70, 0x80):
                    v = gp.read_u64(inner + cand)
                    if v and self.game.obj_name(v):
                        sobj = v
                        break
                return off, (elem, sobj)
            prop = struct.unpack_from("<Q", hdr, 0x18)[0]
            seen += 1
        return None

    # -------------------------------------------------------------- reads
    def find_party(self) -> list[PokemonRecord]:
        info = self._resolve()
        gp = self.gp
        head = gp.rpm(info["box"] + info["party_off"], 16)
        if not head or len(head) < 16:
            return []
        ptr, num, _max = struct.unpack("<Qii", head)
        if not ptr or num <= 0:
            return []
        num = min(num, PARTY_CAPACITY)
        return [PokemonRecord(self.game, gp, ptr + i * info["stride"], info["fields"])
                for i in range(num)]

    def list(self, rescan: bool = False) -> dict:
        if rescan:
            self._layout = None
        party = self.find_party()
        return {
            "count": len(party),
            "slots": [p.as_dict(i) for i, p in enumerate(party, 1)],
        }

    # ------------------------------------------------------------- writes
    def set_shiny(self, slot: int, shiny: bool) -> dict:
        """Flip the persistent shiny flag on one slot.

        Only the party record is written. The battle actor rebuilds its own
        isShiny from this record, and hunting down every in-memory copy meant
        another whole-heap scan for no gain.
        """
        info = self._resolve()
        off = info["fields"].get("bIsShiny")
        if off is None:
            raise RuntimeError("this build's party record has no bIsShiny field")
        party = self.find_party()
        if not 1 <= slot <= len(party):
            raise ValueError("slot must be between 1 and %d" % len(party))
        target = party[slot - 1]
        backup = self._backup(target, info["stride"])
        value = 1 if shiny else 0
        if self.gp.wpm(target.address + off, bytes([value])) != 1:
            raise RuntimeError("write failed at %#x" % (target.address + off))
        if self.gp.rpm(target.address + off, 1) != bytes([value]):
            raise RuntimeError("verification failed at %#x" % (target.address + off))
        return {
            "slot": slot,
            "name": target.name,
            "shiny": bool(shiny),
            "copies": 1,
            "backup": str(backup),
        }

    def _backup(self, record: PokemonRecord, stride: int) -> Path:
        """Save the raw record before writing, so a bad flip can be undone."""
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        folder = BACKUP_DIR / stamp
        folder.mkdir(parents=True, exist_ok=True)
        raw = self.gp.rpm(record.address, stride) or b""
        (folder / ("%s-%016X.bin" % (record.name, record.address))).write_bytes(raw)
        return folder
