"""Backup/restore against a throwaway save folder.

Restore is the one action a user cannot undo, so the things worth pinning down
are: it puts the exact bytes back, it does not leave files behind from the save
it replaced, and it snapshots what it is about to overwrite.
"""
import hashlib

import pytest

from gamma import saves


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    live = tmp_path / "live"
    live.mkdir()
    monkeypatch.setenv(saves.SAVE_ENV, str(live))
    monkeypatch.setenv(saves.BACKUP_ENV, str(tmp_path / "backups"))
    return live


def digest(folder):
    return {p.name: hashlib.md5(p.read_bytes()).hexdigest()
            for p in sorted(folder.glob("*"))}


def test_restore_puts_the_exact_save_back(sandbox):
    (sandbox / "a.dat").write_bytes(b"state one")
    (sandbox / "only-in-first.dat").write_bytes(b"gone later")
    before = digest(sandbox)

    backup = saves.create("state one")

    # the game saves again: content changes, one file goes, another appears
    (sandbox / "a.dat").write_bytes(b"state two")
    (sandbox / "only-in-first.dat").unlink()
    (sandbox / "new.dat").write_bytes(b"second save only")
    after_play = digest(sandbox)

    result = saves.restore(backup["id"])

    assert digest(sandbox) == before          # byte-exact
    assert "new.dat" not in digest(sandbox)   # no leftovers from the replaced save
    assert result["safety_backup"]            # and the overwrite is recoverable

    safety = saves.backup_root() / result["safety_backup"] / "data"
    assert digest(safety) == after_play


def test_backups_stack_up_and_delete_cleanly(sandbox):
    (sandbox / "a.dat").write_bytes(b"x")
    first = saves.create("one")
    second = saves.create("two")
    assert first["id"] != second["id"]        # same second must not collide
    assert len(saves.list_backups()) == 2

    saves.delete(first["id"])
    assert [b["id"] for b in saves.list_backups()] == [second["id"]]


def test_refuses_when_there_is_nothing_to_back_up(sandbox):
    with pytest.raises(RuntimeError):
        saves.create("empty")


def test_restore_rejects_an_unknown_backup(sandbox):
    (sandbox / "a.dat").write_bytes(b"x")
    with pytest.raises(RuntimeError):
        saves.restore("20990101-000000")
