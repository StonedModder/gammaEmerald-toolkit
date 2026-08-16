"""Smoke that new modules import — PyInstaller hiddenimports depend on this."""
from gamma import nav, travel, wild, resources  # noqa: F401
from gamma.wild import WildHunter, BattleManager  # noqa: F401


def test_imports():
    assert nav.GRID == 64.0
    assert resources.find_tool("definitely-not-a-tool.exe") is None
