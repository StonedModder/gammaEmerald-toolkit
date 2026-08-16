"""Per-build UE struct offsets — the single source of truth.

Before this existed the same offset literals were duplicated across gamma_bot.py,
cheats.py, console.py, widgets.py and gui.py, so an engine bump meant editing five
files and missing one. Everything reads from here now.

Every value below marked VERIFIED was measured against the running game, not copied
from a header. The measurement is described so it can be redone on the next bump.

UE 5.3 -> 5.6 turned out to be almost a no-op at runtime: the only confirmed break
is ULevel::Actors. Notably UNCHANGED: the GUObjectArray/NamePool discovery
signatures, FUObjectItem stride (32), the FNameEntry header packing, the UObject
header, UWorld::PersistentLevel, UStruct::Script and EX_FloatConst.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Layout:
    engine: str

    # --- UObjectBaseInternal ------------------------------------------------
    # VERIFIED both builds: self_test() resolves object[0] as Package
    # '/Script/CoreUObject' and object[1] as Class 'Object'.
    obj_class: int = 0x10          # ClassPrivate
    obj_name: int = 0x18           # NamePrivate (FName {u32 idx, u32 num})
    obj_outer: int = 0x20          # OuterPrivate

    # --- FUObjectArray / FUObjectItem --------------------------------------
    uobject_item_size: int = 32    # VERIFIED 5.6: InternalIndex matches slot index
    objobjects: int = 0x10         # FUObjectArray::ObjObjects
    num_elements: int = 0x14       # ...ObjObjects.NumElements

    # --- UWorld / ULevel ----------------------------------------------------
    world_persistent_level: int = 0x30   # VERIFIED 5.6: yields MAP_* levels
    # CHANGED in 5.6: 0x98 -> 0xA0. Found by scanning ULevel for a {ptr,count}
    # whose targets all resolve to real actor classes (WorldSettings,
    # StaticMeshActor, SkyLight...). 828 actors in MAP_Route101.
    level_actors: int = 0xA0
    level_actors_count: int = 0xA8

    # --- UStruct / UFunction ------------------------------------------------
    # VERIFIED 5.6 still 0x60. Beware: sampling the first few thousand UObjects
    # finds only NATIVE functions, whose Script is empty, which looks like "moved".
    # Filter on class_name(outer) == 'BlueprintGeneratedClass' -- 4,354 such
    # functions carry bytecode at 0x60.
    struct_script: int = 0x60
    struct_script_count: int = 0x68
    struct_child_properties: int = 0x50

    # --- FField / FProperty -------------------------------------------------
    field_class: int = 0x08
    field_next: int = 0x18
    field_name: int = 0x20
    prop_array_dim: int = 0x30
    prop_elem_size: int = 0x34
    prop_offset: int = 0x44

    # --- misc ---------------------------------------------------------------
    widget_visibility: int = 0x104
    input_settings_console_keys: int = 0x130
    controller_pawn: int = 0x2F8

    # --- Kismet -------------------------------------------------------------
    # VERIFIED 5.6: found the byte pattern 1e 00 00 80 3f (EX_FloatConst + 1.0f)
    # in real Blueprint bytecode, so the opcode did NOT shift with the engine.
    ex_float_const: int = 0x1E

    # Shiny odds constants to look for as EX_FloatConst operands.
    # VERIFIED 5.6: 0.01 appears at 45 sites (old build: 0.01 at 116 sites).
    shiny_odds: tuple = (0.01, 1 / 8192, 1 / 4096, 1 / 512, 1 / 100,
                         1 / 1024, 1 / 2048)


UE53 = Layout(engine="5.3")
UE56 = Layout(
    engine="5.6",
    level_actors=0xA0,
    level_actors_count=0xA8,
)

# keyed by the version id used in versions.py
BY_VERSION = {"original": UE53, "ea": UE56}


def for_engine(engine_string: str) -> Layout:
    """'5.6.1-44394996+++UE5+Release-5.6' -> the matching Layout."""
    s = engine_string or ""
    if s.startswith("5.6") or "Release-5.6" in s:
        return UE56
    if s.startswith("5.3") or "Release-5.3" in s:
        return UE53
    # Unknown engine: 5.6 layout is the better guess for anything newer, but the
    # caller should run verify() and not trust this blindly.
    return UE56


def verify(game, layout) -> list[tuple[str, bool, str]]:
    """Cheap runtime assertions that a Layout actually fits the live process.

    Returns [(check, ok, detail)]. Anything False means the offsets are wrong for
    this build and the bot must not write memory.
    """
    out = []
    try:
        ok, checks = game.self_test()
        out.append(("core objects/names", ok,
                    "; ".join(f"{n}={d}" for n, _, d in checks)))
    except Exception as e:
        out.append(("core objects/names", False, repr(e)))
        return out

    # a world with a persistent level and a plausible actor array
    worlds = 0
    actors = 0
    try:
        # bounded: this runs on every attach and the UI waits on it. A full walk of
        # ~258k objects three times over made attach take longer than a minute.
        for _idx, obj in game.iter_objects(0):
            if worlds >= 3:
                break
            if game.class_name(obj) != "World":
                continue
            pl = game.gp.read_u64(obj + layout.world_persistent_level)
            if not pl:
                continue
            worlds += 1
            ap = game.gp.read_u64(pl + layout.level_actors)
            cnt = game.gp.read_u32(pl + layout.level_actors_count)
            if ap and 0 < cnt < 100000:
                actors = max(actors, cnt)
        out.append(("world/level actors", actors > 0,
                    f"{worlds} worlds, max {actors} actors"))
    except Exception as e:
        out.append(("world/level actors", False, repr(e)))

    # blueprint bytecode readable at struct_script
    try:
        n = 0
        for _idx, o in game.iter_objects(0):
            if game.class_name(o) != "Function":
                continue
            h = game.obj_header(o)
            if not h or not h["outer"]:
                continue
            if game.class_name(h["outer"]) != "BlueprintGeneratedClass":
                continue
            p = game.gp.read_u64(o + layout.struct_script)
            c = game.gp.read_u32(o + layout.struct_script_count)
            if p and 0 < c < 200000:
                n += 1
            if n >= 10:
                break
        out.append(("blueprint bytecode", n >= 10, f"{n} functions with Script"))
    except Exception as e:
        out.append(("blueprint bytecode", False, repr(e)))
    return out
