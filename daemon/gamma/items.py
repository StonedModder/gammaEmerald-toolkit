"""Bag management: list what the game has, and change what you are carrying.

THE STRUCTURES, read out of the game's own reflection data rather than guessed:

  ItemInventorySystem
      +0x28 ItemsByCategory   TMap<ECategory(uint8), FItemCategoryInventory>
      +0x78 Money             int32                     (see money.py)
      +0x80 ItemDataManager   UItemDataManager*

  The map is a TSparseArray whose elements are 0x20 bytes:
      +0x00 key    uint8 category      (0..5, six categories)
      +0x08 TArray<FItemInstance>  {ptr, num, max}
      +0x18 hash next / hash index

  FItemInstance is 16 bytes:
      +0x00 ItemName  FName
      +0x08 ItemID    int32
      +0x0C Quantity  int32

  UItemDataManager.GlobalNameLookup  TMap<FName, UItemData*> -- every item the
  build defines (46 on the EA build), which is what "valid items" means here.

  UItemData: ItemName +0x30, ItemID +0x60, Category +0x64, BuyPrice +0x6C.

GROWING THE BAG, and why it is done the careful way: a TArray belongs to the
game's own allocator. Repointing it at memory we allocated works right up until
the game appends to it itself, at which point it calls Realloc on a pointer its
allocator never handed out and the process dies. So an append reuses the array's
existing spare capacity whenever there is any, and only falls back to allocating
a fresh block -- with generous headroom, so the game is unlikely to need to grow
it again -- when the category is completely empty.
"""
from __future__ import annotations

import struct

INVENTORY_CLASS = "ItemInventorySystem"
ITEMS_BY_CATEGORY = 0x28
ITEM_DATA_MANAGER = 0x80
GLOBAL_NAME_LOOKUP = 0x88

MAP_STRIDE = 0x20
MAP_KEY = 0x00
MAP_ARRAY = 0x08

ITEM_STRIDE = 0x10
ITEM_NAME = 0x00
ITEM_ID = 0x08
ITEM_QTY = 0x0C

DATA_NAME = 0x30
DATA_ID = 0x60
DATA_CATEGORY = 0x64
DATA_BUY = 0x6C
DATA_SELL = 0x70

LOOKUP_STRIDE = 0x18          # FName key (8) + UItemData* (8) + hash pair (8)

# Spare room left when a category has to be allocated from scratch, so the game
# can add a few items itself without reallocating our block.
SPARE_SLOTS = 64
MAX_QUANTITY = 999


def _inventory(game):
    found = game.actors_of_class(INVENTORY_CLASS)
    return found[0] if found else 0


def catalogue(game) -> list[dict]:
    """Every item this build defines, from the game's own name lookup."""
    gp = game.gp
    inv = _inventory(game)
    if not inv:
        return []
    mgr = gp.read_u64(inv + ITEM_DATA_MANAGER)
    if not mgr:
        return []
    head = gp.rpm(mgr + GLOBAL_NAME_LOOKUP, 16)
    if not head:
        return []
    data, _num, maximum = struct.unpack("<Qii", head)
    if not data or not (0 < maximum <= 4096):
        return []
    blob = gp.rpm(data, maximum * LOOKUP_STRIDE) or b""

    out = []
    for i in range(maximum):
        e = blob[i * LOOKUP_STRIDE:(i + 1) * LOOKUP_STRIDE]
        if len(e) < LOOKUP_STRIDE:
            break
        idx, num = struct.unpack_from("<II", e, 0)
        ptr = struct.unpack_from("<Q", e, 8)[0]
        hash_next = struct.unpack_from("<i", e, 0x10)[0]
        if hash_next != -1 or not (0x10000 < ptr < 0x7FFFFFFFFFFF):
            continue
        try:
            name = game.resolve_name(idx, num)
        except Exception:
            continue
        if not name:
            continue
        raw = gp.rpm(ptr + DATA_ID, 8)
        # Category is a 1-byte enum, not an int -- reading it as one produced
        # values like 16843264 and matched no pocket.
        item_id = struct.unpack_from("<i", raw, 0)[0] if raw else 0
        category = raw[DATA_CATEGORY - DATA_ID] if raw else 0
        price = gp.rpm(ptr + DATA_BUY, 8)
        buy, sell = struct.unpack("<ii", price) if price else (0, 0)
        out.append({"name": name, "id": item_id, "category": category,
                    "buy": buy, "sell": sell})
    out.sort(key=lambda i: i["name"].lower())
    return out


def _slots(game):
    """(element address, category, array address) for each live map entry."""
    gp = game.gp
    inv = _inventory(game)
    if not inv:
        return []
    head = gp.rpm(inv + ITEMS_BY_CATEGORY, 16)
    if not head:
        return []
    data, num, maximum = struct.unpack("<Qii", head)
    if not data or not (0 < maximum <= 1024):
        return []
    blob = gp.rpm(data, maximum * MAP_STRIDE) or b""

    out, seen = [], 0
    for i in range(maximum):
        e = blob[i * MAP_STRIDE:(i + 1) * MAP_STRIDE]
        if len(e) < MAP_STRIDE:
            break
        hash_next = struct.unpack_from("<i", e, 0x18)[0]
        if hash_next != -1:
            continue                       # free slot in the sparse array
        out.append((data + i * MAP_STRIDE, e[MAP_KEY],
                    data + i * MAP_STRIDE + MAP_ARRAY))
        seen += 1
        if seen >= num:
            break
    return out


def bag(game) -> dict:
    """What the player is carrying, by category."""
    gp = game.gp
    cats = {}
    for _elem, category, arr_addr in _slots(game):
        head = gp.rpm(arr_addr, 16)
        if not head:
            continue
        ptr, num, maximum = struct.unpack("<Qii", head)
        items = []
        if ptr and 0 < num <= 4096:
            blob = gp.rpm(ptr, num * ITEM_STRIDE) or b""
            for i in range(num):
                e = blob[i * ITEM_STRIDE:(i + 1) * ITEM_STRIDE]
                if len(e) < ITEM_STRIDE:
                    break
                idx, nnum = struct.unpack_from("<II", e, ITEM_NAME)
                item_id, qty = struct.unpack_from("<ii", e, ITEM_ID)
                try:
                    name = game.resolve_name(idx, nnum)
                except Exception:
                    name = None
                items.append({"name": name or "?", "id": item_id,
                              "quantity": qty, "slot": i})
        cats[category] = {"category": category, "items": items,
                          "count": num, "capacity": maximum}
    return {"categories": [cats[k] for k in sorted(cats)],
            "total": sum(len(c["items"]) for c in cats.values())}


def _find_entry(game, name: str):
    """(array address, slot index, quantity) for an item already in the bag."""
    want = name.strip().lower()
    for cat in bag(game)["categories"]:
        for it in cat["items"]:
            if (it["name"] or "").lower() == want:
                for _e, category, arr in _slots(game):
                    if category == cat["category"]:
                        return arr, it["slot"], it["quantity"]
    return None, None, None


def _catalogue_entry(game, name: str):
    want = name.strip().lower()
    for row in catalogue(game):
        if row["name"].lower() == want:
            return row
    return None


def _fname_of(game, name: str):
    """The (index, number) the game uses for this item's FName."""
    gp = game.gp
    inv = _inventory(game)
    mgr = gp.read_u64(inv + ITEM_DATA_MANAGER) if inv else 0
    if not mgr:
        return None
    head = gp.rpm(mgr + GLOBAL_NAME_LOOKUP, 16)
    data, _num, maximum = struct.unpack("<Qii", head)
    blob = gp.rpm(data, maximum * LOOKUP_STRIDE) or b""
    want = name.strip().lower()
    for i in range(maximum):
        e = blob[i * LOOKUP_STRIDE:(i + 1) * LOOKUP_STRIDE]
        if len(e) < LOOKUP_STRIDE:
            break
        if struct.unpack_from("<i", e, 0x10)[0] != -1:
            continue
        idx, num = struct.unpack_from("<II", e, 0)
        try:
            got = game.resolve_name(idx, num)
        except Exception:
            continue
        if got and got.lower() == want:
            return idx, num
    return None


def set_quantity(game, name: str, quantity: int) -> dict:
    """Add an item, change how many you have, or remove it with quantity 0."""
    quantity = int(quantity)
    if not 0 <= quantity <= MAX_QUANTITY:
        raise ValueError("quantity must be between 0 and %d" % MAX_QUANTITY)

    arr, slot, current = _find_entry(game, name)
    if arr is not None:
        if quantity == 0:
            return remove(game, name)
        ptr = game.gp.read_u64(arr)
        game.gp.wpm(ptr + slot * ITEM_STRIDE + ITEM_QTY,
                    struct.pack("<i", quantity))
        return {"name": name, "quantity": quantity, "was": current,
                "added": False}

    if quantity == 0:
        return {"name": name, "quantity": 0, "was": 0, "added": False}
    return add(game, name, quantity)


def add(game, name: str, quantity: int = 1) -> dict:
    """Put an item the player does not have into the right category."""
    gp = game.gp
    quantity = max(1, min(int(quantity), MAX_QUANTITY))
    row = _catalogue_entry(game, name)
    if not row:
        raise RuntimeError("%r is not an item in this build" % name)
    fname = _fname_of(game, row["name"])
    if not fname:
        raise RuntimeError("could not resolve the name for %r" % row["name"])

    arr = None
    for _elem, category, arr_addr in _slots(game):
        if category == row["category"]:
            arr = arr_addr
            break
    if arr is None:
        raise RuntimeError(
            "this save has no bag pocket for category %d yet" % row["category"])

    head = gp.rpm(arr, 16)
    ptr, num, maximum = struct.unpack("<Qii", head)
    entry = struct.pack("<IIii", fname[0], fname[1], row["id"], quantity)

    if ptr and num < maximum:
        # Room already reserved by the game's own allocator: the safe path.
        gp.wpm(ptr + num * ITEM_STRIDE, entry)
        gp.wpm(arr + 8, struct.pack("<i", num + 1))
        return {"name": row["name"], "quantity": quantity, "added": True,
                "grew": False}

    # Nothing allocated for this pocket. Hand it a block with headroom -- see
    # the note at the top about why this is a last resort.
    capacity = num + SPARE_SLOTS
    block = gp.alloc(capacity * ITEM_STRIDE)
    if not block:
        raise RuntimeError("could not allocate space for the bag")
    if ptr and num:
        old = gp.rpm(ptr, num * ITEM_STRIDE) or b""
        gp.wpm(block, old)
    gp.wpm(block + num * ITEM_STRIDE, entry)
    gp.wpm(arr, struct.pack("<Qii", block, num + 1, capacity))
    return {"name": row["name"], "quantity": quantity, "added": True,
            "grew": True}


def remove(game, name: str) -> dict:
    """Drop an item entirely. Shifts the rest down; never reallocates."""
    gp = game.gp
    arr, slot, current = _find_entry(game, name)
    if arr is None:
        return {"name": name, "removed": False}
    ptr = gp.read_u64(arr)
    num = struct.unpack("<i", gp.rpm(arr + 8, 4))[0]
    tail = gp.rpm(ptr + (slot + 1) * ITEM_STRIDE,
                  (num - slot - 1) * ITEM_STRIDE) or b""
    if tail:
        gp.wpm(ptr + slot * ITEM_STRIDE, tail)
    gp.wpm(arr + 8, struct.pack("<i", num - 1))
    return {"name": name, "removed": True, "was": current}
