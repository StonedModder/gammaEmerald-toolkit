"""Which route the wild panel reports.

Matching loaded levels alone kept the panel an area behind: walking from
Route 103 to Meteor Falls leaves five worlds resident, and first-match-wins
answered "Route_103" long after leaving. The game tracks it itself in
RouteSubsystem::CurrentRoute -- but keeps the last route when you walk
somewhere with no route data at all (Meteor Falls has none), so it is only
trusted while that route's own map is still loaded.
"""
import pytest

from gamma import wild


@pytest.mark.parametrize("route,worlds", [
    ("Route_115", ["MAP_Hoenn_Persistant", "MAP_Route115"]),
    ("Route_103", ["MAP_Route103"]),
    ("Route_PetalburgWoods", ["MAP_PetalburgWoods", "MAP_Rustboro"]),
])
def test_a_route_is_recognised_in_its_own_map(route, worlds):
    assert wild._same_place(route, worlds)


@pytest.mark.parametrize("route,worlds", [
    # the case that made the panel lie: still holding Route 115 in a cave
    ("Route_115", ["MAP_Hoenn_Persistant", "MAP_MeteorFalls"]),
    ("Route_103", ["MAP_Hoenn_Persistant", "MAP_Route115"]),
    ("Route_Olddale", []),
])
def test_a_route_whose_map_is_gone_is_not_where_you_are(route, worlds):
    assert not wild._same_place(route, worlds)


def test_route_115_does_not_match_route_1150():
    assert not wild._same_place("Route_115", ["MAP_Route1150"])


class FakeGP:
    def __init__(self, values):
        self.values = values

    def read_u64(self, addr):
        return self.values.get(addr, 0)


class FakeGame:
    def __init__(self, subsystems, values, names):
        self.gp = FakeGP(values)
        self._subs = subsystems
        self._names = names

    def actors_of_class(self, name, limit=4000):
        return list(self._subs) if name == wild.ROUTE_SUBSYSTEM else []

    def obj_name(self, obj):
        return self._names.get(obj)


def test_current_route_reads_the_subsystem():
    sub, route = 0x1000, 0x2000
    game = FakeGame([sub], {sub + wild.CURRENT_ROUTE: route}, {route: "Route_115"})
    assert wild.current_route(game) == ("Route_115", route)


def test_no_subsystem_means_no_answer():
    assert wild.current_route(FakeGame([], {}, {})) == (None, None)


def test_a_null_current_route_is_not_reported():
    sub = 0x1000
    game = FakeGame([sub], {sub + wild.CURRENT_ROUTE: 0}, {})
    assert wild.current_route(game) == (None, None)
