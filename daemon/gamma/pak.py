"""UE pak v11 extractor (unencrypted original + AES-256-ECB Early Access).

Index layout (PathHashIndex era, version 11 / Fnv64BugFix):
  FPakInfo footer (221-ish bytes, magic 0x5A6F12E1)
  primary index: mount + encoded entries
  PathHashIndex + FullDirectoryIndex stored at absolute file offsets
    listed *inside* the (possibly encrypted) primary index

Directory index is TMap<dir, TMap<file, encoded-entry-offset>>.
Encoded entries use the bit-packed format from Engine FPakFile::DecodePakEntry
(same layout as trumank/repak read_encoded).

Usage:
  python pak_extract.py --version original --list
  python pak_extract.py --version ea --aes-key HEX
  python dump_aes_key.py   # attach to a running EA exe, write ea_aes.key
"""
from __future__ import annotations

import argparse
import os
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from .versions import find_oodle, get
from .iostore import Oodle

MAGIC = 0x5A6F12E1
PAK_VERSION = 11


def _fstring(buf: bytes, p: int) -> tuple[str, int]:
    n = struct.unpack_from("<i", buf, p)[0]
    p += 4
    if n == 0:
        return "", p
    if n < 0:
        n = -n
        s = buf[p:p + n * 2].decode("utf-16-le", "replace").rstrip("\0")
        return s, p + n * 2
    s = buf[p:p + n].decode("utf-8", "replace").rstrip("\0")
    return s, p + n


def _align16(n: int) -> int:
    return (n + 15) & ~15


@dataclass
class Block:
    start: int
    end: int


@dataclass
class PakEntry:
    offset: int
    compressed: int
    uncompressed: int
    compression_slot: int | None  # 0-based into methods list, None = store
    blocks: list[Block] = field(default_factory=list)
    encrypted: bool = False
    compression_block_size: int = 0


def decode_entry(buf: bytes, p: int) -> PakEntry:
    """FPakFile::DecodePakEntry / repak Entry::read_encoded."""
    bits = struct.unpack_from("<I", buf, p)[0]
    p += 4
    comp_n = (bits >> 23) & 0x3F
    compression = None if comp_n == 0 else comp_n - 1
    encrypted = (bits & (1 << 22)) != 0
    block_count = (bits >> 6) & 0xFFFF
    cbs = bits & 0x3F
    if cbs == 0x3F:
        cbs = struct.unpack_from("<I", buf, p)[0]
        p += 4
    else:
        cbs <<= 11

    def var_int(bit: int) -> int:
        nonlocal p
        if bits & (1 << bit):
            v = struct.unpack_from("<I", buf, p)[0]
            p += 4
            return v
        v = struct.unpack_from("<Q", buf, p)[0]
        p += 8
        return v

    offset = var_int(31)
    uncompressed = var_int(30)
    compressed = uncompressed if compression is None else var_int(29)

    # unencoded header size at the data offset (v8+ / fname compression)
    header = 8 + 8 + 8 + 4 + 20  # offset, csize, usize, method u32, sha
    if compression is not None:
        header += 4 + 16 * block_count  # block count + Block{start,end} each
    header += 1 + 4  # flags + block size

    blocks: list[Block] = []
    if block_count == 1 and not encrypted:
        blocks = [Block(header, header + compressed)]
    elif block_count > 0:
        index = header
        for _ in range(block_count):
            bsz = struct.unpack_from("<I", buf, p)[0]
            p += 4
            blocks.append(Block(index, index + bsz))
            index += _align16(bsz) if encrypted else bsz

    return PakEntry(
        offset=offset, compressed=compressed, uncompressed=uncompressed,
        compression_slot=compression, blocks=blocks, encrypted=encrypted,
        compression_block_size=cbs,
    )


@dataclass
class PakInfo:
    version: int
    index_offset: int
    index_size: int
    encrypted_index: bool
    key_guid: bytes
    methods: list[str]


def read_info(data: bytes) -> PakInfo:
    magic = struct.pack("<I", MAGIC)
    idx = data[-256:].rfind(magic)
    if idx < 0:
        raise ValueError("pak magic not found in last 256 bytes")
    tail_off = len(data) - 256 + idx
    ver = struct.unpack_from("<i", data, tail_off + 4)[0]
    ioff, isize = struct.unpack_from("<qq", data, tail_off + 8)
    guid = data[tail_off - 16:tail_off]
    # methods occupy the last 160 bytes (5 x 32)
    methods_raw = data[-160:]
    methods = []
    for i in range(5):
        name = methods_raw[i * 32:(i + 1) * 32].split(b"\0", 1)[0].decode("ascii", "replace")
        if name:
            methods.append(name)
    encrypted = guid != b"\x00" * 16
    return PakInfo(ver, ioff, isize, encrypted, guid, methods)


def aes_ecb_decrypt(data: bytes, key: bytes) -> bytes:
    from Crypto.Cipher import AES
    if len(data) % 16:
        raise ValueError(f"AES payload not 16-aligned ({len(data)})")
    return AES.new(key, AES.MODE_ECB).decrypt(data)


def parse_primary_index(index: bytes) -> tuple[str, bytes, int, int, int, int]:
    """-> mount, encoded_entries, pathhash_off, pathhash_size, dir_off, dir_size"""
    mount, p = _fstring(index, 0)
    _num = struct.unpack_from("<i", index, p)[0]
    p += 4
    p += 8  # PathHashSeed
    has_ph = struct.unpack_from("<i", index, p)[0]
    p += 4
    ph_off = ph_sz = 0
    if has_ph:
        ph_off, ph_sz = struct.unpack_from("<qq", index, p)
        p += 16 + 20  # hash
    has_dir = struct.unpack_from("<i", index, p)[0]
    p += 4
    dir_off = dir_sz = 0
    if has_dir:
        dir_off, dir_sz = struct.unpack_from("<qq", index, p)
        p += 16 + 20
    enc_sz = struct.unpack_from("<i", index, p)[0]
    p += 4
    encoded = index[p:p + enc_sz]
    return mount, encoded, ph_off, ph_sz, dir_off, dir_sz


def parse_directory_index(buf: bytes) -> list[tuple[str, int]]:
    p = 0
    n_dirs = struct.unpack_from("<i", buf, p)[0]
    p += 4
    out = []
    for _ in range(n_dirs):
        dname, p = _fstring(buf, p)
        n_files = struct.unpack_from("<i", buf, p)[0]
        p += 4
        for _ in range(n_files):
            fname, p = _fstring(buf, p)
            eidx = struct.unpack_from("<i", buf, p)[0]
            p += 4
            out.append((dname + fname, eidx))
    return out


class PakReader:
    def __init__(self, path: str | Path, aes_key: bytes | None = None, oodle: Oodle | None = None):
        self.path = Path(path)
        self.f = open(self.path, "rb")
        self.f.seek(0, os.SEEK_END)
        self.size = self.f.tell()
        self.f.seek(max(0, self.size - 256))
        tail = self.f.read()
        # read_info wants the whole file's tail semantics — reconstruct last 256
        pad = b"\x00" * (256 - len(tail)) + tail if len(tail) < 256 else tail
        # we only passed the tail; re-open via a fake by seeking
        self.f.seek(self.size - len(tail))
        # parse from tail directly
        magic = struct.pack("<I", MAGIC)
        idx = tail.rfind(magic)
        if idx < 0:
            raise ValueError("not a pak (magic missing)")
        self.info = PakInfo(
            version=struct.unpack_from("<i", tail, idx + 4)[0],
            index_offset=struct.unpack_from("<q", tail, idx + 8)[0],
            index_size=struct.unpack_from("<q", tail, idx + 16)[0],
            encrypted_index=(tail[idx - 16:idx] != b"\x00" * 16),
            key_guid=tail[idx - 16:idx],
            methods=[
                tail[-160 + i * 32: -160 + (i + 1) * 32].split(b"\0", 1)[0].decode("ascii", "replace")
                for i in range(5)
            ],
        )
        self.info.methods = [m for m in self.info.methods if m]
        self.aes_key = aes_key
        self.oodle = oodle
        self._load_index()

    def _read(self, off: int, size: int) -> bytes:
        self.f.seek(off)
        b = self.f.read(size)
        if len(b) != size:
            raise EOFError(f"short read at {off} want {size} got {len(b)}")
        return b

    def _maybe_decrypt(self, blob: bytes, label: str) -> bytes:
        if not self.info.encrypted_index:
            return blob
        if not self.aes_key:
            raise RuntimeError(
                f"{label} is AES-encrypted; pass --aes-key (see dump_aes_key.py)"
            )
        return aes_ecb_decrypt(blob, self.aes_key)

    def _load_index(self):
        raw = self._read(self.info.index_offset, self.info.index_size)
        index = self._maybe_decrypt(raw, "pak index")
        # sanity: decrypted index starts with FString "../../../"
        n = struct.unpack_from("<i", index, 0)[0]
        if not (1 <= abs(n) <= 64):
            raise RuntimeError(
                "pak index did not decrypt to a mount-point FString "
                f"(first i32={n}). Wrong AES key?"
            )
        mount, encoded, _ph_off, _ph_sz, dir_off, dir_sz = parse_primary_index(index)
        self.mount = mount
        self.encoded = encoded
        dir_raw = self._read(dir_off, dir_sz)
        dir_buf = self._maybe_decrypt(dir_raw, "directory index")
        self.files = parse_directory_index(dir_buf)

    def iter_files(self):
        for path, eoff in self.files:
            yield path, decode_entry(self.encoded, eoff)

    def read_entry(self, entry: PakEntry) -> bytes:
        # data payload starts after the unencoded FPakEntry header at entry.offset
        payload_off = entry.offset
        # skip header by using block ranges when present; otherwise read compressed bytes
        if entry.blocks:
            # blocks are relative to the pak-entry start (RelativeChunkOffsets)
            chunks = []
            for b in entry.blocks:
                raw_off = entry.offset + b.start
                raw_sz = b.end - b.start
                read_sz = _align16(raw_sz) if entry.encrypted else raw_sz
                raw = self._read(raw_off, read_sz)
                if entry.encrypted:
                    if not self.aes_key:
                        raise RuntimeError("file is encrypted; need --aes-key")
                    raw = aes_ecb_decrypt(raw, self.aes_key)[:raw_sz]
                chunks.append(self._decompress(raw, entry, len(chunks)))
            return b"".join(chunks)[:entry.uncompressed]

        read_sz = _align16(entry.compressed) if entry.encrypted else entry.compressed
        # uncompressed stored files still have a header in front
        # For store-only, skip by reading from offset + serialized header.
        # Safer: read compressed bytes after a conservative header skip via
        # seeking to offset and using blocks-equivalent of [header, header+csize].
        header = 8 + 8 + 8 + 4 + 20 + 1 + 4
        raw = self._read(entry.offset + header, read_sz)
        if entry.encrypted:
            raw = aes_ecb_decrypt(raw, self.aes_key)[:entry.compressed]
        if entry.compression_slot is None:
            return raw[:entry.uncompressed]
        return self._decompress(raw, entry, 0)

    def _decompress(self, raw: bytes, entry: PakEntry, block_i: int) -> bytes:
        if entry.compression_slot is None:
            return raw
        method = self.info.methods[entry.compression_slot] if entry.compression_slot < len(self.info.methods) else ""
        if not method or method.lower() == "none":
            return raw
        if entry.compression_block_size and entry.uncompressed > entry.compression_block_size:
            dst = min(entry.compression_block_size,
                      entry.uncompressed - block_i * entry.compression_block_size)
        else:
            dst = entry.uncompressed
        if method.lower() == "oodle":
            if not self.oodle:
                raise RuntimeError(
                    "this pak is Oodle-compressed and oodle-data-shared.dll was "
                    "not found. Drop it in tools/ or set GAMMA_OODLE_DLL — see "
                    "tools/README.md.")
            return self.oodle.decompress(raw, dst)
        if method.lower() == "zlib":
            import zlib
            return zlib.decompress(raw)
        raise RuntimeError(f"unsupported compression {method!r}")

    def close(self):
        self.f.close()


def extract_all(reader: PakReader, out_dir: Path, prefix="", verbose=True):
    n = 0
    for path, entry in reader.iter_files():
        rel = path
        if prefix and not rel.replace("\\", "/").startswith(prefix):
            continue
        dest = out_dir / rel.replace("/", os.sep)
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = reader.read_entry(entry)
        except Exception as e:
            print(f"FAIL {path}: {e}")
            continue
        dest.write_bytes(data)
        n += 1
        if verbose and n % 1000 == 0:
            print(f"{n} files extracted...")
    print(f"done: {n} files -> {out_dir}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", choices=("original", "ea"), default="ea")
    ap.add_argument("--aes-key", help="32-byte AES key as hex (EA pak index)")
    ap.add_argument("--list", action="store_true", help="print file list, don't extract")
    ap.add_argument("--prefix", default="", help="only extract paths with this prefix")
    ap.add_argument("--out", default="", help="override extract dir")
    args = ap.parse_args()

    spec = get(args.version)
    if not spec.pak or not spec.pak.exists():
        sys.exit(f"pak missing: {spec.pak}")
    key = spec.find_aes_key(args.aes_key)
    oodle = None
    try:
        oodle = Oodle(str(find_oodle()))
    except FileNotFoundError as e:
        print("WARN", e)

    reader = PakReader(spec.pak, aes_key=key, oodle=oodle)
    print(f"{spec.name}: {len(reader.files)} files, methods={reader.info.methods}, "
          f"encrypted_index={reader.info.encrypted_index}, mount={reader.mount!r}")
    if args.list:
        for path, entry in reader.iter_files():
            if args.prefix and not path.startswith(args.prefix):
                continue
            print(f"{entry.uncompressed:10d}  {path}")
        return 0
    out = Path(args.out) if args.out else spec.extract_dir
    extract_all(reader, out, prefix=args.prefix)
    reader.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
