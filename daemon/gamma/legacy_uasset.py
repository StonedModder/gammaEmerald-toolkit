"""Cooked legacy uasset+uexp reader for the EA build (UE 5.6, magic 0x9E2A83C1).

Unversioned packages (FileVersionUE4/UE5/Licensee all 0) follow the 5.6
FPackageFileSummary layout: SavedHash (20) then TotalHeaderSize, then a
CustomVersion TArray, then the name/import/export maps. Names are FString + 4
hash bytes; FObjectImport is 32 bytes.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

PACKAGE_FILE_TAG = 0x9E2A83C1
PKG_FILTER_EDITOR_ONLY = 0x80000000
DEFAULT_FLIPBOOK_FPS = 15.0


def _fstring(b: bytes, pos: int) -> tuple[str, int]:
    ln = struct.unpack_from("<i", b, pos)[0]
    pos += 4
    if ln == 0:
        return "", pos
    if ln < 0:
        n = -ln
        s = b[pos:pos + n * 2].decode("utf-16-le", "replace").rstrip("\x00")
        return s, pos + n * 2
    if ln < 0 or pos + ln > len(b) or ln > 1_000_000:
        raise ValueError(f"bad FString length {ln} at {pos - 4}")
    s = b[pos:pos + ln].split(b"\x00")[0].decode("utf-8", "replace")
    return s, pos + ln


@dataclass
class ObjectImport:
    class_package: str
    class_name: str
    outer: int
    object_name: str


@dataclass
class LegacyPackage:
    names: list[str]
    imports: list[ObjectImport]
    total_header_size: int
    export_data: bytes


def parse_package(b: bytes) -> LegacyPackage | None:
    """Parse a concatenated .uasset+.uexp buffer. None if not legacy or corrupt."""
    if len(b) < 64 or struct.unpack_from("<I", b, 0)[0] != PACKAGE_FILE_TAG:
        return None
    legacy = struct.unpack_from("<i", b, 4)[0]
    if not (-9 <= legacy <= -2):
        return None
    pos = 8
    if legacy != -4:
        pos += 4  # LegacyUE3Version
    ue4 = struct.unpack_from("<i", b, pos)[0]
    pos += 4
    if legacy <= -8:
        pos += 4  # FileVersionUE5
    pos += 4  # FileVersionLicensee
    unversioned = ue4 == 0
    # UE 5.4+ PACKAGE_SAVED_HASH: 20-byte SHA then TotalHeaderSize, before custom versions.
    if unversioned:
        pos += 20
        if pos + 4 > len(b):
            return None
        ths = struct.unpack_from("<i", b, pos)[0]
        pos += 4
    else:
        ths = 0
    ncv = struct.unpack_from("<i", b, pos)[0]
    pos += 4
    if not (0 <= ncv < 64):
        return None
    pos += ncv * 20
    if not unversioned:
        ths = struct.unpack_from("<i", b, pos)[0]
        pos += 4
    try:
        _pkg, pos = _fstring(b, pos)
    except (ValueError, struct.error):
        return None
    flags = struct.unpack_from("<I", b, pos)[0]
    pos += 4
    name_count = struct.unpack_from("<i", b, pos)[0]
    name_offset = struct.unpack_from("<i", b, pos + 4)[0]
    pos += 8
    # SoftObjectPaths (UE5)
    pos += 8
    if not (flags & PKG_FILTER_EDITOR_ONLY):
        try:
            _, pos = _fstring(b, pos)
        except (ValueError, struct.error):
            return None
    pos += 8  # GatherableTextData
    _export_count = struct.unpack_from("<i", b, pos)[0]
    _export_offset = struct.unpack_from("<i", b, pos + 4)[0]
    import_count = struct.unpack_from("<i", b, pos + 8)[0]
    import_offset = struct.unpack_from("<i", b, pos + 12)[0]
    if not (0 < name_count < 100000 and 0 < name_offset < len(b)):
        return None
    if not (0 < import_count < 100000 and 0 < import_offset < len(b)):
        return None
    if not (0 < ths <= len(b)):
        return None

    names: list[str] = []
    npos = name_offset
    try:
        for _ in range(name_count):
            s, npos = _fstring(b, npos)
            npos += 4  # NAME_HASHES_SERIALIZED: two u16
            names.append(s)
    except (ValueError, struct.error):
        return None

    def fname(off: int) -> str:
        """An FName is (index, number), and the NUMBER carries a trailing _N.

        UE stores `Foo_3` as the name "Foo" with Number 4, so ignoring Number
        collapsed every sprite in a sheet -- Sprite_0 through Sprite_11 -- to the
        same string, and an atlas animation resolved to one frame repeated.
        """
        idx, number = struct.unpack_from("<ii", b, off)
        if not (0 <= idx < len(names)):
            return ""
        return names[idx] if number == 0 else "%s_%d" % (names[idx], number - 1)

    imports: list[ObjectImport] = []
    for i in range(import_count):
        o = import_offset + i * 32
        if o + 28 > len(b):
            return None
        imports.append(ObjectImport(
            class_package=fname(o),
            class_name=fname(o + 8),
            outer=struct.unpack_from("<i", b, o + 16)[0],
            object_name=fname(o + 20),
        ))

    return LegacyPackage(
        names=names,
        imports=imports,
        total_header_size=ths,
        export_data=b[ths:],
    )


def _resolve_sprite(pkg: LegacyPackage, pi: int) -> str | None:
    """FPackageIndex -> /Game/... sprite package path (with or without _Sprite)."""
    if pi >= 0:
        return None
    idx = -pi - 1
    if idx >= len(pkg.imports):
        return None
    imp = pkg.imports[idx]
    if imp.class_name == "PaperSprite" and imp.outer < 0:
        outer = pkg.imports[-imp.outer - 1]
        if outer.object_name.startswith("/Game/"):
            return outer.object_name
    if imp.object_name.startswith("/Game/"):
        return imp.object_name
    return None


def parse_paper_flipbook(b: bytes) -> tuple[float, list[str]] | None:
    """-> (fps, [sprite package path, ...]) expanded by FrameRun, or None."""
    pkg = parse_package(b)
    if pkg is None:
        return None
    ed = pkg.export_data
    if len(ed) >= 4 and struct.unpack_from("<I", ed, len(ed) - 4)[0] == PACKAGE_FILE_TAG:
        ed = ed[:-4]
    if len(ed) < 10:
        return None

    # Unversioned header is 2 or 3 bytes (zero-mask fragments), then optional
    # float FramesPerSecond, then KeyFrameCount. Scan a small window.
    fps = count = kf_off = None
    for c_off in range(0, 8):
        k_off = c_off + 4
        if len(ed) < k_off + 10:
            continue
        c = struct.unpack_from("<I", ed, c_off)[0]
        if not (0 < c < 10000):
            continue
        need = k_off + c * 10
        if not (need <= len(ed) <= need + 32):
            continue
        if struct.unpack_from("<H", ed, k_off)[0] != 0x0500:
            continue
        if c_off >= 4:
            cand = struct.unpack_from("<f", ed, c_off - 4)[0]
            fps = cand if 0.0 < cand <= 240.0 else DEFAULT_FLIPBOOK_FPS
        else:
            fps = DEFAULT_FLIPBOOK_FPS
        count, kf_off = c, k_off
        break
    if kf_off is None or count is None or fps is None:
        return None

    frames: list[str] = []
    for i in range(count):
        pi = struct.unpack_from("<i", ed, kf_off + i * 10 + 2)[0]
        run = struct.unpack_from("<I", ed, kf_off + i * 10 + 6)[0]
        path = _resolve_sprite(pkg, pi)
        if not path:
            return None
        frames.extend([path] * max(1, run))
    return fps, frames
