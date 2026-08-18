"""Pure checks for the native cheat ports — no live game required."""
import struct

from gamma.encounter import MAX_REGIONAL_DEX, parse_dex, relative, resolve_pokemon
from gamma.money import iter_aligned_qwords


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


# The party record helpers that used to be tested here are gone: the party is
# read through UE reflection now, and tests/test_party.py covers it.


def test_iter_aligned_qwords():
    val = 12345
    buf = b"\x00" * 8 + struct.pack("<q", val) + b"\x00" * 8
    assert list(iter_aligned_qwords(buf, 0x1000, val)) == [0x1008]
