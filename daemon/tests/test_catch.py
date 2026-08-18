"""Guaranteed-catch field lists — no live game required."""
from gamma.catch import BOOL_FIELDS, INT_FIELDS, INT_VALUE


def test_catch_rate_is_the_auto_catch_threshold():
    assert INT_VALUE == 255
    assert "CatchRate" in INT_FIELDS
    assert "BallModifier" in INT_FIELDS
    assert "ClampCatchRate" not in INT_FIELDS


def test_result_flags_are_the_names_from_the_catching_library():
    assert "CatchResult" in BOOL_FIELDS
    assert "OutCatchResult" in BOOL_FIELDS
    assert "bCaught" in BOOL_FIELDS
