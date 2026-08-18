"""The wild shiny odds are a native denominator, not a Blueprint float.

The game rolls `shiny iff rand(0, N-1) == 0`, taking N from the stack:

    mov edi, [rsp+0x80]     <- the load this patch replaces with `mov edi, N`

The old odds control searched Blueprint bytecode for a 0.01 constant. On the
bug-fix build the only matches were two NPC "Footsteps" functions, so it
reported success while changing nothing about shininess.
"""
import struct

import pytest

from gamma.encounter import (WILD_ODDS_DEFAULT, WILD_ODDS_LOAD,
                             WILD_ODDS_WINDOW, EncounterHook)


class FakeGP:
    def __init__(self, code, base=0x140000000):
        self.module_base = base
        self.mem = bytearray(code)
        self.origin = base

    def rpm(self, addr, n):
        i = addr - self.origin
        if i < 0 or i + n > len(self.mem):
            return b""
        return bytes(self.mem[i:i + n])

    def wpm(self, addr, data):
        i = addr - self.origin
        self.mem[i:i + len(data)] = data
        return len(data)

    def write_code(self, addr, data):
        return self.wpm(addr, data)


def _hook(store_rva=0x1000):
    """A hook whose shiny_roll sits at store_rva, with the load above it."""
    size = store_rva + 0x40
    code = bytearray(b"\x90" * size)
    load_at = store_rva - 0x46
    code[load_at:load_at + len(WILD_ODDS_LOAD)] = WILD_ODDS_LOAD
    gp = FakeGP(bytes(code))
    hook = EncounterHook.__new__(EncounterHook)
    hook.gp = gp
    hook._offsets = {"shiny_roll": store_rva, "build": "test"}
    return hook, gp, gp.module_base + load_at


def test_the_denominator_load_is_found_below_the_store():
    hook, _gp, expected = _hook()
    assert hook.wild_odds_site() == expected


def test_unpatched_reports_the_games_own_rate():
    hook, _gp, _site = _hook()
    assert hook.wild_odds() == {"denominator": WILD_ODDS_DEFAULT, "patched": False}


@pytest.mark.parametrize("n", [1, 100, 4096, 65536])
def test_setting_odds_writes_a_mov_immediate_and_reads_back(n):
    hook, gp, site = _hook()
    hook.set_wild_odds(n)
    raw = gp.rpm(site, 7)
    assert raw[0] == 0xBF, "not a mov edi, imm32"
    assert struct.unpack("<I", raw[1:5])[0] == n
    assert raw[5:7] == b"\x90\x90", "the two spare bytes must be nops"
    assert hook.wild_odds() == {"denominator": n, "patched": True}


def test_the_patch_is_exactly_as_long_as_what_it_replaces():
    """A short write would leave half an instruction behind and crash the game."""
    hook, gp, site = _hook()
    before = len(gp.mem)
    hook.set_wild_odds(8)
    assert len(gp.mem) == before
    assert len(WILD_ODDS_LOAD) == 7


def test_clear_restores_the_original_load():
    hook, gp, site = _hook()
    hook.set_wild_odds(1)
    hook.clear_wild_odds()
    assert gp.rpm(site, 7) == WILD_ODDS_LOAD
    assert hook.wild_odds()["patched"] is False


def test_the_site_is_still_found_once_patched():
    """Reading the odds back must work after a patch, not only before one."""
    hook, _gp, site = _hook()
    hook.set_wild_odds(50)
    hook._odds_site = None
    assert hook.wild_odds_site() == site
    assert hook.wild_odds()["denominator"] == 50


@pytest.mark.parametrize("bad", [0, -1, 2 ** 31])
def test_nonsense_odds_are_refused(bad):
    hook, _gp, _site = _hook()
    with pytest.raises(ValueError):
        hook.set_wild_odds(bad)


def test_a_build_without_the_load_fails_loudly():
    hook, gp, site = _hook()
    gp.wpm(site, b"\x00" * 7)
    hook._odds_site = None
    with pytest.raises(RuntimeError):
        hook.wild_odds_site()
