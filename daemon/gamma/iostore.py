"""
Gamma Emerald (UE5.3 fork) IoStore .utoc/.ucas extractor.
Format reverse-engineered from the shipped PokemonEmerald-Windows.utoc/ucas.

Key findings vs stock UE5.3:
- TOC magic is 8 bytes: '-=--==-' (0x2D3D3D2D repeated twice), then 1-byte version (=5)
- Directory index entries are 16 bytes {Name, FirstChildEntry, NextSiblingEntry, FirstFileEntry}
- File index entries are 12 bytes {Name, NextFileEntry, UserData(=chunk idx)}
- FString serialization: int32 length INCLUDING null terminator, chars, null
- Compression method: Oodle (single method)
- Container is NOT encrypted (zero key GUID)
"""
import ctypes
import os
import struct
import sys
import threading
from dataclasses import dataclass

MAGIC8 = b"\x2D\x3D\x3D\x2D\x2D\x3D\x3D\x2D"  # -==--==-
INVALID = 0xFFFFFFFF


@dataclass
class TocHeader:
    version: int
    toc_entry_count: int
    block_entry_count: int
    block_entry_size: int
    cmn_count: int
    cmn_len: int
    compression_block_size: int
    directory_index_size: int
    partition_count: int
    container_id: bytes
    key_guid: bytes
    container_flags: int
    seeds_count: int
    partition_size: int
    chunks_without_ph_count: int


class Oodle:
    def __init__(self, dll_path):
        self.lib = ctypes.CDLL(dll_path)
        self.lib.OodleLZ_Decompress.restype = ctypes.c_int
        self.lib.OodleLZ_Decompress.argtypes = [
            ctypes.c_void_p, ctypes.c_int,
            ctypes.c_void_p, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_void_p, ctypes.c_int,
            ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_void_p, ctypes.c_int,
            ctypes.c_void_p, ctypes.c_int,
        ]

    def decompress(self, src: bytes, dst_size: int) -> bytes:
        dst = ctypes.create_string_buffer(dst_size)
        n = self.lib.OodleLZ_Decompress(
            src, len(src), dst, dst_size,
            0, 0, 0, None, 0, None, None, None, 0, None, 0)
        if n <= 0:
            raise RuntimeError(f"Oodle decompress failed: {n}")
        return dst.raw[:n]


class TocReader:
    def __init__(self, utoc_path):
        with open(utoc_path, "rb") as f:
            self.buf = f.read()
        self._parse()

    def _parse(self):
        b = self.buf
        assert b[:16] == MAGIC8 * 2, "bad magic"
        self.header = TocHeader(
            version=b[16],
            toc_entry_count=struct.unpack_from("<I", b, 24)[0],
            block_entry_count=struct.unpack_from("<I", b, 28)[0],
            block_entry_size=struct.unpack_from("<I", b, 32)[0],
            cmn_count=struct.unpack_from("<I", b, 36)[0],
            cmn_len=struct.unpack_from("<I", b, 40)[0],
            compression_block_size=struct.unpack_from("<I", b, 44)[0],
            directory_index_size=struct.unpack_from("<I", b, 48)[0],
            partition_count=struct.unpack_from("<I", b, 52)[0],
            container_id=b[56:64],
            key_guid=b[64:80],
            container_flags=b[80],
            seeds_count=struct.unpack_from("<I", b, 84)[0],
            partition_size=struct.unpack_from("<Q", b, 88)[0],
            chunks_without_ph_count=struct.unpack_from("<I", b, 96)[0],
        )
        assert b[80] & 0x2 == 0, "container is encrypted?!"
        h = self.header
        assert h.toc_entry_count < 10_000_000 and h.block_entry_count < 20_000_000

        off = 144
        n = h.toc_entry_count
        self.chunk_ids = []
        for _ in range(n):
            cid, idx, pad, ctype = struct.unpack_from("<QHBB", b, off)
            self.chunk_ids.append((cid, idx, ctype))
            off += 12
        self.chunk_offsets = []
        for _ in range(n):
            raw = b[off:off + 10]
            off += 10
            o = raw[4] | (raw[3] << 8) | (raw[2] << 16) | (raw[1] << 24) | (raw[0] << 32)
            l = raw[9] | (raw[8] << 8) | (raw[7] << 16) | (raw[6] << 24) | (raw[5] << 32)
            self.chunk_offsets.append((o, l))
        self.seeds = []
        for _ in range(h.seeds_count):
            self.seeds.append(struct.unpack_from("<i", b, off)[0])
            off += 4
        if h.chunks_without_ph_count:
            self.chunks_without_ph = struct.unpack_from(
                f"<{h.chunks_without_ph_count}i", b, off)
            off += 4 * h.chunks_without_ph_count
        else:
            self.chunks_without_ph = []
        self.blocks = []
        for _ in range(h.block_entry_count):
            data = b[off:off + 12]
            off += 12
            o = int.from_bytes(data[:5], "little") & 0xFFFFFFFFFF
            csize = (struct.unpack_from("<I", data, 4)[0] >> 8) & 0xFFFFFF
            usize = struct.unpack_from("<I", data, 8)[0] & 0xFFFFFF
            midx = data[11]
            self.blocks.append((o, csize, usize, midx))
        names_len = h.cmn_count * h.cmn_len
        names = b[off:off + names_len]
        off += names_len
        self.compression_methods = ["None"] + [
            n.decode("ascii").split("\0")[0] for n in [
                names[i * h.cmn_len:(i + 1) * h.cmn_len]
                for i in range(h.cmn_count)
            ] if n.strip(b"\0")
        ]
        print(f"methods: {self.compression_methods}")
        self.dir_index_off = off
        if h.directory_index_size > 0 and (h.container_flags & 0x8):
            self._parse_directory_index(b[off:off + h.directory_index_size])
        else:
            self.mount_point = ""
            self.dir_entries = []
            self.file_entries = []
            self.strings = []
            print("no directory index in this container")

    def _parse_directory_index(self, d):
        mount_len = struct.unpack_from("<I", d, 0)[0]
        p = 4
        self.mount_point = d[p:p + mount_len - 1].decode("utf-8")
        p += mount_len
        dir_count = struct.unpack_from("<I", d, p)[0]
        p += 4
        self.dir_entries = []
        for _ in range(dir_count):
            name, child, sib, file = struct.unpack_from("<IIII", d, p)
            p += 16
            self.dir_entries.append((name, child, sib, file))
        file_count = struct.unpack_from("<I", d, p)[0]
        p += 4
        self.file_entries = []
        for _ in range(file_count):
            name, nxt, user = struct.unpack_from("<III", d, p)
            p += 12
            self.file_entries.append((name, nxt, user))
        str_count = struct.unpack_from("<I", d, p)[0]
        p += 4
        self.strings = []
        for _ in range(str_count):
            ln = struct.unpack_from("<I", d, p)[0]
            p += 4
            s = d[p:p + ln - 1].decode("utf-8") if ln > 1 else ""
            p += ln
            self.strings.append(s)
        print(f"dirs={dir_count} files={file_count} strings={str_count} mount='{self.mount_point}'")

    def iter_files(self):
        if not self.dir_entries:
            return
        yield from self._walk_dir(0, self.mount_point)

    def _walk_dir(self, dir_idx, parent_path):
        name, child, sib, first_file = self.dir_entries[dir_idx]
        if name != INVALID:
            path = parent_path + self.strings[name] + "/"
        else:
            path = parent_path
        f = first_file
        while f != INVALID:
            fname, nxt, user = self.file_entries[f]
            yield path + self.strings[fname], user
            f = nxt
        if child != INVALID:
            yield from self._walk_dir(child, path)
        if sib != INVALID:
            yield from self._walk_dir(sib, parent_path)


class CasExtractor:
    def __init__(self, toc: TocReader, ucas_path: str, oodle: Oodle):
        self.toc = toc
        self.oodle = oodle
        self.f = open(ucas_path, "rb")
        self._lock = threading.Lock()
        self.block_size = toc.header.compression_block_size

    def extract_chunk(self, chunk_idx: int) -> bytes:
        offset, length = self.toc.chunk_offsets[chunk_idx]
        return self.read_range(offset, length)

    def read_range(self, offset: int, length: int) -> bytes:
        first = offset // self.block_size
        last = (offset + length - 1) // self.block_size
        out = bytearray()
        remaining = length
        cur = offset
        for bi in range(first, last + 1):
            boff, csize, usize, midx = self.toc.blocks[bi]
            # one handle, many request threads -- see PakReader._read
            with self._lock:
                self.f.seek(boff)
                raw = self.f.read(csize)
            if midx == 0:
                src = raw
            else:
                src = self.oodle.decompress(raw, usize)
            in_block = cur % self.block_size
            take = min(self.block_size - in_block, remaining)
            out += src[in_block:in_block + take]
            remaining -= take
            cur += take
        return bytes(out)


def extract_all(utoc_path, ucas_path, out_dir, oodle, verbose=True):
    toc = TocReader(utoc_path)
    cas = CasExtractor(toc, ucas_path, oodle)
    n = 0
    for path, chunk_idx in toc.iter_files():
        rel = path
        while rel.startswith("../"):
            rel = rel[3:]
        dest = os.path.normpath(os.path.join(out_dir, rel))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        try:
            data = cas.extract_chunk(chunk_idx)
        except Exception as e:
            print(f"FAIL {path}: {e}")
            continue
        with open(dest, "wb") as f:
            f.write(data)
        n += 1
        if verbose and n % 1000 == 0:
            print(f"{n} files extracted...")
    print(f"done: {n} files")


if __name__ == "__main__":
    import argparse
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from .versions import add_version_arg, find_oodle, get

    ap = argparse.ArgumentParser(description="Extract IoStore (utoc/ucas) containers")
    add_version_arg(ap, default="original")
    ap.add_argument("--out", default="", help="override extract dir")
    args = ap.parse_args()
    spec = get(args.version)
    if spec.container != "iostore":
        sys.exit(f"{spec.id} is a {spec.container} build — use pak_extract.py --version {spec.id}")
    if not spec.utoc or not spec.utoc.exists():
        sys.exit(f"utoc missing: {spec.utoc}")
    oodle = Oodle(str(find_oodle()))
    # IoStore paths are Content/... after stripping ../../../ ; keep Content under PokemonEmerald/
    out = args.out or str(spec.extract_dir / "PokemonEmerald")
    extract_all(str(spec.utoc), str(spec.ucas), out, oodle)
