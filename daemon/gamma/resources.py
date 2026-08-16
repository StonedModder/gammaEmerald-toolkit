"""Finding bundled tools whether running from source or from a packaged exe.

Every path in this toolkit that pointed at a file next to the source tree broke
the moment it was frozen. PyInstaller gives you three different layouts:

  from source     <repo>/daemon/gamma/x.py  -> tools/ is two levels up
  onefile         __file__ lives in a temp _MEIPASS dir that is deleted on exit,
                  and the exe the user launched is sys.executable
  onedir          sys.executable sits beside an _internal/ folder

So a tool is looked for in all of them, plus PATH, plus an explicit environment
override. Nothing here raises on import: a missing optional tool must degrade to
a clear message at the point of use, not stop the daemon from starting.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def _candidate_roots() -> list[Path]:
    roots: list[Path] = []

    # packaged: next to the exe, and the onefile extraction dir.
    # Electron extraResources land in resources/ next to the app exe, while the
    # frozen daemon is resources/daemon/gamma-daemon.exe -- so tools/ is the
    # daemon's parent, not its own folder.
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        roots += [exe_dir, exe_dir / "tools", exe_dir / "_internal",
                  exe_dir / "_internal" / "tools", exe_dir.parent,
                  exe_dir.parent / "tools", exe_dir.parent.parent,
                  exe_dir.parent.parent / "tools"]
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            roots += [Path(meipass), Path(meipass) / "tools"]

    # from source: <repo>/daemon/gamma/ -> <repo>
    here = Path(__file__).resolve()
    for up in (2, 1, 3):
        if len(here.parents) > up:
            roots += [here.parents[up], here.parents[up] / "tools"]

    # and wherever the process was started from
    roots += [Path.cwd(), Path.cwd() / "tools"]

    seen, out = set(), []
    for r in roots:
        key = str(r).lower()
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def find_tool(*names: str, env: str | None = None, subdir: str | None = None) -> Path | None:
    """Locate a bundled or system tool. None if it is not installed.

    `env` names an environment variable holding an explicit path, which always
    wins so a user can point at their own copy.
    """
    if env:
        override = os.environ.get(env, "").strip().strip('"')
        if override and Path(override).is_file():
            return Path(override)

    for root in _candidate_roots():
        for name in names:
            for cand in ((root / subdir / name) if subdir else None, root / name):
                if cand is not None and cand.is_file():
                    return cand

    for name in names:                       # finally, anything on PATH
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def ffmpeg() -> str:
    """ffmpeg to run. Falls back to the bare name so the error names the tool."""
    p = find_tool("ffmpeg.exe", "ffmpeg", env="GAMMA_FFMPEG")
    return str(p) if p else "ffmpeg"


def have_ffmpeg() -> bool:
    return find_tool("ffmpeg.exe", "ffmpeg", env="GAMMA_FFMPEG") is not None


def binkadec() -> Path | None:
    return find_tool("binkadec.exe", env="GAMMA_BINKADEC", subdir="binkadec")


def oodle() -> Path | None:
    return find_tool("oodle-data-shared.dll", env="GAMMA_OODLE_DLL")
