"""Gamma Emerald bot core: complete UE5.3 runtime access (external, ctypes-only).

Verified layouts (this build):
  UObjectBaseInternal: vtable@0, ObjectFlags@8(u32), InternalIndex@0xC(u32),
                       ClassPrivate@0x10, NamePrivate@0x18 (FName{idx,u32 num}), OuterPrivate@0x20
  FUObjectArray (global in .data):
     +0x00 ObjFirstGCIndex, +0x04 ObjLastNonGCIndex, +0x08 ?, +0x0C OpenForDGC(bool)
     +0x10 ObjObjects: { Objects@0 (ptr->chunk ptr array), PreAllocated@8, MaxEl@0x10, NumEl@0x14, MaxCh@0x18, NumCh@0x1C }
  FUObjectItem = 32 bytes: {ptr, flags, cluster, serial, statid}
  FNameEntryId: block = idx>>16, halfword = idx&0xFFFF, byte offset = (idx&0xFFFF)*2
  FNameEntry: {u16 header; data[len]}  header: wide=header&1, len=header>>6
  FNameEntryAllocator (function-local static in .data): +0x10 = Blocks[4096] ptr array
"""
import os, sys, struct, time
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from memory import GameProcess


class UEGame:
    def __init__(self, gp: GameProcess):
        self.gp = gp
        self.base = gp.module_base
        self.mod_end = gp.module_base + gp.module_size
        self.gobjects = None
        self.namepool = None
        self._chunks = None

    # ---------------- discovery ----------------
    def find_gobjects(self):
        """Scan .data for FUObjectArray signature."""
        base = self.base
        start = base + 0x8000000
        end = self.mod_end
        data = self.gp.rpm(start, end - start)
        arr = np.frombuffer(data, dtype=np.uint64)
        u32s = np.frombuffer(data, dtype=np.uint32)
        idx = np.arange((len(data) // 8) - 16, dtype=np.int64)
        f0 = u32s[idx * 2]
        f1 = u32s[idx * 2 + 1]
        objects_ptr = arr[idx + 2]
        maxel = u32s[idx * 2 + 8]
        numel = u32s[idx * 2 + 9]
        maxch = u32s[idx * 2 + 10]
        numch = u32s[idx * 2 + 11]
        m = (f0 <= 50000) & (1000 <= f1) & (f1 <= 500000) & \
            (objects_ptr > 0x10000000000) & \
            (maxel % 65536 == 0) & (65536 <= maxel) & (maxel <= 4000000) & \
            (1000 <= numel) & (numel <= maxel) & \
            (1 <= maxch) & (maxch <= 64) & (1 <= numch) & (numch <= 64)
        hits = np.where(m)[0]
        for h in hits[:20]:
            h = int(h)
            addr = start + h * 8
            op = int(objects_ptr[h])
            nch = int(numch[h])
            # validate chunk ptr array
            c0 = None
            for ci in range(min(nch, 4)):
                cb = self.gp.rpm(op + ci * 8, 8)
                if cb and len(cb) == 8:
                    c0 = struct.unpack("<Q", cb)[0]
                    break
            if not c0:
                continue
            item = self.gp.rpm(c0, 32)
            if not item or len(item) < 32:
                continue
            self.gobjects = {
                "addr": addr, "objects_ptr": op,
                "num_elements": int(numel[h]), "max_chunks": int(maxch[h]),
                "num_chunks": int(numch[h]),
            }
            print(f"GUObjectArray @ 0x{addr:X}: {self.gobjects['num_elements']} objects, {self.gobjects['num_chunks']} chunks", file=sys.stderr)
            return self.gobjects
        return None

    def find_namepool(self):
        """Scan .data for the FNameEntryAllocator Blocks array (heap ptrs + null tail)."""
        base = self.base
        start = base + 0x8000000
        end = self.mod_end
        data = self.gp.rpm(start, end - start)
        arr = np.frombuffer(data, dtype=np.uint64)
        heap_lo = 0x10000000000
        in_heap = (arr >= heap_lo) & (arr <= 0x7FFFFFFFFFFF)
        zero = arr == 0
        n = len(arr)
        i = 0
        while i < n:
            if not in_heap[i]:
                i += 1
                continue
            j = i
            while j < n and in_heap[j]:
                j += 1
            runlen = j - i
            if 5 <= runlen <= 500:
                k = j
                while k < n and zero[k]:
                    k += 1
                if k - j >= 50:
                    # try every position in the run as the Blocks[] start
                    for p in range(i, j):
                        blocks_addr = start + p * 8
                        b0b = self.gp.rpm(blocks_addr, 8)
                        if not b0b or len(b0b) < 8:
                            continue
                        b0 = struct.unpack("<Q", b0b)[0]
                        head = self.gp.rpm(b0, 8)
                        if head and len(head) >= 8:
                            hdr = struct.unpack("<H", head[:2])[0]
                            if (hdr & 1) == 0 and (hdr >> 6) == 4 and head[2:6] == b"None":
                                # collect the block pointers (read from the Blocks array)
                                raw = self.gp.rpm(blocks_addr, 4096 * 8)
                                ptrs = struct.unpack("<4096Q", raw) if raw and len(raw) >= 4096 * 8 else ()
                                blocks = [q for q in ptrs if q]
                                self.namepool = {"allocator": blocks_addr - 0x10,
                                                 "blocks_addr": blocks_addr,
                                                 "blocks": blocks}
                                print(f"NamePool allocator @ 0x{blocks_addr-0x10:X} ({len(blocks)} blocks)", file=sys.stderr)
                                return self.namepool
            i = j
        return None

    def discover_cached(self, cache):
        """Re-attach using previously discovered addresses instead of rescanning.

        The game has no ASLR churn between runs here: GUObjectArray and the
        NamePool land at the same addresses every launch. Validating a cached
        address costs milliseconds; a fresh scan costs ~10s, which is pure loss on
        every hunt iteration. Falls back to discover() if validation fails.
        """
        try:
            if not cache:
                raise ValueError("no cache")
            self.gobjects = dict(cache["gobjects"])
            self.namepool = dict(cache["namepool"])
            base = self.gobjects["addr"] + 0x10
            n = self.gp.read_u32(base + 0x14)
            nch = self.gp.read_u32(base + 0x1C)
            if not (1000 < n < 5000000) or not (1 <= nch <= 64):
                raise ValueError("object array looks wrong")
            self.gobjects["num_elements"] = n
            self.gobjects["num_chunks"] = nch
            self.gobjects["objects_ptr"] = self.gp.read_u64(self.gobjects["addr"] + 0x10)
            buf = self.gp.rpm(self.gobjects["objects_ptr"], 8 * nch)
            self._chunks = struct.unpack(f"<{nch}Q", buf)
            ok, _ = self.self_test()
            if not ok:
                raise ValueError("self_test failed on cached addresses")
            return self
        except Exception:
            return self.discover()

    def cache(self):
        return {"gobjects": dict(self.gobjects), "namepool": dict(self.namepool)}

    def discover(self):
        if not self.find_gobjects():
            raise RuntimeError("GUObjectArray not found")
        if not self.find_namepool():
            raise RuntimeError("NamePool not found")
        chunks_buf = self.gp.rpm(self.gobjects["objects_ptr"], 8 * self.gobjects["num_chunks"])
        self._chunks = struct.unpack(f"<{self.gobjects['num_chunks']}Q", chunks_buf)
        return self

    # ---------------- accessors ----------------
    def resolve_name(self, index, number=0):
        block = index >> 16
        off = (index & 0xFFFF) * 2
        blocks_addr = self.namepool["blocks_addr"]
        blk = struct.unpack("<Q", self.gp.rpm(blocks_addr + block * 8, 8))[0]
        hdr_b = self.gp.rpm(blk + off, 2)
        if not hdr_b:
            return None
        hdr = struct.unpack("<H", hdr_b)[0]
        wide = hdr & 1
        ln = hdr >> 6
        if not (1 <= ln <= 512):
            return None
        body = self.gp.rpm(blk + off + 2, ln * 2 if wide else ln)
        if not body:
            return None
        s = body.decode("utf-16-le", errors="replace") if wide else body.decode("utf-8", errors="replace")
        if number > 0:
            s += f"_{number - 1}"
        return s

    def item(self, index):
        """Read FUObjectItem by internal index."""
        chunk = self._chunks[index // 65536]
        rec = self.gp.rpm(chunk + (index % 65536) * 32, 32)
        if not rec or len(rec) < 32:
            return None
        return struct.unpack("<Qiii", rec)[:4]

    def obj_header(self, addr):
        hdr = self.gp.rpm(addr, 0x30)
        if not hdr or len(hdr) < 0x28:
            return None
        vt = struct.unpack_from("<Q", hdr, 0)[0]
        flags = struct.unpack_from("<I", hdr, 8)[0]
        iidx = struct.unpack_from("<I", hdr, 0xC)[0]
        cls = struct.unpack_from("<Q", hdr, 0x10)[0]
        nmi = struct.unpack_from("<I", hdr, 0x18)[0]
        nmn = struct.unpack_from("<I", hdr, 0x1C)[0]
        outer = struct.unpack_from("<Q", hdr, 0x20)[0]
        return {"vt": vt, "flags": flags, "iidx": iidx, "class": cls,
                "name_idx": nmi, "name_num": nmn, "outer": outer}

    def obj_name(self, addr):
        h = self.obj_header(addr)
        if not h:
            return None
        return self.resolve_name(h["name_idx"], h["name_num"])

    def class_name(self, addr):
        h = self.obj_header(addr)
        if not h:
            return None
        ch = self.obj_header(h["class"])
        if not ch:
            return None
        return self.resolve_name(ch["name_idx"], ch["name_num"])

    def refresh(self):
        """Re-read the live object-array counts.

        These grow as the game loads worlds -- discover() captured them once, and a
        stale NumElements makes every newly created object INVISIBLE to iteration.
        That silently broke player lookup after a save load: the pawn existed but
        sat beyond the old bound.
        """
        base = self.gobjects["addr"] + 0x10
        n = self.gp.read_u32(base + 0x14)
        ch = self.gp.read_u32(base + 0x1C)
        if n:
            self.gobjects["num_elements"] = n
        if ch and ch != self.gobjects.get("num_chunks"):
            self.gobjects["num_chunks"] = ch
            buf = self.gp.rpm(self.gobjects["objects_ptr"], 8 * ch)
            if buf and len(buf) >= 8 * ch:
                self._chunks = struct.unpack(f"<{ch}Q", buf)
        return self.gobjects

    def iter_objects(self, max_count=None):
        """Yield (index, objaddr) for all live objects."""
        cnt = 0
        self.refresh()
        total = self.gobjects["num_elements"]
        num_chunks = self.gobjects["num_chunks"]
        per_chunk = 65536
        for ci in range(num_chunks):
            chunk = self._chunks[ci]
            n = min(per_chunk, total - ci * per_chunk)
            items = self.gp.rpm(chunk, n * 32)
            if not items:
                continue
            for k in range(n):
                p = struct.unpack_from("<Q", items, k * 32)[0]
                if p:
                    cnt += 1
                    yield ci * per_chunk + k, p
                    if max_count and cnt >= max_count:
                        return

    def find_by_name(self, name, max_scan=0):
        """Find all objects whose name matches (full scan, slow)."""
        for idx, obj in self.iter_objects(max_scan):
            n = self.obj_name(obj)
            if n == name:
                yield idx, obj

    def class_properties(self, class_obj):
        """Walk UStruct.ChildProperties -> [{name, type, offset, arrayDim, elemSize}].
        FField: ClassPrivate(FFieldClass*)@8, Owner@0x10, Next@0x18, NamePrivate@0x20.
        FProperty: ArrayDim@0x30, ElementSize@0x34, PropertyFlags@0x38, OffsetInternal@0x44."""
        t = self.gp
        out = []
        prop = t.read_u64(class_obj + 0x50)
        seen = 0
        while prop and seen < 400:
            hdr = t.rpm(prop, 0x50)
            if not hdr or len(hdr) < 0x30:
                break
            ffc = struct.unpack_from("<Q", hdr, 8)[0]
            nxt = struct.unpack_from("<Q", hdr, 0x18)[0]
            nmi = struct.unpack_from("<I", hdr, 0x20)[0]
            nmn = struct.unpack_from("<I", hdr, 0x24)[0]
            arrdim = struct.unpack_from("<I", hdr, 0x30)[0]
            elemsz = struct.unpack_from("<I", hdr, 0x34)[0]
            off = struct.unpack_from("<i", hdr, 0x44)[0]
            typ = "?"
            if ffc:
                fb = t.rpm(ffc, 8)
                if fb and len(fb) == 8:
                    ti, tn = struct.unpack("<II", fb)
                    typ = self.resolve_name(ti, tn) or "?"
            out.append({"name": self.resolve_name(nmi, nmn), "type": typ,
                        "offset": off, "arrayDim": arrdim, "elemSize": elemsz})
            prop = nxt
            seen += 1
        return out

    def read_prop_value(self, obj, prop, depth=0):
        """Read a property value from an object instance. Handles common types."""
        t = self.gp
        off = obj + prop["offset"]
        typ = prop["type"]
        raw = t.rpm(off, max(prop["elemSize"], 8) * prop["arrayDim"])
        if not raw:
            return None
        if prop["arrayDim"] > 1:
            return list(raw)
        if typ in ("FloatProperty",):
            return struct.unpack("<f", raw[:4])[0]
        if typ in ("IntProperty",):
            return struct.unpack("<i", raw[:4])[0]
        if typ in ("ByteProperty",):
            return raw[0]
        if typ in ("BoolProperty",):
            return bool(raw[0])
        if typ in ("ObjectProperty", "StructProperty", "SoftObjectProperty"):
            return struct.unpack("<Q", raw[:8])[0]
        if typ in ("StructProperty",) and raw:
            return raw
        return raw

    def find_name_index(self, want):
        """Reverse lookup: find the FNameEntryId (index) whose string == want.
        Scans the pool blocks. Returns int index or None."""
        want = want.encode("utf-8")
        blocks_addr = self.namepool["blocks_addr"]
        gp = self.gp
        for block_idx, blk in enumerate(self.namepool["blocks"]):
            if not blk:
                continue
            data = gp.rpm(blk, 0x10000)  # 64KB blocks
            if not data:
                continue
            pos = 0
            n = len(data)
            while pos + 2 <= n:
                hdr = data[pos] | (data[pos + 1] << 8)
                wide = hdr & 1
                ln = hdr >> 6
                if not (1 <= ln <= 512):
                    pos += 2
                    continue
                bl = ln * 2 if wide else ln
                if pos + 2 + bl > n:
                    break
                body = data[pos + 2: pos + 2 + bl]
                if (wide and body.decode("utf-16-le", "replace").encode("utf-8") == want) or \
                   (not wide and body == want):
                    half = pos // 2
                    return (block_idx << 16) | half
                nxt = pos + 2 + bl
                if nxt & 1:
                    nxt += 1
                pos = nxt
        return None

    def self_test(self):
        """Verify the discovered layouts against the game's known boot sequence.
        object[0..1] must resolve to Package '/Script/CoreUObject' and Class 'Object'.
        Returns (ok, [(check_name, passed, detail), ...])."""
        checks = []
        try:
            items = list(self.iter_objects(5))
            if not items:
                return False, [("objects", False, "no objects found")]
            idx0, obj0 = items[0]
            h0 = self.obj_header(obj0)
            n0 = self.obj_name(obj0)
            c0 = self.class_name(obj0)
            checks.append(("obj0 name", n0 == "/Script/CoreUObject", n0 or "?"))
            checks.append(("obj0 class", c0 == "Package", c0 or "?"))
            checks.append(("obj0 index", idx0 == 0, str(idx0)))
            if len(items) > 1:
                idx1, obj1 = items[1]
                n1 = self.obj_name(obj1)
                c1 = self.class_name(obj1)
                checks.append(("obj1 name", n1 == "Object", n1 or "?"))
                checks.append(("obj1 class", c1 == "Class", c1 or "?"))
            ok = all(p for _, p, _ in checks)
            return ok, checks
        except Exception as e:
            return False, [("self_test", False, str(e))]

    def find_class_named(self, name, max_scan=0):
        """Find UClass objects with the given name."""
        for idx, obj in self.iter_objects(max_scan):
            n = self.obj_name(obj)
            if n == name and self.obj_name(self.obj_header(obj)["class"]) in ("Class", "BlueprintGeneratedClass", "ScriptStruct"):
                yield idx, obj


def main():
    gp = GameProcess().attach()
    game = UEGame(gp).discover()

    # sanity: dump first 25 objects with names
    print("first objects:")
    for idx, obj in game.iter_objects(25):
        h = game.obj_header(obj)
        cn = game.class_name(obj)
        nn = game.resolve_name(h["name_idx"], h["name_num"])
        print(f"  [{idx:6d}] 0x{obj:X} class={cn} name={nn}")

    # find UWorld ("World" / "GameWorld" etc.)
    t0 = time.time()
    worlds = list(game.find_by_name("World", 300000))
    print(f"found {len(worlds)} objects named 'World' in {time.time()-t0:.0f}s: {[(i, hex(o)) for i, o in worlds[:5]]}")


if __name__ == "__main__":
    main()
