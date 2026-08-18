"""The party is read through reflection, not by scanning the heap.

The old reader searched every writable region for a plausible TArray header.
On the bug-fix build that meant 8.36 GB and 101 seconds, it found the wrong
thing, and because the UI reads the party automatically on attach it also held
the shared cheat lock long enough to make the wild-encounter hook look hung.
"""
import struct

import pytest

from gamma import party


class FakeGP:
    def __init__(self):
        self.mem = bytearray(0x10000)
        self.base = 0x100000
        self.writes = []

    def _slice(self, addr):
        return addr - self.base

    def rpm(self, addr, size):
        i = self._slice(addr)
        if i < 0 or i + size > len(self.mem):
            return b""
        return bytes(self.mem[i:i + size])

    def wpm(self, addr, data):
        i = self._slice(addr)
        self.mem[i:i + len(data)] = data
        self.writes.append((addr, bytes(data)))
        return len(data)

    def read_u64(self, addr):
        raw = self.rpm(addr, 8)
        return struct.unpack("<Q", raw)[0] if len(raw) == 8 else 0

    def put(self, addr, data):
        i = self._slice(addr)
        self.mem[i:i + len(data)] = data


FIELDS = {"SpeciesData": 0x00, "Level": 0x34, "Nature": 0x74,
          "HP_IV": 0x44, "Attack_IV": 0x48, "Defense_IV": 0x4C,
          "SpecialAttack_IV": 0x50, "SpecialDefense_IV": 0x54, "Speed_IV": 0x58,
          "bIsShiny": 0x164, "bIsFainted": 0x165, "UniqueID": 0x170,
          "Friendship": 0x160, "Gender": 0x75}
STRIDE = 384


class FakeGame:
    """Serves the reflection answers PartyTool asks for."""

    def __init__(self, gp, box, party_off, entries):
        self.gp = gp
        self.box = box
        self.party_off = party_off
        self.entries = entries

    def actors_of_class(self, name, limit=4000):
        return [self.box] if name == party.BOX_CLASS else []

    def class_properties(self, obj):
        return [{"name": n, "offset": o, "type": "Property", "elemSize": 4,
                 "arrayDim": 1} for n, o in FIELDS.items()]

    def obj_name(self, obj):
        return "FPokemon" if obj else None

    def resolve_name(self, index, number):
        return {1: "DA_Treecko", 2: "/Game/Pokemon/DA_Treecko"}.get(index, "")


def _tool(shiny=0, num=1):
    gp = FakeGP()
    box, data = 0x100000, 0x108000
    gp.put(box + 0x28, struct.pack("<Qii", data, num, num))
    for i in range(num):
        e = data + i * STRIDE
        gp.put(e + 0x08, struct.pack("<II", 2, 0))      # package name
        gp.put(e + 0x10, struct.pack("<II", 1, 0))      # asset name
        gp.put(e + FIELDS["Level"], struct.pack("<i", 6))
        gp.put(e + FIELDS["bIsShiny"], bytes([shiny]))
        gp.put(e + FIELDS["UniqueID"], bytes(range(16)))
    tool = party.PartyTool(gp, FakeGame(gp, box, 0x28, data))
    # skip the live property walk; that path is exercised against the game
    tool._layout = {"box": box, "party_off": 0x28, "stride": STRIDE,
                    "fields": FIELDS}
    return tool, gp, data


def test_party_reads_species_name_not_a_hex_placeholder():
    """The old table knew five species; everything else read 'species-0x5d'."""
    tool, _gp, _d = _tool()
    slots = tool.list()["slots"]
    assert len(slots) == 1
    assert slots[0]["name"] == "Treecko"
    assert not slots[0]["name"].startswith("species-")
    assert slots[0]["level"] == 6


def test_set_shiny_writes_only_the_flag_byte():
    tool, gp, data = _tool(shiny=0)
    before = bytes(gp.mem)
    tool.set_shiny(1, True)
    assert tool.list()["slots"][0]["shiny"] is True
    changed = [i for i in range(len(before)) if before[i] != gp.mem[i]]
    assert changed == [data - gp.base + FIELDS["bIsShiny"]], \
        "set_shiny touched bytes other than bIsShiny"


def test_set_shiny_rejects_a_slot_that_is_not_there():
    tool, _gp, _d = _tool()
    with pytest.raises(ValueError):
        tool.set_shiny(3, True)


def test_an_empty_party_is_not_an_error():
    tool, gp, _d = _tool(num=0)
    assert tool.list() == {"count": 0, "slots": []}


def test_the_reader_never_walks_process_memory():
    """Guard against the 101-second heap scan coming back."""
    import inspect
    src = inspect.getsource(party)
    assert "writable_private_regions" not in src
    assert "find_uuid_copies" not in src
