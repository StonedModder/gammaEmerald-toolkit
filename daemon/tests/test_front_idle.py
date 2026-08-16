"""Front Idle battle-sprite path ranking — used by the encounter picker."""
from gamma.assets import classify_front_idle, rank_front


MUDKIP = "PokemonEmerald/Content/SPRITES/POKEMON/WATER/Mudkip/FRONT/Idle/FB_Mudkip_Front_Idle.uasset"
MALE = "SPRITES/POKEMON/BUG/Beautifly/MALE/FRONT/Idle/FB_BeautiflyMale_Front_Idle.uasset"
FEMALE = "SPRITES/POKEMON/BUG/Beautifly/FEMALE/FRONT/Idle/FB_BeautiflyFemale_Front_Idle.uasset"
SHINY = "SPRITES/POKEMON/BUG/Beautifly/SHINY/MALE/FRONT/Idle/FB_BeautiflyMale_Front_Idle.uasset"
LOWHP = "SPRITES/POKEMON/WATER/Mudkip/FRONT/Idle/FB_Mudkip_Front_IdleLowHP.uasset"
CRY = "SPRITES/POKEMON/WATER/Mudkip/FRONT/Cry/FB_Mudkip_Front_Cry.uasset"


def test_ungendered_front_idle():
    hit = classify_front_idle(MUDKIP)
    assert hit is not None
    species, shiny, rank = hit
    assert species == "Mudkip"
    assert shiny is False
    assert rank == 0


def test_prefers_male_over_female():
    assert rank_front(MALE) < rank_front(FEMALE)


def test_shiny_flag_and_skips_lowhp_cry():
    species, shiny, _ = classify_front_idle(SHINY)
    assert species == "Beautifly" and shiny is True
    assert classify_front_idle(LOWHP) is None
    assert classify_front_idle(CRY) is None
