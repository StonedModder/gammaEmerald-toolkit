"""Save-file backup and restore.

WHERE THE SAVE LIVES, and how that was established: the game writes nothing to
the usual Unreal SaveGames folder, and there is no .sav anywhere on disk. The
path came out of the running process instead -- scanning its memory for wide
strings containing "AppData" turned up

    C:/Users/<user>/AppData/Local/PokemonEmerald/Saved/.ged/

a hidden folder holding a handful of .dat files with hashed names
(859c7fd1...dat and friends). Which file is which is not knowable from the
outside and the game rewrites several of them together, so the backup unit is
THE WHOLE FOLDER. Copying one file back on its own would risk mixing halves of
two different saves.

Restoring cannot be undone, so this module never overwrites a save without
first snapshotting what is being replaced (`auto` backups). The UI asks for
confirmation as well; this is the belt to that braces.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

SAVE_ENV = "GAMMA_SAVE_DIR"
BACKUP_ENV = "GAMMA_BACKUP_DIR"


def _local_appdata() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))


def save_dir() -> Path:
    """The folder the game keeps its save in."""
    override = os.environ.get(SAVE_ENV, "").strip().strip('"')
    if override:
        return Path(override)
    return _local_appdata() / "PokemonEmerald" / "Saved" / ".ged"


def backup_root() -> Path:
    override = os.environ.get(BACKUP_ENV, "").strip().strip('"')
    if override:
        return Path(override)
    return _local_appdata() / "GammaToolkit" / "save_backups"


def _folder_stats(folder: Path) -> tuple[int, int, float]:
    """(file count, total bytes, newest mtime)."""
    files = [p for p in folder.glob("*") if p.is_file()] if folder.is_dir() else []
    total = sum(p.stat().st_size for p in files)
    newest = max((p.stat().st_mtime for p in files), default=0.0)
    return len(files), total, newest


def info() -> dict:
    """What the live save looks like right now."""
    folder = save_dir()
    count, total, newest = _folder_stats(folder)
    return {
        "dir": str(folder),
        "exists": folder.is_dir(),
        "files": count,
        "bytes": total,
        "modified": newest or None,
        "backup_dir": str(backup_root()),
    }


def _writable(path: Path) -> bool:
    """True if this file can actually be replaced right now.

    Opening for append is the cheap way to ask Windows whether another process
    holds the file: it changes nothing, and raises PermissionError when the
    game has it open.
    """
    try:
        with path.open("ab"):
            return True
    except OSError:
        return False


def _meta_path(entry: Path) -> Path:
    return entry / "meta.json"


def _read_meta(entry: Path) -> dict:
    try:
        return json.loads(_meta_path(entry).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def list_backups() -> list[dict]:
    """Newest first. A backup is a folder holding `data/` plus meta.json."""
    root = backup_root()
    if not root.is_dir():
        return []
    out = []
    for entry in root.iterdir():
        data = entry / "data"
        if not data.is_dir():
            continue
        meta = _read_meta(entry)
        count, total, _newest = _folder_stats(data)
        out.append({
            "id": entry.name,
            "label": meta.get("label") or entry.name,
            "created": meta.get("created") or entry.stat().st_mtime,
            "auto": bool(meta.get("auto")),
            "files": count,
            "bytes": total,
        })
    out.sort(key=lambda b: b["created"], reverse=True)
    return out


def _copy_folder(src: Path, dst: Path) -> int:
    dst.mkdir(parents=True, exist_ok=True)
    n = 0
    for item in src.glob("*"):
        if item.is_file():
            shutil.copy2(item, dst / item.name)
            n += 1
    return n


def create(label: str = "", auto: bool = False) -> dict:
    """Snapshot the live save. Any number of these can coexist."""
    src = save_dir()
    if not src.is_dir():
        raise RuntimeError("no save folder found at %s" % src)
    files = [p for p in src.glob("*") if p.is_file()]
    if not files:
        raise RuntimeError("the save folder is empty; nothing to back up")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    entry = backup_root() / stamp
    # two backups in the same second must not collide
    suffix = 1
    while entry.exists():
        suffix += 1
        entry = backup_root() / ("%s-%d" % (stamp, suffix))

    copied = _copy_folder(src, entry / "data")
    meta = {
        "label": (label or "").strip() or time.strftime("%d %b %Y, %H:%M:%S"),
        "created": time.time(),
        "auto": bool(auto),
        "source": str(src),
        "files": copied,
    }
    _meta_path(entry).write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return {"id": entry.name, "label": meta["label"], "files": copied,
            "created": meta["created"], "auto": meta["auto"]}


def restore(backup_id: str, snapshot_first: bool = True) -> dict:
    """Replace the live save with a backup. NOT reversible on its own.

    The current save is snapshotted first (unless explicitly refused) so a
    mistaken restore is still recoverable -- restoring is the one action here
    that destroys data the user cannot get back any other way.
    """
    entry = backup_root() / backup_id
    data = entry / "data"
    if not data.is_dir():
        raise RuntimeError("no such backup: %s" % backup_id)
    files = [p for p in data.glob("*") if p.is_file()]
    if not files:
        raise RuntimeError("backup %s holds no files" % backup_id)

    dest = save_dir()
    dest.mkdir(parents=True, exist_ok=True)

    # PRE-FLIGHT. The running game holds its save files open, and Windows
    # refuses to delete or overwrite them -- a real PermissionError, not a
    # warning. Restore clears the folder before copying, so hitting a locked
    # file halfway would leave the save half-deleted: the worst possible
    # outcome for the one operation the user cannot undo. Check every file is
    # actually writable BEFORE touching any of them.
    locked = [p.name for p in dest.glob("*") if p.is_file() and not _writable(p)]
    if locked:
        raise RuntimeError(
            "close the game first — it still has %s open, and Windows will not "
            "let the save be replaced while it is running."
            % (locked[0] if len(locked) == 1 else "%d save files" % len(locked)))

    safety = None
    if snapshot_first and any(dest.glob("*")):
        safety = create(label="Before restoring %s" % _read_meta(entry).get(
            "label", backup_id), auto=True)

    # Clear the live folder first: the game names files by hash, so a stale file
    # from the replaced save would otherwise sit alongside the restored ones.
    removed = 0
    for item in dest.glob("*"):
        if item.is_file():
            item.unlink()
            removed += 1
    copied = _copy_folder(data, dest)
    return {"restored": backup_id, "files": copied, "removed": removed,
            "safety_backup": safety["id"] if safety else None}


def delete(backup_id: str) -> dict:
    entry = backup_root() / backup_id
    if not (entry / "data").is_dir():
        raise RuntimeError("no such backup: %s" % backup_id)
    shutil.rmtree(entry)
    return {"deleted": backup_id}
