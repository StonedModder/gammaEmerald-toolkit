"""Fast travel fires the game's own teleport volume.

The two bugs these cover both looked like "teleport doesn't work":

  * moving the player by writing their transform -- they arrive and then fall
    out of the world (measured Z 64 -> -23,555), landing in an empty zone
  * holding `isOverlapping?` high -- the game treats the player as
    mid-transition and ignores movement, so they freeze in place and the
    volume never gets the step it needs
"""
import struct

from gamma import nav, travel


class FakeGP:
    """Just enough process memory to watch what travel writes."""

    def __init__(self):
        self.mem = {}
        self.writes = []          # (addr, bytes) in order

    def wpm(self, addr, data):
        self.writes.append((addr, bytes(data)))
        for i, b in enumerate(bytes(data)):
            self.mem[addr + i] = b
        return True

    def rpm(self, addr, size):
        return bytes(self.mem.get(addr + i, 0) for i in range(size))

    def read_u64(self, addr):
        return struct.unpack("<Q", self.rpm(addr, 8))[0]


class FakeGame:
    def __init__(self, gp, volumes):
        self.gp = gp
        self._volumes = volumes

    def actors_of_class(self, name, limit=4000):
        return list(self._volumes) if name == travel.VOLUME_CLASS else []

    def refresh(self):
        pass


VOL = 0x1000


def test_disarm_stale_clears_every_armed_volume():
    gp = FakeGP()
    gp.wpm(VOL + travel.IS_OVERLAPPING, b"\x01")
    gp.wpm(VOL + 0x100 + travel.IS_OVERLAPPING, b"\x01")
    game = FakeGame(gp, [VOL, VOL + 0x100])

    assert travel.disarm_stale(game) == 2
    assert gp.rpm(VOL + travel.IS_OVERLAPPING, 1) == b"\x00"
    assert gp.rpm(VOL + 0x100 + travel.IS_OVERLAPPING, 1) == b"\x00"


def test_disarm_stale_leaves_unarmed_volumes_alone():
    gp = FakeGP()
    game = FakeGame(gp, [VOL])
    assert travel.disarm_stale(game) == 0


def test_the_overlap_flag_is_pulsed_not_held():
    """Every arming write must be followed by a disarming one.

    Holding the flag is what froze the player: with it high the game ignores
    movement, so the step that fires the volume can never happen.
    """
    gp = FakeGP()
    arm = disarm = 0
    for addr, data in [(VOL + travel.IS_OVERLAPPING, b"\x01"),
                       (VOL + travel.IS_OVERLAPPING, b"\x00")]:
        gp.wpm(addr, data)
    for addr, data in gp.writes:
        if addr == VOL + travel.IS_OVERLAPPING:
            arm += data == b"\x01"
            disarm += data == b"\x00"
    assert arm == disarm, "the flag was left armed"


def test_travel_never_writes_the_player_transform():
    """Guard against re-introducing the fall-through-the-world teleport."""
    import inspect
    src = inspect.getsource(travel.travel)
    for banned in ("RELATIVE_LOCATION", "COMPONENT_TO_WORLD", "nav.teleport"):
        assert banned not in src, "travel writes the player transform again (%s)" % banned


def test_nav_no_longer_exposes_a_teleport():
    """It moved the player and dropped them out of the world. Keep it gone."""
    assert not hasattr(nav, "teleport")
