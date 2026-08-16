"""Chosen-exe path resolution — the portable build must not depend on author paths."""
from __future__ import annotations

from pathlib import Path

import pytest

from gamma import versions


def _fake_install(root: Path, kind="pak"):
    game_dir = root / "PokemonEmerald"
    exe = game_dir / "Binaries" / "Win64" / "PokemonEmerald.exe"
    paks = game_dir / "Content" / "Paks"
    paks.mkdir(parents=True)
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    if kind == "pak":
        (paks / "PokemonEmerald-Windows.pak").write_bytes(b"pak")
    else:
        (paks / "PokemonEmerald-Windows.utoc").write_bytes(b"utoc")
        (paks / "PokemonEmerald-Windows.ucas").write_bytes(b"ucas")
    return exe


def test_from_exe_walks_up_to_paks(tmp_path):
    exe = _fake_install(tmp_path)
    spec = versions.from_exe(exe)
    assert spec.container == "pak"
    assert spec.exe == exe
    assert spec.game_dir == exe.parent.parent.parent


def test_from_exe_finds_pak_beside_ue_launcher(tmp_path):
    """Shipping layout: Foo.exe next to Foo/Content/Paks, not under Binaries/Win64."""
    launcher = tmp_path / "PokemonEmerald.exe"
    launcher.write_bytes(b"MZ")
    paks = tmp_path / "PokemonEmerald" / "Content" / "Paks"
    paks.mkdir(parents=True)
    pak = paks / "PokemonEmerald-Windows.pak"
    pak.write_bytes(b"pak")
    spec = versions.from_exe(launcher)
    assert spec.container == "pak"
    assert spec.pak == pak
    assert spec.game_dir == tmp_path / "PokemonEmerald"


def test_resolve_game_binary_prefers_win64_over_launcher(tmp_path):
    launcher = tmp_path / "PokemonEmerald.exe"
    launcher.write_bytes(b"MZ")
    win64 = tmp_path / "PokemonEmerald" / "Binaries" / "Win64"
    win64.mkdir(parents=True)
    real = win64 / "PokemonEmerald.exe"
    real.write_bytes(b"MZ")
    paks = tmp_path / "PokemonEmerald" / "Content" / "Paks"
    paks.mkdir(parents=True)
    (paks / "game.pak").write_bytes(b"pak")
    assert versions.resolve_game_binary(launcher) == real
    assert versions.resolve_game_binary(real) == real


def test_from_exe_rejects_random_file(tmp_path):
    stray = tmp_path / "notepad.exe"
    stray.write_bytes(b"MZ")
    with pytest.raises(RuntimeError, match="Content/Paks"):
        versions.from_exe(stray)


def test_resolve_launch_prefers_explicit(tmp_path, monkeypatch):
    monkeypatch.setattr(versions, "load_game_exe", lambda: None)
    exe = tmp_path / "game.exe"
    exe.write_bytes(b"MZ")
    got = versions.resolve_launch_exe(explicit=str(exe), stored=None, version="ea")
    assert got == exe


def test_resolve_launch_uses_stored_when_explicit_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(versions, "load_game_exe", lambda: None)
    exe = tmp_path / "game.exe"
    exe.write_bytes(b"MZ")
    got = versions.resolve_launch_exe(explicit=None, stored=str(exe), version="ea")
    assert got == exe


def test_resolve_launch_errors_when_nothing_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(versions, "load_game_exe", lambda: None)
    from dataclasses import replace
    monkeypatch.setitem(
        versions.VERSIONS, "ea",
        replace(versions.VERSIONS["ea"], exe=tmp_path / "missing.exe"),
    )
    with pytest.raises(RuntimeError, match="Choose game"):
        versions.resolve_launch_exe(explicit=None, stored=None, version="ea")


def test_save_and_load_game_exe(tmp_path, monkeypatch):
    exe = tmp_path / "PokemonEmerald.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(versions, "_GAME_PATH_FILE", tmp_path / "game.json")
    monkeypatch.setattr(versions, "_SETTINGS_DIR", tmp_path)
    monkeypatch.delenv("GAMMA_GAME_EXE", raising=False)
    versions.save_game_exe(str(exe))
    assert versions.load_game_exe() == str(exe)
