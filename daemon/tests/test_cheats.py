"""Pure checks for the native cheat ports — no live game required."""
import struct

from gamma.encounter import MAX_REGIONAL_DEX, parse_dex, relative, resolve_pokemon
from gamma.money import iter_aligned_qwords
from gamma.party import PokemonRecord, parse_party_bytes


def test_parse_dex_leading_zeros():
    assert parse_dex("007") == 7
    assert parse_dex("42") == 42


def test_parse_dex_rejects_out_of_range():
    try:
        parse_dex("0")
        assert False
    except ValueError:
        pass
    try:
        parse_dex(str(MAX_REGIONAL_DEX + 1))
        assert False
    except ValueError:
        pass


def test_resolve_known_name():
    assert resolve_pokemon("mudkip") == ("mudkip", 7)
    assert resolve_pokemon("alakazam", "042") == ("alakazam", 42)


def test_relative_rel32():
    blob = relative(100, 150)
    assert struct.unpack("<i", blob)[0] == 50
    blob = relative(200, 100)
    assert struct.unpack("<i", blob)[0] == -100


def _slot(primary=0x14FB, level=5, shiny=0, uuid=bytes(range(16))):
    raw = bytearray(0x180)
    struct.pack_into("<I", raw, 0x28, primary)
    struct.pack_into("<I", raw, 0x34, level)
    raw[0x164] = shiny
    raw[0x170:0x180] = uuid
    return bytes(raw)


def test_pokemon_record_fields():
    rec = PokemonRecord(0x10000, _slot(shiny=1, level=12))
    assert rec.shiny == 1
    assert rec.level == 12
    assert rec.name == "Mudkip"
    d = rec.as_dict(1)
    assert d["slot"] == 1 and d["shiny"] is True


def test_parse_party_bytes_accepts_valid_slots():
    a = _slot(uuid=bytes(range(16)))
    b = _slot(primary=0xC3BF, uuid=bytes(range(16, 32)))
    blob = a + b

    def read(addr, n):
        off = addr - 0x20000
        return blob[off:off + n]

    party = parse_party_bytes(read, 0x20000, 2)
    assert party is not None
    assert [p.name for p in party] == ["Mudkip", "Poochyena"]


def test_parse_party_bytes_rejects_bad_level():
    blob = _slot(level=0)

    def read(addr, n):
        return blob[:n]

    assert parse_party_bytes(read, 0x20000, 1) is None


def test_iter_aligned_qwords():
    val = 12345
    buf = b"\x00" * 8 + struct.pack("<q", val) + b"\x00" * 8
    assert list(iter_aligned_qwords(buf, 0x1000, val)) == [0x1008]
