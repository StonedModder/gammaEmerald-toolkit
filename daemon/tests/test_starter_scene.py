"""Starter suitcase lookup must not walk 258k named objects on every bag open."""
import struct

from gamma.hunt import STARTER_SCENE_CLASS, StarterScene
from gamma.layouts import UE56


class FakeGame:
    def __init__(self, names, classes, objects=()):
        self._names = names
        self._classes = classes
        self._objects = objects
        self.gp = self
        self.iter_calls = 0

    def class_name(self, o):
        return self._classes.get(o)

    def obj_name(self, o):
        return self._names.get(o)

    def iter_objects(self, max_count=None):
        self.iter_calls += 1
        for i, o in enumerate(self._objects):
            yield i, o

    def actors_of_class(self, class_name, limit=0, skip_cdo=True):
        out = []
        for o in self._objects:
            if self._classes.get(o) != class_name:
                continue
            if skip_cdo and (self._names.get(o) or "").startswith("Default__"):
                continue
            out.append(o)
            if limit and len(out) >= limit:
                break
        return out


class MemGP:
    def __init__(self):
        self.b = {}

    def write(self, addr, data):
        self.b[addr] = bytes(data)

    def rpm(self, addr, n):
        blob = self.b.get(addr)
        if blob is not None:
            return blob[:n]
        for a, blob in self.b.items():
            if a <= addr < a + len(blob):
                chunk = blob[addr - a:addr - a + n]
                return chunk if len(chunk) == n else None
        return None

    def read_u64(self, a):
        r = self.rpm(a, 8)
        return struct.unpack("<Q", r)[0] if r and len(r) == 8 else 0

    def read_u32(self, a):
        r = self.rpm(a, 4)
        return struct.unpack("<I", r)[0] if r and len(r) == 4 else 0


def test_find_skips_cdo():
    game = FakeGame(
        names={1: "Default__BP_GE_PickStarterPlayer_C", 2: "BP_GE_PickStarterPlayer_C_0"},
        classes={1: STARTER_SCENE_CLASS, 2: STARTER_SCENE_CLASS},
        objects=(1, 2),
    )
    assert StarterScene.find(game).addr == 2


def test_find_in_level_skips_cdo_and_returns_live():
    layout = UE56
    level, arr, cdo, live = 0x10000, 0x20000, 0x30000, 0x40000
    gp = MemGP()
    gp.write(level + layout.level_actors, struct.pack("<Q", arr))
    gp.write(level + layout.level_actors_count, struct.pack("<I", 2))
    gp.write(arr, struct.pack("<QQ", cdo, live))
    game = FakeGame(
        names={cdo: "Default__BP_GE_PickStarterPlayer_C",
               live: "BP_GE_PickStarterPlayer_C_0"},
        classes={cdo: STARTER_SCENE_CLASS, live: STARTER_SCENE_CLASS},
    )
    game.gp = gp
    hit = StarterScene.find_in_level(game, layout, level)
    assert hit is not None
    assert hit.addr == live
    assert game.iter_calls == 0


def test_find_in_level_empty_actors():
    layout = UE56
    level = 0x10000
    gp = MemGP()
    gp.write(level + layout.level_actors, struct.pack("<Q", 0))
    gp.write(level + layout.level_actors_count, struct.pack("<I", 0))
    game = FakeGame(names={}, classes={})
    game.gp = gp
    assert StarterScene.find_in_level(game, layout, level) is None
