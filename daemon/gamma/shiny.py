"""Shiny odds scanning and patching.

The game stores its shiny probability as an EX_FloatConst operand inside Blueprint
bytecode (UStruct::Script). Patching that float changes the odds live, with no
restart -- which is exactly what makes a hunter bot testable: set the odds to 1/1
and a "successful hunt" happens on the first attempt instead of after 4096 of them.

VERIFIED on the EA build (UE 5.6):
  * Script is at UStruct+0x60 (unchanged from 5.3)
  * EX_FloatConst is still 0x1E (found `1e 00 00 80 3f` = opcode + 1.0f)
  * the odds constant is 0.01, present at 45 sites
  * 4,354 BlueprintGeneratedClass functions carry bytecode

The trap that cost me an hour: iterating UObjects and stopping early samples only
NATIVE engine functions, whose Script is empty, which looks exactly like "the offset
moved". Always filter on class_name(outer) == 'BlueprintGeneratedClass'.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, asdict


@dataclass
class Site:
    addr: int            # address of the 4 float bytes (operand, not opcode)
    value: float         # value currently there
    original: float      # value when first seen
    cls: str
    func: str

    def as_dict(self):
        d = asdict(self)
        d["addr"] = hex(self.addr)
        return d


def odds_text(p: float) -> str:
    """0.01 -> '1/100'. The display format asked for, and the honest one:
    a probability is much harder to eyeball than a denominator."""
    if p <= 0:
        return "0 (never)"
    if p >= 1.0:
        return "1/1 (guaranteed)"
    denom = 1.0 / p
    # snap to a whole denominator when we're within rounding noise of one
    r = round(denom)
    if r > 0 and abs(denom - r) / denom < 1e-4:
        return f"1/{r:,}"
    return f"1/{denom:,.1f}"


class ShinyEngine:
    def __init__(self, game, layout):
        self.game = game
        self.gp = game.gp
        self.L = layout
        self.sites: list[Site] = []

    # ------------------------------------------------------------------ scan
    def blueprint_functions(self, limit=0):
        g = self.game
        n = 0
        for _idx, o in g.iter_objects(0):
            if g.class_name(o) != "Function":
                continue
            h = g.obj_header(o)
            if not h or not h["outer"]:
                continue
            if g.class_name(h["outer"]) != "BlueprintGeneratedClass":
                continue
            yield o, h
            n += 1
            if limit and n >= limit:
                return

    def scan(self, tolerance=1e-9, extra_values=()) -> list[Site]:
        """Find every EX_FloatConst whose operand matches a known odds value."""
        L = self.L
        wanted = list(L.shiny_odds) + list(extra_values)
        found: list[Site] = []
        for o, h in self.blueprint_functions():
            ptr = self.gp.read_u64(o + L.struct_script)
            cnt = self.gp.read_u32(o + L.struct_script_count)
            if not ptr or not (0 < cnt < 200000):
                continue
            code = self.gp.rpm(ptr, cnt)
            if not code or len(code) != cnt:
                continue
            i = 0
            end = len(code) - 5
            while i < end:
                if code[i] == L.ex_float_const:
                    v = struct.unpack_from("<f", code, i + 1)[0]
                    for w in wanted:
                        if abs(v - w) <= tolerance * max(1.0, abs(w)):
                            found.append(Site(
                                addr=ptr + i + 1, value=v, original=v,
                                cls=self.game.obj_name(h["outer"]) or "?",
                                func=self.game.obj_name(o) or "?"))
                            break
                    i += 5
                else:
                    i += 1
        self.sites = found
        return found

    # ----------------------------------------------------------------- read
    def refresh(self) -> list[Site]:
        """Re-read each site so the UI shows what the game actually holds now."""
        for s in self.sites:
            raw = self.gp.rpm(s.addr, 4)
            if raw and len(raw) == 4:
                s.value = struct.unpack("<f", raw)[0]
        return self.sites

    def current_odds(self) -> float | None:
        """The odds the game is using, as a single number for the display.

        Sites can legitimately disagree (different species//methods), so report the
        most common value rather than pretending there is exactly one.
        """
        if not self.sites:
            return None
        self.refresh()
        counts: dict[float, int] = {}
        for s in self.sites:
            counts[round(s.value, 10)] = counts.get(round(s.value, 10), 0) + 1
        return max(counts.items(), key=lambda kv: kv[1])[0]

    # ---------------------------------------------------------------- patch
    def set_odds(self, probability: float, only=None) -> int:
        """Write `probability` to every (or a subset of) site. Returns count."""
        if not (0.0 <= probability <= 1.0):
            raise ValueError("probability must be in [0,1]")
        blob = struct.pack("<f", probability)
        n = 0
        for s in self.sites:
            if only is not None and s not in only:
                continue
            if self.gp.wpm(s.addr, blob) == 4:
                s.value = probability
                n += 1
        return n

    def force_shiny(self) -> int:
        return self.set_odds(1.0)

    def restore(self) -> int:
        """Put every site back to the value it had when first scanned."""
        n = 0
        for s in self.sites:
            if self.gp.wpm(s.addr, struct.pack("<f", s.original)) == 4:
                s.value = s.original
                n += 1
        return n

    def verify_roundtrip(self) -> tuple[bool, str]:
        """Write a probe value, read it back, restore. Proves we really can patch
        before a hunt relies on it."""
        if not self.sites:
            return False, "no sites"
        s = self.sites[0]
        before = s.value
        probe = 0.5
        if self.gp.wpm(s.addr, struct.pack("<f", probe)) != 4:
            return False, "write failed"
        raw = self.gp.rpm(s.addr, 4)
        got = struct.unpack("<f", raw)[0] if raw else None
        self.gp.wpm(s.addr, struct.pack("<f", before))
        ok = got is not None and abs(got - probe) < 1e-6
        return ok, f"wrote {probe}, read {got}, restored {before}"
