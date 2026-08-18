"""Per-build code offsets and the title-menu reader.

These are the two things that changed when the bug-fix update landed, and both
are easy to get subtly wrong in a way that only shows up against a live game --
so the parts that can be checked without one are checked here.
"""
import struct

import pytest

from gamma import screen
from gamma.encounter import BUILD_OFFSETS, EncounterHook


def test_every_build_has_the_same_offset_keys():
    needed = {"hook", "init", "get_by_dex", "shiny_roll", "shiny_rand"}
    for build, off in BUILD_OFFSETS.items():
        assert needed <= set(off), "%s is missing offsets" % build


def test_hook_bytes_are_a_call_to_that_build_s_init():
    """The stored constant only ever matched Early Access.

    A call encodes a distance, so the expected bytes have to be derived from
    each build's own hook/init pair. Getting this wrong rejected the bug-fix
    build, whose identical instruction reads E8 C6 1B 01 00.
    """
    for build, off in BUILD_OFFSETS.items():
        raw = EncounterHook.hook_orig(off)
        assert raw[0] == 0xE8, "%s: not a call" % build
        distance = struct.unpack("<i", raw[1:])[0]
        assert off["hook"] + 5 + distance == off["init"], build


def test_builds_do_not_share_a_hook_address():
    hooks = [o["hook"] for o in BUILD_OFFSETS.values()]
    assert len(hooks) == len(set(hooks))


def test_aug18_is_a_slide_of_the_bugfix_block():
    """The 2026-08-18 exe kept the Aug 17 layout and moved the whole block."""
    old = BUILD_OFFSETS["bugfix-2026-08-17"]
    new = BUILD_OFFSETS["ea-2026-08-18"]
    delta = new["hook"] - old["hook"]
    assert delta == 0x4A0
    for key in ("init", "get_by_dex", "shiny_roll", "shiny_rand"):
        assert new[key] - old[key] == delta, key
    assert new["shiny_roll"] - new["hook"] == 0x54
    assert new["shiny_rand"] - new["init"] == 0x31B


def _menu(width, height, block_top, row_height, highlight=None, rows=4):
    """A fake window: dark menu bars on a light background."""
    px = bytearray(b"\xC8" * (width * height * 4))
    for r in range(rows):
        top = block_top + r * row_height
        shade = 90 if highlight == r else 40
        for y in range(top + 2, top + row_height - 2):
            row = y * width * 4
            for x in range(0, int(width * 0.30)):
                i = row + x * 4
                px[i] = px[i + 1] = px[i + 2] = shade
    return bytes(px)


@pytest.mark.parametrize("w,h,top,row", [
    (1920, 1080, 146, 76),      # Early Access, as measured
    (4320, 2430, 31, 88),       # bug-fix build, as measured
    (1280, 720, 90, 60),        # a smaller window
    (2560, 1440, 200, 120),     # and a larger one
])
def test_menu_rows_found_at_any_size(w, h, top, row):
    rows = screen.rows_from_pixels(w, h, _menu(w, h, top, row))
    assert len(rows) == 4, "expected the four title entries, got %r" % (rows,)
    first = rows[0]
    centre = (first[0] + first[1]) // 2
    assert top <= centre <= top + row, "first row centre landed outside Continue"


def test_a_shape_that_is_not_the_menu_is_rejected():
    """Anything but four even rows must not be treated as the title menu.

    Acting on a stray dark shape is how the confirm key once reached Close Game.
    """
    w, h = 1920, 1080
    assert screen.rows_from_pixels(w, h, _menu(w, h, 146, 76, rows=2)) == [] or \
        len(screen.rows_from_pixels(w, h, _menu(w, h, 146, 76, rows=2))) != 4
    plain = bytes(b"\xC8" * (w * h * 4))
    assert screen.rows_from_pixels(w, h, plain) == []
