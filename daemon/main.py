"""Gamma Toolkit daemon — JSON-RPC over stdio, driven by the Electron app.

Protocol: one JSON object per line, both directions.
  in   {"id": 1, "method": "bot.attach", "params": {...}}
  out  {"id": 1, "ok": true, "result": {...}}          reply
  out  {"event": "hunt", "data": {...}}                 unsolicited push

Long jobs (extraction, hunts) run on worker threads and stream `event` lines, so
the UI never blocks. stdout is the protocol channel ONLY -- every diagnostic goes
to stderr, because a stray print() would corrupt the stream.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Rendered previews land here, not in the repo: a GIF costs a couple of seconds
# to build and users click back and forth constantly. Electron passes its own
# userData path in; this default is for running the daemon by hand.
# Bump when texture/animation decoding changes, so stale renders are not served
DECODER_VERSION = 3

CACHE_DIR = Path(os.environ.get("GAMMA_CACHE")
                 or Path(os.environ.get("LOCALAPPDATA", Path.home()))
                 / "GammaToolkit" / "cache")

from gamma import versions, layouts          # noqa: E402
from gamma.memory import GameProcess          # noqa: E402
from gamma.ue import UEGame                   # noqa: E402
from gamma.shiny import ShinyEngine, odds_text  # noqa: E402
from gamma.hunt import (StarterHunter, Player, StarterScene,  # noqa: E402
                        odds_from_rate, STARTERS, STARTER_FLAG)
from gamma import input as gameinput          # noqa: E402
from gamma import nav                          # noqa: E402
from gamma import saves                        # noqa: E402
from gamma import wild as wildmod                # noqa: E402
from gamma import travel as travelmod            # noqa: E402
from gamma.wild import WildHunter, BattleManager  # noqa: E402
from gamma.encounter import EncounterHook, resolve_pokemon  # noqa: E402
from gamma.party import PartyTool             # noqa: E402
from gamma.money import MoneyScanner, read_money, write_money  # noqa: E402
from gamma import items as itemsmod            # noqa: E402

_out_lock = threading.Lock()


def send(obj):
    with _out_lock:
        sys.stdout.write(json.dumps(obj, default=str) + "\n")
        sys.stdout.flush()


def emit(event, data):
    send({"event": event, "data": data})


def log(*a):
    print(*a, file=sys.stderr, flush=True)


class Session:
    """Everything stateful the UI can talk to."""

    def __init__(self):
        self.gp = None
        self.game = None
        self.layout = None
        self.version = "ea"
        self.shiny = None
        self.player = None
        self.hunter = None
        self.hunt_thread = None
        self.input = None
        self.worlds = []
        self.libs = {}
        self._ue_cache = {}   # module base -> discovered addresses, reused on re-attach
        self.game_exe = versions.load_game_exe()
        self.encounter = None
        self.party = None
        self.money = None
        self._cheat_busy = False
        self.wild = None
        self.wild_thread = None

    # ------------------------------------------------------------- discovery
    def versions(self, _p):
        out = []
        for vid, spec in versions.VERSIONS.items():
            exe = spec.exe
            out.append({
                "id": vid, "name": spec.name, "engine": spec.engine,
                "container": spec.container, "asset_format": spec.asset_format,
                "sha8": spec.sha8,
                "installed": bool(exe and Path(exe).exists()),
                "game_dir": str(spec.game_dir),
                "encrypted": spec.container == "pak",
                "has_key": bool(spec.find_aes_key()),
            })
        if self.game_exe and Path(self.game_exe).is_file():
            try:
                spec = versions.from_exe(self.game_exe)
                custom = {
                    "id": "custom", "name": spec.name, "engine": spec.engine,
                    "container": spec.container, "asset_format": spec.asset_format,
                    "sha8": spec.sha8, "installed": True,
                    "game_dir": str(spec.game_dir),
                    "encrypted": spec.container == "pak",
                    "has_key": bool(spec.find_aes_key()),
                }
            except Exception:
                custom = {
                    "id": "custom", "name": Path(self.game_exe).name,
                    "installed": True, "game_dir": str(Path(self.game_exe).parent),
                    "encrypted": False, "has_key": False,
                    "engine": "", "container": "", "asset_format": "", "sha8": "",
                }
            out = [custom] + [v for v in out if v["id"] != "custom"]
        return {"versions": out}

    def processes(self, _p):
        """Running game processes the user can attach to."""
        import subprocess
        out = []
        try:
            raw = subprocess.check_output(
                ["tasklist", "/FI", "IMAGENAME eq PokemonEmerald.exe", "/FO", "CSV", "/NH"],
                stderr=subprocess.DEVNULL).decode("utf-8", "replace")
            for line in raw.splitlines():
                parts = [p.strip('"') for p in line.split('","')]
                if len(parts) >= 5 and parts[0].lower().startswith("pokemonemerald"):
                    mem_kb = int(parts[4].replace(",", "").replace(" K", "") or 0)
                    out.append({"pid": int(parts[1]), "mem_mb": mem_kb // 1024})
        except Exception as e:
            log("tasklist failed:", e)
        # the launcher shim is tiny; the real game is the big one
        out.sort(key=lambda p: -p["mem_mb"])
        for p in out:
            p["likely_game"] = p["mem_mb"] > 500
        return {"processes": out}

    # ------------------------------------------------------------------- bot
    def attach(self, p):
        """Attach and return quickly.

        This used to finish with a full Player.find -- a ~250k object walk that
        resolves a NAME per object and takes the better part of half a minute.
        Attach sat on "discovering" for that whole time for no reason: status()
        already looks for the pawn on a throttle, and nothing needs it before the
        user has picked something to do. The scan addresses are also reused
        between attaches, because they do not move for the life of the process.
        """
        pid = p.get("pid")
        self.version = p.get("version", self.version)
        self.gp = GameProcess(pid=pid, name="PokemonEmerald").attach()

        emit("status", {"stage": "reading the object array"})
        cache = self._ue_cache.get(self.gp.module_base)
        game = UEGame(self.gp)
        self.game = game.discover_cached(cache) if cache else game.discover()
        self._ue_cache[self.gp.module_base] = self.game.cache()

        self.layout = layouts.BY_VERSION.get(self.version, layouts.UE56)
        self.shiny = ShinyEngine(self.game, self.layout)
        self.encounter = EncounterHook(self.gp)
        self.party = PartyTool(self.gp)
        self.money = MoneyScanner(self.gp)
        self.worlds = []
        self.player = None
        self._last_find = 0.0            # let status() pick the pawn up
        hwnd = gameinput.find_window(self.gp.pid)
        self.input = gameinput.GameInput(hwnd, pid=self.gp.pid) if hwnd else None
        emit("status", {"stage": "attached"})

        # The offset checks walk the object array twice (~17s). They are a
        # diagnostic, not a precondition, so they run in the background and
        # arrive as an event -- waiting on them was most of the attach time.
        def verify():
            try:
                checks = layouts.verify(self.game, self.layout)
                emit("checks", {"checks": [{"name": n, "ok": ok, "detail": d}
                                           for n, ok, d in checks]})
            except Exception as e:
                log("verify failed:", e)

        threading.Thread(target=verify, daemon=True).start()

        # Warm the class-pointer caches. The first scan of a class costs a few
        # seconds because every object's class has to be read once; after that
        # the same query is ~0.15s. Doing it here means the first thing the user
        # clicks is not the one that pays for it -- wild.state took 20s cold.
        def warm():
            for cname in ("World", "BP_GrassTile_C", "BP_BattleManager_C",
                          "BP_GE_TeleportVolume_C", "BP_PokemonMaster_C",
                          "RouteData"):
                if self.game is None:
                    return
                try:
                    self.game.actors_of_class(cname)
                except Exception as e:
                    log("warm %s failed: %s" % (cname, e))
            emit("warm", {"ready": True})

        threading.Thread(target=warm, daemon=True).start()

        return {
            "pid": self.gp.pid,
            "module_base": hex(self.gp.module_base),
            "objects": self.game.gobjects["num_elements"],
            "name_blocks": len(self.game.namepool["blocks"]),
            "layout": self.layout.engine,
            "checks": [],                # arrive later via the `checks` event
            "checking": True,
            "hwnd": hex(hwnd) if hwnd else None,
            "player": None,
        }

    def _need_game(self):
        if not self.game:
            raise RuntimeError("not attached")

    def status(self, _p):
        """Cheap enough to poll once a second.

        This used to run a full Player.find (a ~240k object walk) on EVERY poll.
        With the UI polling at 1Hz that piled up scans faster than they finished
        and starved the hunt thread -- the hunt sat on "waiting for player"
        forever. Now: reuse the cached pawn, and only re-search on a throttle.
        """
        if not self.game:
            return {"attached": False}
        d = {"attached": True, "pid": self.gp.pid}

        if self.hunter:
            # while a hunt owns the game, report ITS view; it keeps its own pawn
            d["hunt"] = self.hunter.stats.as_dict()
            p = self.hunter.player
            d["player"] = p.snapshot() if (p and p.alive()) else None
            return d

        if self.player and self.player.alive():
            d["player"] = self.player.snapshot()
            return d

        # The deep search walks every object and takes ~24s, so it CANNOT run on
        # the RPC thread: doing so froze the whole UI for the duration, which is
        # what "Attach" hanging on discovering actually was. Kick it off in the
        # background and answer immediately.
        d["player"] = None
        d["searching"] = self._find_player_async()
        if self.player and self.player.alive():
            d["player"] = self.player.snapshot()
            d["searching"] = False
        return d

    def _find_player_async(self) -> bool:
        """Start a background pawn search if one is warranted. True if running."""
        th = getattr(self, "_find_thread", None)
        if th and th.is_alive():
            return True
        now = time.time()
        if now - getattr(self, "_last_find", 0) < 5.0:
            return False
        self._last_find = now

        def work():
            try:
                # cheap path first: hundreds of actors in the known worlds
                p = Player.find_in_worlds(self.game, self.layout, self.worlds)
                if p is None:
                    p = Player.find(self.game, worlds_out=self.worlds)
                if p is not None:
                    self.player = p
                    emit("player", p.snapshot())
            except Exception as e:
                log("player search failed:", e)

        self._find_thread = threading.Thread(target=work, daemon=True)
        self._find_thread.start()
        return True

    # ---------------------------------------------------------------- shiny
    def shiny_scan(self, _p):
        self._need_game()
        sites = self.shiny.scan()
        return {
            "count": len(sites),
            "odds": odds_text(self.shiny.current_odds() or 0),
            "sites": [s.as_dict() for s in sites[:200]],
        }

    def shiny_set(self, p):
        """Set the float-based odds (encounters etc). Not the starter roll."""
        self._need_game()
        if not self.shiny.sites:
            self.shiny.scan()
        n = self.shiny.set_odds(float(p["probability"]))
        return {"patched": n, "odds": odds_text(self.shiny.current_odds() or 0)}

    def shiny_restore(self, _p):
        self._need_game()
        return {"restored": self.shiny.restore()}

    def shiny_verify(self, _p):
        self._need_game()
        if not self.shiny.sites:
            self.shiny.scan()
        ok, detail = self.shiny.verify_roundtrip()
        return {"ok": ok, "detail": detail}

    # --------------------------------------------------- starter shiny (REAL)
    # BP_GE_PickStarterPlayer_C is the verified mechanism. BP_Brendan_C.ShinyRate
    # looks like it should work and does not -- see hunt.py.
    def starter_state(self, _p):
        self._need_game()
        sc = StarterScene.find(self.game)
        if not sc:
            return {"scene": None,
                    "hint": "open the starter screen first (talk to Birch's bag)"}
        d = sc.snapshot()
        d["odds"] = "1/1 (forced)" if any(d[f] for f in STARTER_FLAG.values()) else "natural"
        return {"scene": d}

    def starter_force(self, p):
        """Write the shiny flag for one starter. VERIFIED to produce a shiny."""
        self._need_game()
        sc = StarterScene.find(self.game)
        if not sc:
            raise RuntimeError("starter screen is not open")
        starter = p.get("starter", "treecko")
        ok = sc.force(starter)
        return {"forced": ok, "starter": starter, "scene": sc.snapshot()}

    # legacy names kept so the UI keeps working; they now drive the real thing
    def rate_get(self, _p):
        return self.starter_state(_p)

    def rate_set(self, p):
        return self.starter_force(p)

    def _need_cheats(self):
        self._need_game()
        if not self.encounter or not self.party or not self.money:
            raise RuntimeError("not attached")

    def _cheat_job(self, event, fn):
        if self._cheat_busy:
            raise RuntimeError("a cheat scan is already running")
        self._cheat_busy = True

        def work():
            try:
                emit(event, {"kind": "working"})
                result = fn()
                emit(event, {"kind": "done", **(result or {})})
            except Exception as e:
                log(event, "failed:", traceback.format_exc())
                emit(event, {"kind": "error", "error": f"{type(e).__name__}: {e}"})
            finally:
                self._cheat_busy = False

        threading.Thread(target=work, daemon=True).start()
        return {"started": True}

    # ----------------------------------------------------------- save files
    def saves_info(self, _p):
        """Where the save is and whether the game is holding it open.

        A restore while the game is running looks like it did nothing: the
        running game still has the old save in memory and writes it back out at
        the next save point, so the UI warns rather than silently losing work.
        """
        out = saves.info()
        out["game_running"] = bool(self.processes(None)["processes"])
        return out

    def saves_list(self, _p):
        return {"backups": saves.list_backups()}

    def saves_create(self, p):
        return saves.create(label=p.get("label") or "")

    def saves_restore(self, p):
        return saves.restore(str(p["id"]))

    def saves_delete(self, p):
        return saves.delete(str(p["id"]))

    def encounter_status(self, _p):
        self._need_cheats()
        return self.encounter.status()

    def encounter_species(self, _p):
        """Every species the hook can force, with its dex number."""
        self._need_cheats()
        self._need_game()
        table = self.encounter.species_map(self.game)
        return {"species": sorted(
            ({"name": n, "dex": d} for n, d in table.items()),
            key=lambda s: s["dex"])}

    def encounter_set(self, p):
        """Enable or disable the wild-encounter hook. Finding the species DB can
        take about a minute the first time after opening the Pokedex."""
        self._need_cheats()
        if not p.get("enabled", True):
            return self.encounter.clear()

        # The name is enough: the game's own species database knows the dex
        # number, so it is looked up rather than typed. An explicit dex still
        # wins, and an unknown name still falls through to resolve_pokemon so
        # "38" keeps working as a name.
        name = p.get("name") or ""
        dex_in = p.get("dex")
        if name and (dex_in is None or str(dex_in).strip() == ""):
            try:
                table = self.encounter.species_map(self.game)
            except Exception:
                table = {}
            hit = next((d for n, d in table.items() if n.lower() == name.lower()),
                       None)
            if hit is not None:
                dex_in = hit
        label, dex = resolve_pokemon(name, dex_in)
        shiny = bool(p.get("shiny"))

        def work():
            return self.encounter.set(dex, shiny=shiny, label=label)

        return self._cheat_job("encounter", work)

    def encounter_clear(self, _p):
        self._need_cheats()
        return self.encounter.clear()

    def party_list(self, p):
        self._need_cheats()
        rescan = bool(p.get("rescan"))

        def work():
            return self.party.list(rescan=rescan)

        return self._cheat_job("party", work)

    def party_set_shiny(self, p):
        self._need_cheats()
        slot = int(p["slot"])
        shiny = bool(p.get("shiny"))

        def work():
            r = self.party.set_shiny(slot, shiny)
            listed = self.party.list(rescan=False)
            r["slots"] = listed["slots"]
            r["count"] = listed["count"]
            return r

        return self._cheat_job("party", work)

    # ------------------------------------------------------------------ bag
    def items_catalogue(self, _p):
        """Every item this build defines -- what can legitimately be spawned."""
        self._need_game()
        return {"items": itemsmod.catalogue(self.game)}

    def items_bag(self, _p):
        self._need_game()
        return itemsmod.bag(self.game)

    def items_set(self, p):
        """Add an item, change how many you hold, or drop it with quantity 0."""
        self._need_game()
        return itemsmod.set_quantity(self.game, str(p["name"]), int(p["quantity"]))

    def items_remove(self, p):
        self._need_game()
        return itemsmod.remove(self.game, str(p["name"]))

    def money_get(self, _p):
        """The player's money, read straight off the inventory system."""
        self._need_game()
        value = read_money(self.game)
        return {"money": value, "found": value is not None}

    def money_set(self, p):
        self._need_game()
        return write_money(self.game, int(p["amount"]))

    def money_status(self, _p):
        self._need_cheats()
        return self.money.status()

    def money_scan(self, p):
        self._need_cheats()
        value = int(p["value"])

        def work():
            return self.money.scan(value)

        return self._cheat_job("money", work)

    def money_narrow(self, p):
        self._need_cheats()
        value = int(p["value"])

        def work():
            return self.money.narrow(value)

        return self._cheat_job("money", work)

    def money_write(self, p):
        self._need_cheats()
        return self.money.write(int(p["amount"]))

    def money_reset(self, _p):
        self._need_cheats()
        return self.money.reset()

    # ------------------------------------------------------- encounter odds
    # One denominator, both knobs. There are two separate mechanisms and the UI
    # should not make the user care: the Blueprint float constants carry the
    # encounter probability, and BP_Brendan_C.ShinyRate is a rand(0..rate)==0
    # bound. Setting only one of them leaves the other contradicting it.
    #
    # NB this is the WILD/encounter roll. The starter is a separate, binary
    # mechanism (starter_force) -- ShinyRate was proven not to drive it.
    def _scan_sites_async(self) -> bool:
        """Start the Blueprint constant scan in the background. True if running.

        The scan walks every Blueprint function (~37s). Running it inline made
        the UI freeze right after attach, which looked exactly like the attach
        itself hanging.
        """
        th = getattr(self, "_scan_thread", None)
        if th and th.is_alive():
            return True
        if self.shiny.sites:
            return False

        def work():
            try:
                self.shiny.scan()
                emit("odds", self.odds_get({}))
            except Exception as e:
                log("shiny scan failed:", e)

        self._scan_thread = threading.Thread(target=work, daemon=True)
        self._scan_thread.start()
        return True

    def odds_get(self, _p):
        self._need_game()
        if not self.shiny.sites:
            if self._scan_sites_async():
                return {"scanning": True, "denominator": None, "sites": 0,
                        "text": "scanning…"}
        prob = self.shiny.current_odds()
        rate = None
        pl = self.player if (self.player and self.player.alive()) else None
        if pl:
            rate = pl.shiny_rate
        denom = None
        if prob and prob > 0:
            denom = int(round(1.0 / prob))
        elif rate is not None:
            denom = int(rate) + 1
        return {"denominator": denom, "probability": prob, "shiny_rate": rate,
                "text": odds_text(prob or 0), "sites": len(self.shiny.sites)}

    def odds_set(self, p):
        self._need_game()
        denom = max(1, int(p["denominator"]))
        if not self.shiny.sites:
            self.shiny.scan()
        patched = self.shiny.set_odds(1.0 / denom)
        rate_ok = False
        pl = self.player if (self.player and self.player.alive()) else None
        if pl:
            rate_ok = bool(pl.set("ShinyRate", denom - 1))
        return {"denominator": denom, "patched": patched, "rate_set": rate_ok,
                "text": odds_text(1.0 / denom)}

    # ----------------------------------------------------------------- hunt
    def hunt_start(self, p):
        self._need_game()
        if self.hunt_thread and self.hunt_thread.is_alive():
            raise RuntimeError("hunt already running")
        if not self.input:
            raise RuntimeError("no game window for input")
        self.hunter = StarterHunter(
            self.game, self.layout, self.input, player=self.player,
            on_event=lambda ev: emit("hunt", ev),
            starter=p.get("starter", "torchic"),
            open_bag=p.get("open_bag", True))
        self.hunter.force_shiny = bool(p.get("force_shiny", False))
        self.hunter.worlds = self.worlds
        spec = self.spec_for()
        raw = Path(self.game_exe) if self.game_exe else Path(spec.exe)
        self.hunter.exe_path = versions.resolve_game_binary(raw)
        self.hunter.reattach = self._reattach
        rate = p.get("rate")
        maxa = int(p.get("max_attempts", 0) or 0)
        self.hunt_thread = threading.Thread(
            target=self.hunter.run, kwargs={"max_attempts": maxa, "rate": rate},
            daemon=True)
        self.hunt_thread.start()
        return {"started": True, "starter": self.hunter.starter_id}

    def hunt_attempt(self, p):
        """ONE attempt, no reset. This is the safe way to exercise the loop while
        StarterHunter.reset_keys is still an uncalibrated placeholder -- a full run
        would pick correctly and then mash keys at a menu that never opened."""
        self._need_game()
        if not self.input:
            raise RuntimeError("no game window for input")
        if not self.hunter:
            self.hunter = StarterHunter(
                self.game, self.layout, self.input, player=self.player,
                on_event=lambda ev: emit("hunt", ev),
                starter=p.get("starter", "treecko"),
                open_bag=p.get("open_bag", True))
        else:
            self.hunter.configure(starter=p.get("starter"), open_bag=p.get("open_bag"))
        self.hunter.worlds = self.worlds
        shiny = self.hunter.attempt()
        return {"shiny": shiny, "stats": self.hunter.stats.as_dict()}

    def _reattach(self, hunter):
        """Rebuild every handle after the game restarts: new PID, new window,
        new UObject addresses. Nothing from the old session survives."""
        procs = self.processes({}).get("processes", [])
        pid = next((x["pid"] for x in procs if x.get("likely_game")), None)
        if not pid:
            return False
        self.gp = GameProcess(pid=pid).attach()
        self.game = UEGame(self.gp).discover()
        hwnd = gameinput.find_window(pid)
        if not hwnd:
            return False
        self.input = gameinput.GameInput(hwnd, pid=pid)
        self.shiny = ShinyEngine(self.game, self.layout)
        self.encounter = EncounterHook(self.gp)
        self.party = PartyTool(self.gp)
        self.money = MoneyScanner(self.gp)
        self.worlds = []
        self.player = None
        hunter.game = self.game
        hunter.gp = self.gp
        hunter.input = self.input
        hunter.player = None
        return True

    def hunt_stop(self, _p):
        if self.hunter:
            self.hunter.stop()
        return {"stopped": True}

    # -------------------------------------------------------------- utilities
    def find_player(self, _p):
        """Re-locate BP_Brendan_C — needed after loading a save, since attaching on
        the title screen finds no pawn."""
        self._need_game()
        self.worlds = []
        self.player = Player.find(self.game, worlds_out=self.worlds)
        if self.hunter:
            self.hunter.player = self.player
        return {"player": self.player.snapshot() if self.player else None}

    # ------------------------------------------------------------ game path
    def spec_for(self, version=None):
        """The chosen install: an exe the user pointed at, else a built-in."""
        if self.game_exe:
            try:
                return versions.from_exe(self.game_exe)
            except Exception as e:
                log("game path unusable, falling back:", e)
        return versions.get(version or self.version)

    def set_game_path(self, p):
        """Point the toolkit at an install anywhere on disk.

        The chosen exe is remembered even if the pak layout cannot be probed —
        Launch only needs a file that exists. Assets/AES follow from from_exe
        when that succeeds.
        """
        exe = p.get("exe")
        if not exe:
            self.game_exe = None
            versions.save_game_exe(None)
            return {"exe": None}
        path = Path(exe).expanduser()
        if not path.is_file():
            raise RuntimeError(f"no such file: {path}")
        self.game_exe = str(path)
        versions.save_game_exe(self.game_exe)
        self.libs.pop("custom", None)
        try:
            return self.game_path({})
        except Exception as e:
            log("layout probe failed; exe is still saved for launch:", e)
            return {
                "exe": self.game_exe, "name": path.name,
                "game_dir": str(path.parent),
                "container": None, "asset_format": None, "pak": None,
                "has_key": False, "running": bool(self._game_pids()),
                "warning": str(e),
            }

    def game_path(self, _p):
        if not self.game_exe:
            return {"exe": None}
        spec = versions.from_exe(self.game_exe)
        return {
            "exe": str(spec.exe), "name": spec.name,
            "game_dir": str(spec.game_dir),
            "container": spec.container, "asset_format": spec.asset_format,
            "pak": str(spec.pak) if spec.pak else None,
            "has_key": bool(spec.find_aes_key()),
            "running": bool(self._game_pids()),
        }

    @staticmethod
    def _game_pids():
        import subprocess
        out = []
        try:
            raw = subprocess.check_output(
                ["tasklist", "/FI", "IMAGENAME eq PokemonEmerald.exe", "/FO", "CSV", "/NH"],
                stderr=subprocess.DEVNULL).decode("utf-8", "replace")
            for line in raw.splitlines():
                parts = [q.strip('"') for q in line.split('","')]
                if len(parts) >= 5 and parts[0].lower().startswith("pokemonemerald"):
                    mb = int(parts[4].replace(",", "").replace(" K", "") or 0) // 1024
                    out.append((int(parts[1]), mb))
        except Exception:
            pass
        return out

    def launch_game(self, p):
        """Start the game and (optionally) attach once it is up."""
        exe = versions.resolve_game_binary(versions.resolve_launch_exe(
            explicit=p.get("exe"), stored=self.game_exe,
            version=p.get("version", self.version)))
        self.game_exe = str(exe)
        versions.save_game_exe(self.game_exe)
        versions.spawn_game(exe)

        if not p.get("attach", True):
            return {"launched": str(exe), "attached": False}

        # Boot takes ~40s and the launcher shim shows up first, so wait for a
        # process big enough to actually be the game before attaching.
        def wait_and_attach():
            deadline = time.time() + float(p.get("timeout", 240))
            while time.time() < deadline:
                time.sleep(2.0)
                big = [pid for pid, mb in self._game_pids() if mb > 500]
                if not big:
                    continue
                try:
                    r = self.attach({"pid": big[0],
                                     "version": p.get("version", self.version)})
                    emit("attached", r)
                    return
                except Exception as e:
                    log("auto-attach retry:", e)
            emit("attached", {"error": "game did not come up in time"})

        threading.Thread(target=wait_and_attach, daemon=True).start()
        return {"launched": str(exe), "attaching": True}

    def widgets_list(self, p):
        self._need_game()
        want = (p.get("match") or "").lower()
        out = []
        for _idx, o in self.game.iter_objects(0):
            cn = self.game.class_name(o) or ""
            if not cn.startswith("W_") and "Widget" not in cn:
                continue
            if want and want not in cn.lower():
                continue
            out.append({"addr": hex(o), "cls": cn, "name": self.game.obj_name(o)})
            if len(out) >= 400:
                break
        return {"widgets": out}

    def hunt_stats(self, _p):
        return self.hunter.stats.as_dict() if self.hunter else {}

    # ---------------------------------------------------------------- input
    def input_tap(self, p):
        if not self.input:
            raise RuntimeError("no game window")
        self.input.tap(p["key"])
        return {"sent": p["key"]}

    def starters(self, _p):
        return {"starters": list(STARTERS)}

    # -------------------------------------------------------------- travel
    def travel_destinations(self, _p):
        """Maps you can reach from where you are standing right now."""
        self._need_game()
        pos = None
        try:
            pos = nav.location(self.gp, self._pawn_addr())
        except Exception:
            pass
        dests = travelmod.destinations(self.game, from_pos=pos)
        out = []
        for d in dests:
            out.append({
                "map": d["map"], "volume": d["addr"], "via": d["name"],
                "distance": round(nav.dist2d(pos, d["pos"])) if (pos and d["pos"]) else None,
            })
        return {"here": sorted(travelmod.loaded_worlds(self.game)),
                "destinations": out}

    def travel_maps(self, p):
        """Every map in the game, with the ones you can reach right now marked.

        Travel works by walking the player into one of the current level's own
        teleport volumes, so only that level's exits actually go anywhere --
        rewriting a volume's destination is ignored by the game (see
        gamma/travel.py). The full list is still worth showing: it is how you
        find a map by name, and it says plainly which ones are one hop away
        instead of leaving the user to guess.
        """
        maps = []
        try:
            lib = self.library(p.get("version"))
            # A few maps ship under more than one folder, so the raw level list
            # repeats names -- deduped here, or the picker shows "MAP_Route101"
            # twice with no way to tell the rows apart.
            maps = sorted({s["name"] for s in
                           lib.subjects("levels", limit=1000)["subjects"]})
        except Exception as e:
            log("travel.maps: could not list levels:", e)

        reachable, here = {}, []
        if self.game:
            try:
                pos = None
                try:
                    pos = nav.location(self.gp, self._pawn_addr())
                except Exception:
                    pass
                for d in travelmod.destinations(self.game, from_pos=pos):
                    reachable[d["map"]] = {
                        "volume": d["addr"], "via": d["name"],
                        "distance": (round(nav.dist2d(pos, d["pos"]))
                                     if (pos and d["pos"]) else None),
                    }
                here = sorted(travelmod.loaded_worlds(self.game))
            except Exception as e:
                log("travel.maps: could not read destinations:", e)

        # a reachable destination the level list did not know about still belongs
        # in the list -- being able to GO there is the point
        for name in reachable:
            if name not in maps:
                maps.append(name)

        rows = [{"map": m, **(reachable.get(m) or {}),
                 "reachable": m in reachable,
                 "current": m in here} for m in sorted(maps)]
        return {"here": here, "maps": rows,
                "reachable_count": len(reachable), "total": len(rows)}

    def travel_go(self, p):
        """Walk into a teleport volume so the game moves the player itself."""
        self._need_game()
        if not self.input:
            raise RuntimeError("no game window for input")
        vol = p.get("volume")
        if not vol:
            # allow travelling by map name
            pos = nav.location(self.gp, self._pawn_addr())
            match = [d for d in travelmod.destinations(self.game, from_pos=pos)
                     if d["map"] == p.get("map")]
            if not match:
                raise RuntimeError("no route to %s from here" % p.get("map"))
            vol = match[0]["addr"]
        return travelmod.travel(self.game, self.input, self._pawn_addr(), int(vol),
                                on_event=lambda ev: emit("travel", ev))

    # ----------------------------------------------------------- wild hunt
    def _pawn_addr(self):
        p = self.player if (self.player and self.player.alive()) else None
        if p is None:
            p = Player.find_in_worlds(self.game, self.layout, self.worlds)                 or Player.find(self.game, worlds_out=self.worlds)
            self.player = p
        if p is None:
            raise RuntimeError(
                "no player pawn — the game needs to be in the overworld. "
                "Load a save, and close any menu or battle first.")
        return p.addr

    def _battle_maps(self):
        if not getattr(self, "_bmaps", None):
            try:
                self._bmaps = wildmod.battle_map_names(self.library())
            except Exception as e:
                log("battle map list failed:", e)
                self._bmaps = set()
        return self._bmaps

    def wild_state(self, _p):
        """Everything the wild panel needs, cheap enough to poll."""
        self._need_game()
        m = BattleManager.find(self.game)
        pos = None
        tiles = []
        try:
            addr = self._pawn_addr()
            pos = nav.location(self.gp, addr)
            tiles = nav.grass_tiles(self.game)
        except Exception:
            pass
        bm = self._battle_maps()
        avail = {"route": None, "encounters": []}
        try:
            w = WildHunter(self.game, self.layout, self.input, battle_maps=bm)
            avail = w.available_species()
        except Exception:
            pass
        return {
            "manager": m.snapshot() if m else None,
            "in_encounter": wildmod.in_battle(self.game, bm),
            "route": avail.get("route"),
            "encounter_rate": avail.get("rate"),
            "available": avail.get("encounters", []),
            "player": [round(v, 1) for v in pos] if pos else None,
            "grass_tiles": len(tiles),
            "on_grass": bool(pos and tiles and nav.on_grass(pos, tiles)),
            "nearest_grass": (lambda n: [round(v) for v in n[1]] if n else None)(
                nav.nearest(pos, tiles) if (pos and tiles) else None),
            "hunt": self.wild.stats.as_dict() if self.wild else None,
        }

    def wild_goto_grass(self, _p):
        self._need_game()
        w = WildHunter(self.game, self.layout, self.input,
                       pawn_addr=self._pawn_addr(),
                       on_event=lambda ev: emit("wild", ev))
        ok = w.goto_grass()
        return {"on_grass": ok, "stats": w.stats.as_dict()}

    def wild_start(self, p):
        self._need_game()
        if self.wild_thread and self.wild_thread.is_alive():
            raise RuntimeError("wild hunt already running")
        if not self.input:
            raise RuntimeError("no game window for input")
        self.wild = WildHunter(self.game, self.layout, self.input,
                               pawn_addr=self._pawn_addr(),
                               on_event=lambda ev: emit("wild", ev),
                               force_shiny=bool(p.get("force_shiny")),
                               battle_maps=self._battle_maps(),
                               filters=p.get("filters") or {})
        max_enc = int(p.get("max_encounters") or 0)
        self.wild_thread = threading.Thread(
            target=self.wild.run, kwargs={"max_encounters": max_enc}, daemon=True)
        self.wild_thread.start()
        return {"started": True}

    def wild_stop(self, _p):
        if self.wild:
            self.wild.stop()
        return {"stopped": True}

    def wild_force(self, _p):
        """Write the wild shiny flag on the live battle manager."""
        self._need_game()
        m = BattleManager.find(self.game)
        if not m:
            raise RuntimeError("no battle manager")
        return {"forced": m.force_shiny(), "manager": m.snapshot()}

    # --------------------------------------------------------------- assets
    def library(self, version=None):
        """One AssetLibrary per build, kept alive. The pak index costs ~2s to read
        and nothing after that, so rebuilding it per call is the only thing that
        would make browsing feel slow."""
        spec = self.spec_for(version)
        key = spec.id if spec.id != "custom" else "custom:" + str(spec.exe)
        lib = self.libs.get(key)
        if lib is None:
            from gamma.assets import AssetLibrary
            lib = AssetLibrary(spec.id if spec.id != "custom" else "ea")
            lib.spec = spec              # read the pak the user pointed at
            self.libs[key] = lib
        return lib

    def assets_categories(self, p):
        return {"categories": self.library(p.get("version")).categories()}

    def assets_subjects(self, p):
        return self.library(p.get("version")).subjects(
            p["category"], p.get("query", ""), int(p.get("limit", 500)))

    def assets_search(self, p):
        return self.library(p.get("version")).search(
            p.get("query", ""), int(p.get("limit", 400)))

    def assets_entries(self, p):
        return self.library(p.get("version")).entries(p["dir"])

    def assets_preview(self, p):
        """A data: URI the renderer drops straight into <img> or <audio>.

        Rendered previews are cached on disk -- re-encoding a 48-frame GIF every
        time someone clicks back to it would make browsing feel broken.
        """
        from gamma.assets import data_uri
        lib = self.library(p.get("version"))
        kind, pid = p["kind"], p["id"]
        # The decoder version is part of the cache key. Without it, fixing a
        # decoding bug leaves every previously-viewed asset showing the OLD
        # broken render forever -- exactly what happened after the pixel
        # alignment fix: the Poke Balls looked unfixed until the cache was
        # cleared by hand. Bump DECODER_VERSION whenever decoding changes.
        key = "v%d_%s" % (DECODER_VERSION,
                          re.sub(r"[^A-Za-z0-9]+", "_", pid).strip("_")[-110:])

        if kind == "image":
            png = lib.sprite_png(pid)
            if not png:
                raise RuntimeError("no texture in " + pid.rsplit("/", 1)[-1])
            return {"kind": "image", "mime": "image/png",
                    "uri": data_uri(png, "image/png")}

        if kind == "animation":
            out = CACHE_DIR / "gif" / (key + ".gif")
            plan = lib.anim_plan(pid, max_frames=int(p.get("max_frames", 120)))
            if not out.exists():
                if not lib.animation_gif(pid, out,
                                         max_frames=int(p.get("max_frames", 120))):
                    raise RuntimeError("could not render "
                                       + pid.rsplit("/", 1)[-1])
            return {"kind": "animation", "mime": "image/gif", "path": str(out),
                    "uri": data_uri(out.read_bytes(), "image/gif"),
                    "frames": len(plan[1]) if plan else 0,
                    "fps": round(plan[0], 1) if plan else None}

        if kind == "audio":
            out = CACHE_DIR / "mp3" / (key + ".mp3")
            if not out.exists() and not lib.audio_mp3(pid, out):
                raise RuntimeError("no audio in " + pid.rsplit("/", 1)[-1])
            return {"kind": "audio", "mime": "audio/mpeg", "path": str(out),
                    "uri": data_uri(out.read_bytes(), "audio/mpeg")}

        raise ValueError("cannot preview " + kind)

    def assets_roster(self, p):
        return {"pokemon": self.library(p.get("version")).pokemon_roster()}

    def assets_front(self, p):
        """Front Idle battle sprite: PNG still or animated GIF."""
        from gamma.assets import data_uri
        lib = self.library(p.get("version"))
        name = p.get("name") or ""
        shiny = bool(p.get("shiny"))
        anim_id = lib.find_front_idle(name, shiny)
        if not anim_id:
            raise RuntimeError("no Front Idle sprite for " + name)
        if p.get("still"):
            png = lib.first_frame_png(anim_id)
            if not png:
                raise RuntimeError("could not decode " + name)
            return {"kind": "image", "mime": "image/png",
                    "uri": data_uri(png, "image/png"), "id": anim_id, "name": name}
        return self.assets_preview({
            "version": p.get("version"), "kind": "animation", "id": anim_id,
            "max_frames": int(p.get("max_frames", 48)),
        })

    def assets_extract(self, p):
        """Extract on a worker thread, reporting progress as events.

        A whole Pokemon is ~700 animations; blocking the RPC pipe on that would
        freeze the UI.
        """
        items = p["items"]                      # [{kind, id, name}, ...]
        out_dir = Path(p["out_dir"])
        lib = self.library(p.get("version"))
        out_dir.mkdir(parents=True, exist_ok=True)

        def work():
            done, failed = [], []
            for i, it in enumerate(items):
                emit("extract", {"done": i, "total": len(items),
                                 "current": it.get("name") or it["id"]})
                try:
                    name = re.sub(r'[<>:"/\\|?*]', "_", it.get("name") or "asset")
                    if it["kind"] == "animation":
                        r = lib.animation_gif(it["id"], out_dir / (name + ".gif"))
                    elif it["kind"] == "audio":
                        r = lib.audio_mp3(it["id"], out_dir / (name + ".mp3"))
                    elif it["kind"] == "image":
                        png = lib.sprite_png(it["id"])
                        r = out_dir / (name + ".png") if png else None
                        if r:
                            r.write_bytes(png)
                    else:                        # no preview: raw package bytes
                        r = out_dir / it["id"].rsplit("/", 1)[-1]
                        r.write_bytes(lib.read(it["id"]))
                    (done if r else failed).append(name)
                except Exception as e:
                    log("extract failed", it["id"], e)
                    failed.append(it.get("name") or it["id"])
            emit("extract", {"done": len(items), "total": len(items),
                             "finished": True, "written": len(done),
                             "failed": failed[:20], "out_dir": str(out_dir)})

        threading.Thread(target=work, daemon=True).start()
        return {"started": True, "total": len(items), "out_dir": str(out_dir)}


SESSION = Session()

METHODS = {
    "app.versions": SESSION.versions,
    "app.processes": SESSION.processes,
    "app.starters": SESSION.starters,
    "bot.attach": SESSION.attach,
    "bot.status": SESSION.status,
    "shiny.scan": SESSION.shiny_scan,
    "shiny.set": SESSION.shiny_set,
    "shiny.restore": SESSION.shiny_restore,
    "shiny.verify": SESSION.shiny_verify,
    "rate.get": SESSION.rate_get,
    "rate.set": SESSION.rate_set,
    "odds.get": SESSION.odds_get,
    "odds.set": SESSION.odds_set,
    "starter.state": SESSION.starter_state,
    "starter.force": SESSION.starter_force,
    "saves.info": SESSION.saves_info,
    "saves.list": SESSION.saves_list,
    "saves.create": SESSION.saves_create,
    "saves.restore": SESSION.saves_restore,
    "saves.delete": SESSION.saves_delete,
    "encounter.status": SESSION.encounter_status,
    "encounter.species": SESSION.encounter_species,
    "encounter.set": SESSION.encounter_set,
    "encounter.clear": SESSION.encounter_clear,
    "party.list": SESSION.party_list,
    "party.set_shiny": SESSION.party_set_shiny,
    "items.catalogue": SESSION.items_catalogue,
    "items.bag": SESSION.items_bag,
    "items.set": SESSION.items_set,
    "items.remove": SESSION.items_remove,
    "money.get": SESSION.money_get,
    "money.set": SESSION.money_set,
    "money.status": SESSION.money_status,
    "money.scan": SESSION.money_scan,
    "money.narrow": SESSION.money_narrow,
    "money.write": SESSION.money_write,
    "money.reset": SESSION.money_reset,
    "hunt.start": SESSION.hunt_start,
    "hunt.attempt": SESSION.hunt_attempt,
    "hunt.stop": SESSION.hunt_stop,
    "bot.find_player": SESSION.find_player,
    "app.launch": SESSION.launch_game,
    "app.set_game_path": SESSION.set_game_path,
    "app.game_path": SESSION.game_path,
    "widgets.list": SESSION.widgets_list,
    "hunt.stats": SESSION.hunt_stats,
    "input.tap": SESSION.input_tap,
    "travel.destinations": SESSION.travel_destinations,
    "travel.maps": SESSION.travel_maps,
    "travel.go": SESSION.travel_go,
    "wild.state": SESSION.wild_state,
    "wild.goto_grass": SESSION.wild_goto_grass,
    "wild.start": SESSION.wild_start,
    "wild.stop": SESSION.wild_stop,
    "wild.force": SESSION.wild_force,
    "assets.categories": SESSION.assets_categories,
    "assets.subjects": SESSION.assets_subjects,
    "assets.search": SESSION.assets_search,
    "assets.entries": SESSION.assets_entries,
    "assets.preview": SESSION.assets_preview,
    "assets.roster": SESSION.assets_roster,
    "assets.front": SESSION.assets_front,
    "assets.extract": SESSION.assets_extract,
}


def handle(msg):
    mid = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") or {}
    fn = METHODS.get(method)
    if not fn:
        send({"id": mid, "ok": False, "error": f"unknown method {method!r}"})
        return
    try:
        send({"id": mid, "ok": True, "result": fn(params)})
    except Exception as e:
        log("ERROR", method, traceback.format_exc())
        send({"id": mid, "ok": False, "error": f"{type(e).__name__}: {e}"})


def main():
    # PyInstaller console=True allocates a console; Alt+Enter on that subsystem
    # is "toggle fullscreen". Detach before we post any keys to the game.
    if getattr(sys, "frozen", False) and os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.FreeConsole()
        except Exception:
            pass
    emit("ready", {"pid": os.getpid(), "python": sys.version.split()[0]})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception as e:
            send({"ok": False, "error": f"bad json: {e}"})
            continue
        # each request on its own thread so a slow scan can't block the UI
        threading.Thread(target=handle, args=(msg,), daemon=True).start()


if __name__ == "__main__":
    main()
