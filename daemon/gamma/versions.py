"""Per-build paths and format facts for the two Gamma Emerald builds.

  original  — UE 5.3, IoStore (Zen uasset), unencrypted
  ea        — Early Access, UE 5.6, .pak v11 (legacy uasset+uexp),
              pak *index* is AES-256 encrypted; the key is compiled in below

Nothing else in the toolkit hardcodes a build. In normal use you do not touch
these at all: point the app at the game's .exe (Choose game...) and `from_exe`
below derives everything from it, which is what makes this work on a machine
other than the author's.

The entries below are only a convenience for a default install layout. Set
GAMMA_ROOT to wherever the game folders live, or ignore them entirely.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path

# Remembered install, independent of Electron settings. The portable exe
# extracts to a new temp dir every launch, so the daemon cannot rely on cwd.
_SETTINGS_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "GammaToolkit"
_GAME_PATH_FILE = _SETTINGS_DIR / "game.json"

# Where the game folders live, if you keep them together. Overridable, and
# irrelevant once you have used Choose game... in the app.
ROOT = Path(os.environ.get("GAMMA_ROOT") or Path.home() / "Gamma Emerald")

# Oodle is needed only for Oodle-compressed paks. It ships with the engine and
# is NOT redistributed here -- see tools/README.md. Point GAMMA_OODLE_DLL at a
# copy, or drop one in tools/.
def _src_oodle():
    try:
        here = Path(__file__).resolve()
        if len(here.parents) > 2:
            return here.parents[2] / "tools" / "oodle-data-shared.dll"
    except (OSError, IndexError):
        pass
    return None


OODLE_CANDIDATES = [
    Path(os.environ["GAMMA_OODLE_DLL"]) if os.environ.get("GAMMA_OODLE_DLL") else None,
    _src_oodle(),
    ROOT / "re-tools" / "oodle-data-shared.dll",
]
OODLE_CANDIDATES = [p for p in OODLE_CANDIDATES if p is not None]


@dataclass(frozen=True)
class VersionSpec:
    id: str
    name: str
    engine: str
    game_dir: Path
    container: str          # "iostore" | "pak"
    asset_format: str       # "zen" | "legacy"
    extract_dir: Path
    viewer_dir: Path
    sha8: str
    notes: str = ""
    utoc: Path | None = None
    ucas: Path | None = None
    pak: Path | None = None
    exe: Path | None = None
    aes_key_env: str = ""
    aes_key_hex: str = ""
    viewer_index: str = "index.json"
    viewer_audio: str = "audio.json"
    viewer_sprites: str = "sprites"
    viewer_audio_dir: str = "audio"

    def content_root(self) -> Path:
        return self.extract_dir / "PokemonEmerald" / "Content"

    def find_aes_key(self, explicit: str | None = None) -> bytes | None:
        """32-byte AES key: explicit > env var > key file > built-in default.

        The built-in is last so a newer key can override it without a code change,
        but it IS present, so a shipped build decrypts the EA pak with zero setup.
        """
        if explicit:
            h = explicit.strip().removeprefix("0x")
            key = bytes.fromhex(h)
            if len(key) != 32:
                raise ValueError(f"AES key must be 32 bytes, got {len(key)}")
            return key
        if self.aes_key_env:
            env = os.environ.get(self.aes_key_env, "").strip()
            if env:
                return bytes.fromhex(env.removeprefix("0x"))
        # version-scoped only: an unencrypted build must resolve to None, not to
        # some other build's key, or a real failure gets masked later
        candidates = [ROOT / "re-tools" / f"{self.id}_aes.key"]
        if self.id == "ea":
            candidates.append(ROOT / "EarlyAccessUpdate" / "ea-AES.txt")
        for keyfile in candidates:
            if keyfile.exists():
                raw = keyfile.read_text().strip().removeprefix("0x")
                if raw:
                    return bytes.fromhex(raw)
        if self.aes_key_hex:
            return bytes.fromhex(self.aes_key_hex)
        return None


def find_oodle() -> Path:
    """Locate oodle-data-shared.dll, or explain where to put it.

    Uses the shared resolver so a packaged exe finds a DLL sitting beside it --
    the old version looked two directories above this source file, which does
    not exist once frozen. Also walks up from the saved game exe, because that
    is where re-tools/ lives on a machine that never put the DLL next to the app.
    """
    from . import resources
    p = resources.oodle()
    if p:
        return p
    extra: list[Path] = []
    if getattr(sys, "frozen", False):
        extra.append(Path(sys.executable).resolve().parent / "oodle-data-shared.dll")
    saved = _read_saved_exe()
    if saved:
        cur = Path(saved).resolve().parent
        for _ in range(6):
            extra.append(cur / "oodle-data-shared.dll")
            extra.append(cur / "re-tools" / "oodle-data-shared.dll")
            extra.append(cur / "tools" / "oodle-data-shared.dll")
            extra.append(cur / "gammaEmerald-toolkit" / "tools" / "oodle-data-shared.dll")
            if cur.parent == cur:
                break
            cur = cur.parent
    for cand in extra:
        if cand.is_file():
            return cand
    raise FileNotFoundError(
        "oodle-data-shared.dll not found. Put it next to the app (or in tools/), "
        "or set GAMMA_OODLE_DLL. See tools/README.md."
    )



VERSIONS: dict[str, VersionSpec] = {
    "original": VersionSpec(
        id="original",
        name="Original (2025-05-31)",
        engine="5.3.2-29314046",
        game_dir=ROOT / "Windows" / "PokemonEmerald",
        container="iostore",
        asset_format="zen",
        extract_dir=ROOT / "extracted",
        viewer_dir=ROOT / "viewer",
        sha8="4820665b",
        utoc=ROOT / "Windows" / "PokemonEmerald" / "Content" / "Paks" / "PokemonEmerald-Windows.utoc",
        ucas=ROOT / "Windows" / "PokemonEmerald" / "Content" / "Paks" / "PokemonEmerald-Windows.ucas",
        pak=ROOT / "Windows" / "PokemonEmerald" / "Content" / "Paks" / "PokemonEmerald-Windows.pak",
        exe=ROOT / "Windows" / "PokemonEmerald" / "Binaries" / "Win64" / "PokemonEmerald.exe",
        notes="IoStore Zen packages; unencrypted; Development + Shipping + PDB",
    ),
    "ea": VersionSpec(
        id="ea",
        name="Early Access (2026-08-15)",
        engine="5.6.0",
        game_dir=ROOT / "EarlyAccessUpdate" / "PokemonEmerald",
        container="pak",
        asset_format="legacy",
        extract_dir=ROOT / "extracted-ea",
        viewer_dir=ROOT / "viewer" / "ea",
        sha8="76ba746c",
        pak=ROOT / "EarlyAccessUpdate" / "PokemonEmerald" / "Content" / "Paks" / "PokemonEmerald-Windows.pak",
        exe=ROOT / "EarlyAccessUpdate" / "PokemonEmerald" / "Binaries" / "Win64" / "PokemonEmerald.exe",
        aes_key_env="GAMMA_EA_AES_KEY",
        # Recovered from the live process (see docs/botUpdaterPlan.md 7.1):
        # scanned RW memory for the raw 32-byte FAESKey, verified by decrypting
        # the pak index to mount point "../../../" and 404,134 entries.
        aes_key_hex="3615fd2021f60ad062b8b4d4895fcc581b0fdb2fc9172982129976cab1c4065d",
        notes="UE 5.6 pak v11, legacy uasset+uexp, encrypted index (AES-256 ECB)",
    ),
}


def get(version: str) -> VersionSpec:
    try:
        return VERSIONS[version]
    except KeyError as e:
        raise SystemExit(
            f"unknown version {version!r}; choose from: {', '.join(VERSIONS)}"
        ) from e


def save_game_exe(exe: str | None) -> None:
    _SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    _GAME_PATH_FILE.write_text(json.dumps({"exe": exe}, indent=2), encoding="utf-8")


def load_game_exe() -> str | None:
    for candidate in (
        os.environ.get("GAMMA_GAME_EXE", "").strip() or None,
        _read_saved_exe(),
    ):
        if candidate and Path(candidate).is_file():
            return candidate
    return None


def _read_saved_exe() -> str | None:
    try:
        data = json.loads(_GAME_PATH_FILE.read_text(encoding="utf-8"))
        return data.get("exe") or None
    except Exception:
        return None


def find_game_dir(exe) -> Path:
    """Locate the project folder that contains Content/Paks.

    Two layouts exist:

    - shipping / Win64:  …/PokemonEmerald/Binaries/Win64/PokemonEmerald.exe
      (Paks is three directories up)
    - UE launcher:       …/EarlyAccessUpdate/PokemonEmerald.exe sitting *beside*
      …/EarlyAccessUpdate/PokemonEmerald/Content/Paks
      Walking up never sees that sibling folder, which is why the portable
      build reported "could not read the pak" after Choose game picked the
      launcher.
    """
    exe = Path(exe).resolve()
    folders = []
    here = exe.parent
    for _ in range(10):
        folders.append(here)
        if here.parent == here:
            break
        here = here.parent
    folders.append(exe.parent / exe.stem)
    try:
        folders.extend(sorted(p for p in exe.parent.iterdir() if p.is_dir()))
    except OSError:
        pass
    seen: set[Path] = set()
    for folder in folders:
        folder = Path(folder)
        if folder in seen:
            continue
        seen.add(folder)
        if (folder / "Content" / "Paks").is_dir():
            return folder
    raise RuntimeError(
        f"no Content/Paks folder near {exe}. "
        "Pick PokemonEmerald.exe inside the game install "
        "(the launcher next to the PokemonEmerald folder is fine)."
    )


_SKIP_EXE = ("crash", "eac", "prereq", "redist", "unins", "easyanticheat")


def resolve_game_binary(chosen) -> Path:
    """Win64 game exe, not the sibling UE launcher stub.

    Launching EarlyAccessUpdate/PokemonEmerald.exe (the bootstrapper) from
    Electron's job object starts the game exclusive-fullscreen with a cwd that
    misses Saved/Config, so the hunt cannot see widgets or land clicks.
    Binaries/Win64/PokemonEmerald.exe with cwd=Win64 is the process the
    uncompiled app already attaches to.
    """
    chosen = Path(chosen)
    if not chosen.is_file():
        return chosen
    try:
        game_dir = find_game_dir(chosen)
    except RuntimeError:
        return chosen
    win64 = game_dir / "Binaries" / "Win64"
    if not win64.is_dir():
        return chosen
    exes = []
    for p in win64.glob("*.exe"):
        n = p.name.lower()
        if any(s in n for s in _SKIP_EXE):
            continue
        exes.append(p)
    if not exes:
        return chosen
    for p in exes:
        if p.name.lower() == "pokemonemerald.exe":
            return p
    for p in exes:
        if "shipping" in p.name.lower():
            return p
    return exes[0]


def spawn_game(exe) -> object:
    """Start the game outside Electron's job, windowed, from the Win64 folder."""
    import subprocess
    exe = resolve_game_binary(exe)
    cmd = [str(exe), "-windowed"]
    cwd = str(exe.parent)
    if os.name != "nt":
        return subprocess.Popen(cmd, cwd=cwd)
    flags_try = (
        0x00000008 | 0x00000200 | 0x01000000,  # DETACHED | NEW_GROUP | BREAKAWAY
        0x00000200 | 0x01000000,
        0x00000200,
        0,
    )
    last = None
    for flags in flags_try:
        try:
            return subprocess.Popen(cmd, cwd=cwd, creationflags=flags, close_fds=False)
        except OSError as e:
            last = e
    raise last


def resolve_launch_exe(explicit=None, stored=None, version: str = "ea") -> Path:
    """First existing file among the chosen path, a remembered path, then builtins."""
    for candidate in (explicit, stored, load_game_exe()):
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if path.is_file():
            return path
    spec = get(version)
    if spec.exe and Path(spec.exe).is_file():
        return Path(spec.exe)
    raise RuntimeError(
        "game exe not found. Use Choose game and pick "
        "PokemonEmerald.exe (…/Binaries/Win64/PokemonEmerald.exe)."
    )


def from_exe(exe) -> VersionSpec:
    """Build a spec for an install the user pointed at, wherever it lives.

    The built-in specs hardcode this machine's paths, which is fine for the
    author and useless for anybody else. A packaged UE game always looks like
    `<root>/<Project>/Binaries/Win64/<Project>.exe`, so the project directory
    and its Paks folder follow from the exe alone.

    The container identifies the build far more reliably than a version string:
    the original ships IoStore (.utoc/.ucas), the Early Access build ships a
    legacy .pak with an encrypted index. Everything else -- notably the AES key
    -- is inherited from whichever built-in spec matches.
    """
    exe = Path(exe)
    if not exe.is_file():
        raise RuntimeError(f"no such file: {exe}")
    game_dir = find_game_dir(exe)
    paks = game_dir / "Content" / "Paks"

    utoc = next(iter(sorted(paks.glob("*.utoc"))), None)
    pak = next(iter(sorted(paks.glob("*.pak"))), None)
    if utoc:
        base, container, fmt = VERSIONS["original"], "iostore", "zen"
    elif pak:
        base, container, fmt = VERSIONS["ea"], "pak", "legacy"
    else:
        raise RuntimeError(f"no .pak or .utoc in {paks}")

    return replace(
        base,
        id="custom",
        name=f"{game_dir.name} — {game_dir.parent.name}",
        game_dir=game_dir,
        container=container,
        asset_format=fmt,
        exe=exe,
        pak=pak,
        utoc=utoc,
        ucas=utoc.with_suffix(".ucas") if utoc else None,
    )


def add_version_arg(parser, default="original"):
    parser.add_argument(
        "--version", choices=list(VERSIONS), default=default,
        help="game build to read (default: %(default)s)",
    )
    return parser
