"""On-demand asset browsing, preview and extraction.

Nothing is unpacked up front. The pak index is read once (404,134 entries for the
EA build) and individual files are decoded only when you actually look at them, so
browsing costs nothing and previewing costs one read.

  sprites    cooked Texture2D -> PNG (raw BGRA at the end of the package)
  animations a UPaperFlipbook's frames -> animated GIF, built with ffmpeg
  audio      Bink Audio 2 -> WAV via binkadec -> MP3 via ffmpeg

GIFs go through ffmpeg's palettegen/paletteuse so transparency and colour survive;
hand-rolling a quantiser here would be worse and slower.
"""
from __future__ import annotations

import base64
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

from . import versions
from .versions import find_oodle
from .legacy_uasset import parse_package, parse_paper_flipbook

BINKADEC = Path(__file__).resolve().parents[2] / "tools" / "binkadec" / "binkadec.exe"
FFMPEG = "ffmpeg"

# Legacy cooked packages end with this tag. It sits AFTER the inline pixel data,
# so any tail slice has to drop it first.
PACKAGE_TAG = bytes((0xC1, 0x83, 0x2A, 0x9E))

# Categories the UI offers. Order is deliberate: the things people actually browse
# first, not alphabetical.
#
# `depth` is how many folders below the prefix name one *subject* -- one Pokemon,
# one trainer, one building. Pokemon are filed under a type folder
# (POKEMON/BUG/Beautifly) so they sit two deep; everything else is one. Sound
# folders are flat, so each file is its own subject.
CATEGORIES = [
    ("pokemon",   "Pokemon",    "SPRITES/POKEMON/",   2, "sprite"),
    ("trainers",  "Trainers",   "SPRITES/TRAINERS/",  1, "sprite"),
    ("npc",       "NPCs",       "SPRITES/NPC/",       1, "sprite"),
    ("brendan",   "Player",     "SPRITES/BRENDAN/",   1, "sprite"),
    ("items",     "Items",      "SPRITES/ITEMS/",     1, "sprite"),
    ("pokeballs", "Poke Balls", "SPRITES/POKEBALLS/", 1, "sprite"),
    ("ui",        "UI",         "SPRITES/UI/",        1, "sprite"),
    ("fx",        "Effects",    "SPRITES/FX/",        1, "sprite"),
    ("buildings", "Buildings",  "MODELS/BUILDINGS/",  1, "model"),
    ("maps",      "Maps",       "MODELS/MAPS/",       1, "model"),
    ("cries",     "Cries",      "SOUNDS/CRIES/",      0, "audio"),
    ("music",     "Music",      "SOUNDS/OST/",        0, "audio"),
    ("sfx",       "Sound FX",   "SOUNDS/SFX/",        0, "audio"),
]

# Prefixes Unreal's cooker gives each asset kind. Used to tell a previewable
# texture from a mesh or a Blueprint without parsing the package.
TEXTURE_PREFIX = ("SPR_", "T_", "TEX_", "UI_")
AUDIO_PREFIX = ("SND_", "SFX_", "MUS_")
SKIP_PREFIX = ("BP_", "M_", "MI_", "MAT_", "ABP_", "AS_", "DT_", "E_", "S_")


def _run(cmd, **kw):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          **kw)


class AssetLibrary:
    """Reads either build's container and serves individual assets."""

    def __init__(self, version="ea"):
        self.version = version
        self.spec = versions.get(version)
        self._reader = None
        self._paths = None
        self._entries = None
        self._by_dir = None

    # ------------------------------------------------------------------ open
    def reader(self):
        if self._reader is not None:
            return self._reader
        if self.spec.container == "pak":
            from .pak import PakReader
            from .iostore import Oodle
            # Oodle is not redistributed with this repo (it is Epic/RAD
            # proprietary -- see tools/README.md). Browsing the index does not
            # need it, so a missing DLL must not stop the library from opening:
            # it degrades to "you can list everything, previews explain what to
            # install" rather than failing outright.
            oodle = None
            try:
                oodle = Oodle(str(find_oodle()))
            except Exception as e:
                print("Oodle not available (%s); previews of compressed assets "
                      "will explain what to install" % e, file=sys.stderr)
            self._reader = PakReader(self.spec.pak,
                                     aes_key=self.spec.find_aes_key(),
                                     oodle=oodle)
        else:
            from .iostore import IoStoreReader
            self._reader = IoStoreReader(self.spec.utoc, self.spec.ucas)
        return self._reader

    def _index(self):
        """path -> entry, built once. Walking 404k entries per read cost ~3s a
        preview; the map turns every later read into a dict lookup."""
        if self._entries is None:
            self._entries = dict(self.reader().iter_files())
            self._paths = sorted(self._entries)
        return self._entries

    def paths(self):
        """Every path in the container, cached. One index read, not an extract."""
        self._index()
        return self._paths

    def read(self, path: str) -> bytes:
        """Raw bytes of one entry, decompressed and decrypted."""
        entry = self._index().get(path)
        if entry is None:
            raise KeyError(path)
        return self.reader().read_entry(entry)

    def read_many(self, wanted) -> dict:
        out = {}
        for p in wanted:
            try:
                out[p] = self.read(p)
            except Exception:
                pass
        return out

    # ------------------------------------------------------------- listing
    def _cat(self, cid):
        for row in CATEGORIES:
            if row[0] == cid:
                return row
        raise KeyError(cid)

    def categories(self):
        paths = self.paths()
        out = []
        for cid, label, prefix, depth, kind in CATEGORIES:
            n = sum(1 for p in paths if prefix in p)
            if n:
                out.append({"id": cid, "label": label, "prefix": prefix,
                            "kind": kind, "count": n})
        return out

    def subjects(self, category: str, query: str = "", limit: int = 500):
        """The named things in a category: a Pokemon, a trainer, a building.

        Listing raw files is useless here -- Pokemon alone is 355k entries, one per
        animation frame. A subject is the folder a person would name.
        """
        cid, label, prefix, depth, kind = self._cat(category)
        rx = re.compile(re.escape(query), re.I) if query else None
        subs = {}
        for p in self.paths():
            i = p.find(prefix)
            if i < 0:
                continue
            rest = p[i + len(prefix):]
            if depth == 0:                      # flat sound folder: file == subject
                fn = rest.rsplit("/", 1)[-1]
                if not fn.lower().endswith(".uasset"):
                    continue
                if not fn.upper().startswith(AUDIO_PREFIX):
                    continue
                name = fn[:-len(".uasset")]
                if rx and not rx.search(name):
                    continue
                subs[name] = {"id": p, "name": name, "kind": "audio", "count": 1}
                continue
            parts = rest.split("/")
            d = depth
            if parts[0].upper() == "FOLLOWERS":   # POKEMON/FOLLOWERS/<TYPE>/<Name>
                d += 1
            if len(parts) <= d:
                continue
            name = parts[d - 1]
            if rx and not rx.search(name):
                continue
            e = subs.get(name)
            if e is None:
                e = subs[name] = {
                    "id": p[:i + len(prefix)] + "/".join(parts[:d]),
                    "name": name, "kind": kind, "count": 0,
                    "group": parts[d - 2] if d > 1 else "",
                }
            e["count"] += 1

        # Some categories have no folder per subject -- Items is 12 loose files
        # sitting directly in SPRITES/ITEMS -- and would otherwise come back empty.
        if not subs and not query:
            for p in self.paths():
                i = p.find(prefix)
                if i < 0 or "/" in p[i + len(prefix):]:
                    continue
                root = p[:i + len(prefix)].rstrip("/")
                subs[label] = {"id": root, "name": label, "kind": kind,
                               "count": 0, "group": ""}
                subs[label]["count"] += 1

        rows = sorted(subs.values(), key=lambda e: e["name"].lower())
        return {"total": len(rows), "subjects": rows[:limit]}

    def search(self, query: str, limit: int = 400):
        """Find a subject in ANY category, in one pass over the index.

        Searching inside the selected category only meant typing a name you knew
        was in the game and being told it does not exist -- "Fisherman" is an NPC,
        not a Pokemon, and nothing said so.
        """
        if not query:
            return {"total": 0, "subjects": []}
        rx = re.compile(re.escape(query), re.I)
        subs = {}
        for p in self.paths():
            for cid, label, prefix, depth, kind in CATEGORIES:
                i = p.find(prefix)
                if i < 0:
                    continue
                rest = p[i + len(prefix):]
                if depth == 0:
                    fn = rest.rsplit("/", 1)[-1]
                    if not fn.lower().endswith(".uasset"):
                        break
                    if not fn.upper().startswith(AUDIO_PREFIX):
                        break
                    name = fn[:-len(".uasset")]
                    if rx.search(name):
                        subs[(cid, name)] = {"id": p, "name": name, "kind": "audio",
                                             "count": 1, "group": label}
                    break
                parts = rest.split("/")
                d = depth + (1 if parts[0].upper() == "FOLLOWERS" else 0)
                if len(parts) <= d:
                    break
                name = parts[d - 1]
                if rx.search(name):
                    e = subs.get((cid, name))
                    if e is None:
                        e = subs[(cid, name)] = {
                            "id": p[:i + len(prefix)] + "/".join(parts[:d]),
                            "name": name, "kind": kind, "count": 0, "group": label}
                    e["count"] += 1
                break
        # exact-ish matches first, then by name
        rows = sorted(subs.values(),
                      key=lambda e: (e["name"].lower() != query.lower(),
                                     not e["name"].lower().startswith(query.lower()),
                                     e["name"].lower()))
        return {"total": len(rows), "subjects": rows[:limit]}

    def entries(self, subject_dir: str):
        """What you can actually look at inside a subject.

        Three kinds come back: `animation` (a folder of SPR_ frames -> GIF),
        `image` (a lone texture -> PNG) and `audio` (a sound wave -> MP3). Meshes,
        materials and Blueprints have no preview, so they are listed as `raw` and
        can only be extracted.
        """
        pre = subject_dir.rstrip("/") + "/"
        anims, images, audio, raw = [], [], [], []
        frame_dirs, fb_dirs = {}, set()

        for p in self.paths():
            if not p.startswith(pre) or not p.lower().endswith(".uasset"):
                continue
            d, _, fn = p.rpartition("/")
            up = fn.upper()
            rel = p[len(pre):-len(".uasset")]

            if up.startswith("FB_"):
                # A flipbook IS the animation: it owns the frame order, the frame
                # runs and the FPS. Folder listings get all three wrong, and for
                # the overworld sprites there is no folder of frames at all -- the
                # frames are sub-rectangles of one sheet.
                fb_dirs.add(d)
                anims.append({"kind": "animation", "id": p,
                              "name": rel[3:] if rel.upper().startswith("FB_")
                                      else fn[3:-len(".uasset")],
                              "count": 0})
            elif up.endswith("_SPRITE.UASSET") or re.search(r"_SPRITE_\d+\.UASSET$", up):
                continue                      # UPaperSprite wrappers, not textures
            elif up.startswith("SPR_"):
                frame_dirs.setdefault(d, []).append(p)
            elif up.startswith(AUDIO_PREFIX):
                audio.append({"kind": "audio", "id": p, "name": fn[:-7], "count": 1})
            elif up.startswith(TEXTURE_PREFIX):
                images.append({"kind": "image", "id": p, "name": fn[:-7], "count": 1})
            elif not up.startswith(SKIP_PREFIX):
                raw.append({"kind": "raw", "id": p, "name": fn[:-7], "count": 1})

        # Folders of numbered frames with no flipbook (the battle sprites) are
        # still animations. Group by stem first: sharing a folder is not enough --
        # SPRITES/ITEMS holds three unrelated item icons, and treating the folder
        # as one animation played a book, a box and a heart in sequence.
        for d, frames in frame_dirs.items():
            if d in fb_dirs:
                continue
            stems = {}
            for f in frames:
                fn = f.rsplit("/", 1)[-1][:-len(".uasset")]
                # trailing frame number, with or without a separator:
                # SPR_AromaLady_Idle_01 and SPR_Greatball10 are both frame 
                # sequences, and requiring the underscore split every Poke Ball
                # animation into a pile of unrelated single images
                stems.setdefault(re.sub(r"_?\d+$", "", fn), []).append(f)
            folder = d[len(pre):] or subject_dir.rsplit("/", 1)[-1]
            multi = len(stems) > 1
            for stem, group in stems.items():
                name = ("%s/%s" % (folder, stem)) if multi and folder else (folder or stem)
                if len(group) > 1:
                    # "<dir>|<stem>" when a folder holds more than one sequence,
                    # so each plays its own frames instead of all of them
                    anims.append({"kind": "animation",
                                  "id": d if not multi else (d + "|" + stem),
                                  "name": name, "count": len(group)})
                    continue
                # A lone SPR_ file is usually a texture, but not always: some
                # flipbooks are named SPR_ rather than FB_ (SPR_GreatballOpen,
                # SPR_MagmaSherry_Idle_Down). Classifying on the prefix alone
                # showed those as broken images, so ask the package itself.
                fb = self.flipbook(group[0])
                if fb and fb[1]:
                    anims.append({"kind": "animation", "id": group[0],
                                  "name": stem if multi else name,
                                  "count": len(fb[1])})
                else:
                    images.append({"kind": "image", "id": group[0],
                                   "name": stem if multi else name, "count": 1})

        rows = (sorted(anims, key=lambda e: e["name"])
                + sorted(images, key=lambda e: e["name"])[:400]
                + sorted(audio, key=lambda e: e["name"])
                + sorted(raw, key=lambda e: e["name"]))

        # Cosmetic: the cooker's prefixes and the repeated subject name are noise
        # in a list that already sits under that subject's heading.
        subject = subject_dir.rstrip("/").rsplit("/", 1)[-1]
        for e in rows:
            n = re.sub(r"^(SPR_|FB_|T_|SND_)", "", e["name"])
            n = re.sub(r"^%s[/_]" % re.escape(subject), "", n, flags=re.I)
            n = re.sub(r"^(SPR_|FB_)", "", n)
            e["name"] = n or e["name"]
        return {"dir": subject_dir, "entries": rows}

    def files_in(self, directory: str, limit: int = 4000):
        pre = directory.rstrip("/") + "/"
        out = [p for p in self.paths() if p.startswith(pre) and "/" not in p[len(pre):]]
        return out[:limit]

    # ------------------------------------------------------------- decoding
    @staticmethod
    def texture_from_package(blob: bytes):
        """(w, h, BGRA) from a cooked Texture2D, or None.

        Same heuristic the sprite pipeline uses: dimensions sit immediately before
        the PF_B8G8R8A8 format string and the pixels are the tail of the package.
        """
        best = None
        start = 0
        while True:
            pos = blob.find(b"PF_B8G8R8A8", start)
            if pos < 0:
                break
            if pos >= 24:
                w = struct.unpack_from("<I", blob, pos - 16)[0]
                h = struct.unpack_from("<I", blob, pos - 12)[0]
                if 4 <= w <= 4096 and 4 <= h <= 4096 and w * h * 4 <= len(blob):
                    best = (w, h)
            start = pos + 1
        if not best:
            return None
        w, h = best
        return w, h, blob[-(w * h * 4):]

    # ------------------------------------------------------ block compression
    # Not every texture is raw BGRA -- the effects and some player sheets are
    # cooked to DXT. Rather than hand-roll a BC decoder (the first attempt, which
    # produced recognisable shapes in rainbow noise), wrap the payload in a DDS
    # header and hand it to ffmpeg, which is already a dependency and does this
    # in C.
    FOURCC = {b"PF_DXT1": b"DXT1", b"PF_DXT5": b"DXT5"}

    @classmethod
    def dds_bytes(cls, w: int, h: int, payload: bytes, fmt: bytes) -> bytes | None:
        four = cls.FOURCC.get(fmt)
        if not four:
            return None
        head = bytearray(128)
        head[0:4] = b"DDS "
        struct.pack_into("<I", head, 4, 124)                    # dwSize
        struct.pack_into("<I", head, 8, 0x1 | 0x2 | 0x4 | 0x1000 | 0x80000)
        struct.pack_into("<I", head, 12, h)
        struct.pack_into("<I", head, 16, w)
        struct.pack_into("<I", head, 20, len(payload))          # dwLinearSize
        struct.pack_into("<I", head, 28, 1)                     # dwMipMapCount
        struct.pack_into("<I", head, 76, 32)                    # pixelformat size
        struct.pack_into("<I", head, 80, 0x4)                   # DDPF_FOURCC
        head[84:88] = four
        struct.pack_into("<I", head, 108, 0x1000)               # DDSCAPS_TEXTURE
        return bytes(head) + payload

    @classmethod
    def dxt_offset(cls, w: int, h: int, data: bytes, fmt: bytes, slack: int) -> int:
        """Where the base mip actually starts inside an inline payload.

        Compressed pixels sit near the FRONT of the .uexp, after a small export
        header, with the trailing bulk info and package tag behind them -- so
        slicing the tail (which is right for a .ubulk) lands mid-block and decodes
        as recognisable shapes in rainbow noise. The header size varies, so the
        start is found rather than assumed: blocks are 16 bytes, so only the 16
        possible alignments need testing, scored on a decoded top strip.
        """
        need = cls._payload_size(w, h, fmt)
        strip = min(h, 64)
        best, best_off = None, 0
        for off in range(0, min(16, slack + 1)):
            chunk = data[off:off + need]
            if len(chunk) < cls._payload_size(w, strip, fmt):
                continue
            r = cls.decode_dxt(w, strip, chunk[:cls._payload_size(w, strip, fmt)],
                               fmt, _align=False)
            if not r:
                continue
            px = r[2]
            # noise scores high: compare horizontally adjacent pixels
            tot = n = 0
            for y in range(0, strip, 7):
                row = y * w * 4
                for x in range(0, (w - 1) * 4, 64):
                    tot += abs(px[row + x] - px[row + x + 4])
                    n += 1
            score = tot / max(1, n)
            if best is None or score < best:
                best, best_off = score, off
        return best_off

    @staticmethod
    def bgra_offset(w: int, h: int, data: bytes, slack: int) -> int:
        """Where the BGRA image starts inside an inline payload.

        Scored on wrap-around: if the slice is off by n pixels, the strip that
        falls off one side reappears on the other, so the left and right border
        columns both light up. A correctly framed sprite has quiet borders.

        Ties keep the LAST candidate, which is the tail -- the historical
        behaviour, and the right answer for a texture that fills its own edges
        (a UI panel), where every offset scores the same.
        """
        if slack <= 0:
            return max(0, slack)
        # Candidates must stay congruent to the tail modulo 4. Stepping from zero
        # instead broke the B/G/R/A alignment and every sprite came out purple.
        best, best_off = None, slack
        for k in range(0, 129):
            off = slack - 4 * k
            if off < 0:
                break
            edge = 0
            for y in range(0, h, max(1, h // 24)):
                row = off + y * w * 4
                if row + w * 4 > len(data):
                    break
                edge += data[row + 3]                 # leftmost pixel alpha
                edge += data[row + (w - 1) * 4 + 3]   # rightmost pixel alpha
            if best is None or edge < best:
                best, best_off = edge, off
        return best_off

    @staticmethod
    def _payload_size(w: int, h: int, fmt: bytes) -> int:
        per = 8 if fmt == b"PF_DXT1" else 16
        return max(1, (w + 3) // 4) * max(1, (h + 3) // 4) * per

    @classmethod
    def decode_dxt(cls, w: int, h: int, payload: bytes, fmt: bytes, _align=False):
        """DXT1/DXT5 -> (w, h, BGRA) via ffmpeg, or None."""
        dds = cls.dds_bytes(w, h, payload, fmt)
        if not dds:
            return None
        tmp = Path(tempfile.mkdtemp(prefix="gamma_dds_"))
        src, dst = tmp / "t.dds", tmp / "t.raw"
        try:
            src.write_bytes(dds)
            r = _run([FFMPEG, "-hide_banner", "-v", "error", "-y", "-i", str(src),
                      "-f", "rawvideo", "-pix_fmt", "bgra", str(dst)])
            if r.returncode != 0 or not dst.exists():
                return None
            raw = dst.read_bytes()
            need = w * h * 4
            return (w, h, raw[:need]) if len(raw) >= need else None
        finally:
            for f in (src, dst):
                if f.exists():
                    f.unlink()
            try:
                tmp.rmdir()
            except OSError:
                pass

    @staticmethod
    def png_from_bgra(w, h, bgra) -> bytes:
        ba = bytearray(bgra)
        ba[0::4], ba[2::4] = ba[2::4], ba[0::4]      # BGRA -> RGBA
        stride = w * 4
        raw = b"".join(b"\x00" + bytes(ba[y * stride:(y + 1) * stride])
                       for y in range(h))

        def chunk(tag, data):
            return (struct.pack(">I", len(data)) + tag + data
                    + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

        return (b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
                + chunk(b"IEND", b""))

    def sprite_png(self, path: str) -> bytes | None:
        t = self.texture(path)
        return self.png_from_bgra(*t) if t else None

    def sprite_source(self, path: str, blob: bytes | None = None) -> str | None:
        """A UPaperSprite's underlying Texture2D package, or None.

        Some assets named SPR_ are sprite wrappers, not textures -- SPR_B_Normal
        points at SPR_cursor_fight. Reported as broken until this followed the
        reference.
        """
        try:
            pkg = parse_package(blob if blob is not None
                                else self._package_bytes(path))
        except Exception:
            return None
        if not pkg:
            return None
        for imp in pkg.imports:
            if imp.class_name != "Texture2D":
                continue
            outer = imp.outer
            if outer >= 0:
                continue
            owner = pkg.imports[-outer - 1].object_name
            if not owner.startswith("/Game/"):
                continue
            root = self._content_root(path)
            if not root:
                return None
            cand = root + owner[len("/Game/"):] + ".uasset"
            if cand != path and cand in self._index():
                return cand
        return None

    def texture(self, path: str, blobs: dict | None = None):
        """(w, h, BGRA) for a cooked Texture2D in the legacy layout.

        Dimensions live in the header/exports, but the PIXELS are in a separate
        .ubulk -- legacy cooking moves bulk data out of the package. Reading only
        the .uasset/.uexp finds the format string and no pixels, which is why the
        first pass decoded nothing.
        """
        def grab(pp):
            if blobs is not None:
                return blobs.get(pp)
            try:
                return self.read(pp)
            except KeyError:
                return None

        stem = path[:-len(".uasset")] if path.lower().endswith(".uasset") else path
        head = grab(stem + ".uasset") or b""
        exp = grab(stem + ".uexp") or b""
        meta = head + exp
        # Which pixel format, and how big. Effects and a few player sheets are
        # cooked to DXT rather than raw BGRA; looking only for PF_B8G8R8A8 found
        # no dimensions at all and the preview came back empty.
        bulk = grab(stem + ".ubulk") or b""
        have = max(len(bulk), len(meta))
        dims = fmt = None
        for tag in (b"PF_B8G8R8A8", b"PF_DXT5", b"PF_DXT1", b"PF_BC5"):
            start = 0
            while True:
                pos = meta.find(tag, start)
                if pos < 0:
                    break
                if pos >= 24:
                    w = struct.unpack_from("<I", meta, pos - 16)[0]
                    h = struct.unpack_from("<I", meta, pos - 12)[0]
                    # The bytes before the format string are only DIMENSIONS if
                    # the pixels could actually fit. Without this check the last
                    # match won even when it was garbage -- SPR_Divider read as
                    # 1308622848x6647407 instead of its real 198x2.
                    size = (w * h * 4 if tag == b"PF_B8G8R8A8"
                            else self._payload_size(w, h, tag))
                    if 2 <= w <= 8192 and 2 <= h <= 8192 and size <= have:
                        dims, fmt = (w, h), tag
                start = pos + 1
            if dims:
                break
        if not dims:
            # Not a texture of its own. A UPaperSprite is a wrapper that points at
            # one, so follow it rather than reporting the asset as broken.
            src = self.sprite_source(path, meta)
            return self.texture(src, blobs) if src else None
        w, h = dims

        if fmt == b"PF_BC5":
            return None                           # normal maps, nothing to show
        need = (w * h * 4 if fmt == b"PF_B8G8R8A8"
                else self._payload_size(w, h, fmt))

        if bulk and len(bulk) >= need:
            payload = bulk[-need:]                # .ubulk is pixels and nothing else
            if fmt == b"PF_B8G8R8A8":
                return w, h, payload
            return self.decode_dxt(w, h, payload, fmt)

        # Inline pixels live in the .uexp. Raw BGRA sits at the end, but a
        # compressed mip sits at the FRONT, behind a small export header whose
        # size varies -- so its start is located rather than assumed.
        if fmt == b"PF_B8G8R8A8":
            # The pixels are followed by a trailer (a few bytes, then the 4-byte
            # legacy package tag), so the tail is NOT the end of the image. Being
            # 8 bytes out rotated every frame by two pixels -- which is exactly
            # what "the sprite sheet isn't clipped properly" looked like: the ball
            # ran off the left edge and its missing strip reappeared on the right.
            # The trailer size varies, so the start is found, not assumed.
            if len(meta) < need:
                return None
            off = self.bgra_offset(w, h, meta, len(meta) - need)
            return w, h, meta[off:off + need]

        if len(exp) < need:
            return None
        off = self.dxt_offset(w, h, exp, fmt, len(exp) - need)
        return self.decode_dxt(w, h, exp[off:off + need], fmt)

    def _package_bytes(self, path: str) -> bytes:
        """Legacy cooked packages split header/.uasset from exports/.uexp; the
        pixels live in the .uexp, so both halves have to be joined."""
        blob = self.read(path)
        if path.lower().endswith(".uasset"):
            uexp = path[:-len(".uasset")] + ".uexp"
            try:
                blob += self.read(uexp)
            except KeyError:
                pass
        return blob

    # ---------------------------------------------------------------- audio
    def audio_mp3(self, path: str, out_path: Path) -> Path | None:
        blob = self._package_bytes(path)
        stem = path[:-len(".uasset")] if path.lower().endswith(".uasset") else path
        try:                                          # cooked audio may be in .ubulk
            blob += self.read(stem + ".ubulk")
        except KeyError:
            pass
        p = blob.find(b"ABEU")                       # 'UEBA' little-endian
        if p < 0:
            return None
        if not BINKADEC.exists():
            raise RuntimeError(
                "binkadec.exe is missing — audio export needs it. See "
                "tools/README.md for where to put it.")
        tmp = Path(tempfile.mkdtemp(prefix="gamma_snd_"))
        raw, wav = tmp / "a.binka", tmp / "a.wav"
        try:
            raw.write_bytes(blob[p:])
            if _run([str(BINKADEC), "-i", str(raw), "-o", str(wav)]).returncode != 0:
                return None
            out_path.parent.mkdir(parents=True, exist_ok=True)
            r = _run([FFMPEG, "-hide_banner", "-v", "error", "-y", "-i", str(wav),
                      "-c:a", "libmp3lame", "-q:a", "4", str(out_path)])
            return out_path if r.returncode == 0 else None
        finally:
            for f in (raw, wav):
                if f.exists():
                    f.unlink()
            try:
                tmp.rmdir()
            except OSError:
                pass

    # ------------------------------------------------------------ animation
    @staticmethod
    def frame_sort_key(name: str):
        m = re.search(r"_?(\d+)(?:_Sprite)?\.uasset$", name, re.I)
        return (int(m.group(1)) if m else 0, name)

    def animation_frames(self, directory: str):
        """Frame packages of one animation folder, in play order.

        Accepts "<dir>|<stem>" to take only one sequence out of a folder that
        holds several.
        """
        directory, _, stem = directory.partition("|")
        files = [p for p in self.files_in(directory, limit=4000)
                 if p.lower().endswith(".uasset")]
        if stem:
            files = [p for p in files
                     if re.sub(r"_?\d+\.uasset$", "", p.rsplit("/", 1)[-1],
                               flags=re.I) == stem]
        frames = [p for p in files
                  if p.rsplit("/", 1)[-1].upper().startswith("SPR_")
                  and not re.search(r"_SPRITE(_\d+)?\.UASSET$", p.upper())]
        frames.sort(key=lambda p: self.frame_sort_key(p.rsplit("/", 1)[-1]))
        return frames

    # ------------------------------------------------------- flipbook frames
    def _content_root(self, path: str) -> str:
        """The `<Pak>/<Project>/Content/` prefix a /Game/... path hangs off."""
        i = path.find("/Content/")
        return path[:i + len("/Content/")] if i >= 0 else ""

    def flipbook(self, fb_path: str):
        """(fps, [(texture package, sprite index or None), ...]) for an FB_ asset.

        The flipbook is the only thing that knows an animation's real frame ORDER,
        its frame RUNS (a held frame is listed twice) and its FPS. Listing a folder
        gets all three wrong -- and for the overworld sprites it is not even a list
        of frames, because those are sub-sprites of one sheet.
        """
        try:
            blob = self._package_bytes(fb_path)
        except KeyError:
            return None
        parsed = parse_paper_flipbook(blob)
        if not parsed:
            return None
        fps, sprites = parsed
        root = self._content_root(fb_path)
        out = []
        for pkg in sprites:
            if not pkg.startswith("/Game/"):
                continue
            rel = pkg[len("/Game/"):]
            # `SPR_Sheet_Sprite_4` is sub-sprite 4 of SPR_Sheet; `SPR_Frame_Sprite`
            # is a lone sprite wrapping SPR_Frame outright.
            m = re.search(r"_Sprite_(\d+)$", rel)
            if m:
                idx = int(m.group(1))
                rel = rel[:m.start()]
            else:
                idx = None
                if rel.endswith("_Sprite"):
                    rel = rel[:-len("_Sprite")]
            out.append((root + rel + ".uasset", idx))
        return (fps, out) if out else None

    def flipbooks_in(self, directory: str):
        return sorted(p for p in self.files_in(directory, limit=4000)
                      if p.rsplit("/", 1)[-1].upper().startswith("FB_")
                      and p.lower().endswith(".uasset"))

    @staticmethod
    def _grid(w: int, h: int, n: int):
        """(cols, rows) for a sheet holding n sub-sprites, or None.

        Cells are SQUARE and tile the sheet exactly, so the answer is the largest
        square size dividing both dimensions that still leaves room for every
        sub-sprite. The sheet is not always full: NurseJoy is 5 sprites on a 96x96
        sheet, which is a 3x3 grid of 32x32 with four slots unused.

        Factorising the sprite COUNT instead (the first attempt) cannot express
        that -- 5 has no grid that divides 96x96 -- so those sheets fell back to
        showing the whole sheet, which is what the NPCs and Poke Balls looked like.

        The exact per-sprite rect lives in each UPaperSprite's BakedRenderData,
        which this build does not serialise; this is derived instead.
        """
        if n <= 0 or w <= 0 or h <= 0:
            return None
        for cell in range(min(w, h), 0, -1):
            if w % cell or h % cell:
                continue
            cols, rows = w // cell, h // cell
            if cols * rows >= n:
                return cols, rows
        return None

    def sheet_cells(self, tex_path: str) -> int:
        """How many sub-sprites reference this texture as a sheet."""
        stem = tex_path[:-len(".uasset")]
        pre = stem + "_Sprite_"
        return sum(1 for p in self.paths() if p.startswith(pre)
                   and p.lower().endswith(".uasset"))

    @staticmethod
    def crop_bgra(w, h, bgra, x, y, cw, ch):
        rows = []
        for r in range(y, y + ch):
            off = (r * w + x) * 4
            rows.append(bgra[off:off + cw * 4])
        return cw, ch, b"".join(rows)

    def anim_plan(self, target: str, every: int = 1, max_frames: int = 120):
        """(fps, [(texture package, sprite index or None), ...]) for anything
        playable: an FB_ flipbook, or a folder of numbered frames.

        A flipbook is preferred wherever one exists -- it carries the real order,
        the real FPS and the held frames. A folder is the fallback for the battle
        sprites, which genuinely are one texture per frame.
        """
        if target.lower().endswith(".uasset"):
            fb = self.flipbook(target)
            if fb:
                fps, frames = fb
            else:
                # A handful of flipbooks do not parse (BoxSlotFX, Fire3, a few FX).
                # Falling back to the numbered frames beside them plays the right
                # thing, just without the authored order or FPS -- much better than
                # the blank preview those used to give.
                folder = target.rsplit("/", 1)[0]
                frames = [(p, None) for p in self.animation_frames(folder)]
                fps = 15.0
        else:
            frames = [(p, None) for p in self.animation_frames(target)]
            fps = 15.0
        if not frames:
            return None
        frames = frames[:max_frames * max(1, every)][::max(1, every)]
        return fps / max(1, every), frames

    def anim_pngs(self, plan, out_dir: Path) -> int:
        """Decode a plan to numbered PNGs. Returns how many were written."""
        fps, frames = plan
        wanted = set()
        for tex, _idx in frames:
            stem = tex[:-len(".uasset")]
            wanted |= {tex, stem + ".uexp", stem + ".ubulk"}
        blobs = self.read_many(wanted)
        cells = {}
        n = 0
        for tex, idx in frames:
            t = self.texture(tex, blobs)
            if not t:
                continue
            if idx is not None:
                w, h, bgra = t
                if tex not in cells:
                    total = self.sheet_cells(tex)
                    cells[tex] = self._grid(w, h, total) if total > 1 else None
                grid = cells[tex]
                if grid:
                    cols, rows = grid
                    cw, ch = w // cols, h // rows
                    t = self.crop_bgra(w, h, bgra,
                                       (idx % cols) * cw, (idx // cols) * ch, cw, ch)
            (out_dir / ("f%04d.png" % n)).write_bytes(self.png_from_bgra(*t))
            n += 1
        return n

    def animation_gif(self, target: str, out_path: Path, fps: float | None = None,
                      every: int = 1, max_frames: int = 120) -> Path | None:
        """Render a flipbook (or a frame folder) to a GIF via ffmpeg.

        ffmpeg's palettegen/paletteuse is used rather than a hand-rolled quantiser:
        it keeps 1-bit transparency and picks a better palette than anything worth
        writing here.
        """
        plan = self.anim_plan(target, every=every, max_frames=max_frames)
        if not plan:
            return None
        rate = max(1.0, float(fps) if fps else plan[0])
        tmp = Path(tempfile.mkdtemp(prefix="gamma_gif_"))
        try:
            if self.anim_pngs(plan, tmp) == 0:
                return None
            out_path.parent.mkdir(parents=True, exist_ok=True)
            vf = ("split[a][b];[a]palettegen=reserve_transparent=1[p];"
                  "[b][p]paletteuse=alpha_threshold=128")
            r = _run([FFMPEG, "-hide_banner", "-v", "error", "-y",
                      "-framerate", "%.3f" % rate, "-i", str(tmp / "f%04d.png"),
                      "-vf", vf, "-loop", "0", str(out_path)])
            return out_path if r.returncode == 0 and out_path.exists() else None
        finally:
            for f in tmp.glob("*.png"):
                f.unlink()
            try:
                tmp.rmdir()
            except OSError:
                pass

def data_uri(blob: bytes, mime: str) -> str:
    return "data:%s;base64,%s" % (mime, base64.b64encode(blob).decode("ascii"))
